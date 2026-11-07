"""Subprocess-based sandbox executor (MVP implementation).

Runs user/LLM code in a child process with:
- AST import/builtin safety check (pre-execution, no subprocess launched if blocked)
- Restricted environment variables
- subprocess timeout
- Resource limits via resource.setrlimit on POSIX; best-effort on Windows
- Per-run scratch directory (auto-cleaned after output copy)

Not a hard boundary on its own (see guard.py). For real isolation run jobs on
the gVisor sandbox worker: PHOTON_SANDBOX=remote (backend.py, D-038).
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from textwrap import dedent
from typing import Optional

from . import limits
from .base import Executor
from ...config import config
from ...models.dto import ExecutionResult

# ---------------------------------------------------------------------------
# AST safety checker
# ---------------------------------------------------------------------------

# Modules whose import is blocked (any name that starts with these prefixes)
_BLOCKED_IMPORT_PREFIXES: frozenset[str] = frozenset(
    {
        "subprocess",
        "socket",
        "requests",
        "http",
        "urllib",
        "ftplib",
        "smtplib",
        "telnetlib",
        "xmlrpc",
        "asyncio",  # can open sockets
        "multiprocessing",
        "threading",
        "ctypes",
        "cffi",
        "importlib",
        "pkgutil",
        "pickle",
        "marshal",
        "shelve",
        "dill",
        "gc",
        "inspect",
        "sys",
        "builtins",
        "pty",
        "signal",
        "shutil",
        "tempfile",
        "glob",
        "mmap",
        "resource",
    }
)

# Dangerous builtins / introspection names (any Load reference is rejected:
# getattr & co. are how string-built attribute names dodge the checks below).
_BLOCKED_BUILTINS: frozenset[str] = frozenset(
    {"eval", "exec", "__import__", "compile", "open", "breakpoint",
     "getattr", "setattr", "delattr", "globals", "locals", "vars",
     "__builtins__", "__loader__", "__spec__"}
)

# Calls blocked by attribute name, whatever the object (os.system, os.fork...).
_BLOCKED_CALL_ATTRS: frozenset[str] = frozenset(
    {"system", "popen", "exec", "execl", "execle", "execlp", "execlpe", "execv",
     "execve", "execvp", "execvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe",
     "spawnv", "spawnve", "spawnvp", "spawnvpe", "posix_spawn", "posix_spawnp",
     "fork", "forkpty", "kill", "killpg", "startfile", "putenv"}
)

# Dunder attributes allowed in attribute access; every other __x__ (e.g.
# __class__, __subclasses__, __globals__, __dict__, __reduce__) is rejected —
# they are the classic object-graph walk out of a Python sandbox.
_ALLOWED_DUNDER_ATTRS: frozenset[str] = frozenset(
    {"__init__", "__name__", "__doc__", "__len__", "__call__", "__enter__",
     "__exit__", "__iter__", "__next__", "__getitem__", "__setitem__",
     "__contains__", "__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__",
     "__hash__", "__repr__", "__str__", "__add__", "__sub__", "__mul__",
     "__truediv__", "__array__", "__version__"}
)
# Frame / generator / code internals: same reason, not dunders.
_BLOCKED_ATTRS: frozenset[str] = frozenset(
    {"f_globals", "f_locals", "f_builtins", "f_back", "f_code", "gi_frame", "gi_code",
     "cr_frame", "cr_code", "ag_frame", "tb_frame", "tb_next", "co_code", "func_globals"}
)

# Custom pipeline nodes receive arrays and return arrays; they never need the
# filesystem. strict_io=True (used for custom nodes) rejects file-I/O calls by
# attribute name. Run-code snippets read input_path and write output_dir, so
# they keep these (the runtime guard still confines them to those paths).
_FILE_IO_ATTRS: frozenset[str] = frozenset(
    {"open", "read_text", "read_bytes", "write_text", "write_bytes", "imread",
     "imreadmulti", "imwrite", "imsave", "imdecode", "load", "loads", "save", "savez",
     "savez_compressed", "fromfile", "tofile", "loadtxt", "savetxt", "genfromtxt",
     "memmap", "unlink", "rmdir", "mkdir", "makedirs", "rename", "replace", "remove",
     "removedirs", "touch", "symlink_to", "hardlink_to", "chmod", "listdir", "scandir",
     "walk", "glob", "rglob", "iterdir", "VideoCapture", "VideoWriter", "FileStorage",
     "TiffFile", "TiffWriter"}
)

# Allowed top-level module names (whitelist — anything else is blocked).
# Auto-synced from pyproject.toml at config import time (dep_sync.py) — add a
# library via `uv add xyz` + restart, not by editing this set.
_ALLOWED_IMPORT_TOPS: frozenset[str] = config.SANDBOX_ALLOWED_IMPORTS

# The runtime guard's source, inlined into the run-code harness (the child
# runs with PYTHONPATH="" and can't import the repo).
_GUARD_SOURCE: str = (Path(__file__).parent / "guard.py").read_text(encoding="utf-8")


class ASTSecurityError(Exception):
    """Raised when AST check rejects the submitted code."""


def _is_true(node) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _check_ast_safety(code: str, *, strict_io: bool = False) -> None:
    """Parse code and raise ASTSecurityError if any forbidden construct is found.

    First layer only: static checks on Python are bypassable, so every child
    also runs under the runtime guard (guard.py, D-036). Checks:
    1. Blocked builtins / introspection names (eval, exec, getattr, globals...)
    2. Blocked call attributes (os.system, fork, spawn...)
    3. Dunder / frame attribute access outside a small allowlist
    4. Unpickling: allow_pickle=True anywhere; torch.load without weights_only=True
    5. Import of blocked modules, or of modules not in the allowlist
    6. strict_io (custom nodes): any file-I/O call

    Raises:
        ASTSecurityError: with a descriptive message if any check fails.
        SyntaxError: if the code cannot be parsed.
    """
    tree = ast.parse(code, mode="exec")

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _BLOCKED_BUILTINS:
                raise ASTSecurityError(
                    f"Use of '{node.func.id}' is blocked by sandbox policy."
                )
            for kw in node.keywords:
                if kw.arg == "allow_pickle" and not (isinstance(kw.value, ast.Constant)
                                                     and kw.value.value is False):
                    raise ASTSecurityError("allow_pickle is blocked by sandbox policy "
                                           "(unpickling runs arbitrary code).")
            if isinstance(node.func, ast.Attribute):
                attr = node.func.attr
                if attr in _BLOCKED_CALL_ATTRS:
                    raise ASTSecurityError(
                        f"Call to '{attr}' is blocked by sandbox policy."
                    )
                if attr == "load" and isinstance(node.func.value, ast.Name) \
                        and node.func.value.id == "torch" \
                        and not any(kw.arg == "weights_only" and _is_true(kw.value)
                                    for kw in node.keywords):
                    raise ASTSecurityError("torch.load needs weights_only=True "
                                           "(unpickling runs arbitrary code).")
                if strict_io and attr in _FILE_IO_ATTRS:
                    raise ASTSecurityError(
                        f"File I/O ('{attr}') is not allowed in custom nodes: they "
                        "receive arrays via `inputs` and return arrays."
                    )

        if isinstance(node, ast.Attribute):
            a = node.attr
            if (a.startswith("__") and a.endswith("__") and a not in _ALLOWED_DUNDER_ATTRS) \
                    or a in _BLOCKED_ATTRS:
                raise ASTSecurityError(f"Access to '.{a}' is blocked by sandbox policy.")

        # Check Name references to blocked builtins
        if isinstance(node, ast.Name) and node.id in _BLOCKED_BUILTINS:
            if isinstance(getattr(node, "ctx", None), ast.Load):
                raise ASTSecurityError(
                    f"Reference to '{node.id}' is blocked by sandbox policy."
                )

        # Check imports
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                names = [node.module] if node.module else []
                if node.level:
                    raise ASTSecurityError("Relative imports are blocked by sandbox policy.")

            for name in names:
                if name is None:
                    continue
                top = name.split(".")[0]
                # Block list check (higher priority)
                if top in _BLOCKED_IMPORT_PREFIXES:
                    raise ASTSecurityError(
                        f"Import of '{name}' is blocked by sandbox policy."
                    )
                # Allow list check
                if top not in _ALLOWED_IMPORT_TOPS:
                    raise ASTSecurityError(
                        f"Import of '{name}' is not in the sandbox allowlist. "
                        f"Allowed top-level modules: {sorted(_ALLOWED_IMPORT_TOPS)}"
                    )


# ---------------------------------------------------------------------------
# Harness script template
# ---------------------------------------------------------------------------

_HARNESS_TEMPLATE = dedent(
    """\
    import json
    import sys
    import time
    import traceback as _tb

    # ---- sandbox guard (core/sandbox/guard.py, inlined) ----
    exec(compile({guard_source!r}, "<sandbox-guard>", "exec"), _g := {{"__name__": "_sandbox_guard"}})
    _g["install"](read_roots=_g["default_read_roots"]() + [{input_path!r}],
                  write_roots=[{output_dir!r}, {scratch_dir!r}])
    del _g

    # ---- user code ----
    {user_code}
    # ---- end user code ----

    if __name__ == "__main__":
        input_path = {input_path!r}
        output_dir = {output_dir!r}
        t0 = time.time()
        try:
            result_path = main(input_path, output_dir)
            elapsed = time.time() - t0
            print(json.dumps({{"time_taken": elapsed, "output_path": str(result_path), "error": None}}))
        except Exception as e:
            elapsed = time.time() - t0
            print(json.dumps({{"time_taken": elapsed, "output_path": None, "error": _tb.format_exc()}}))
            sys.exit(1)
    """
)

# ---------------------------------------------------------------------------
# SubprocessExecutor
# ---------------------------------------------------------------------------


class SubprocessExecutor(Executor):
    """MVP sandbox executor using subprocess isolation.

    Security model:
    - AST check blocks forbidden imports and builtins before any process is spawned.
    - Child process runs with a stripped environment.
    - Timeout enforced via subprocess.run(timeout=...).
    - Resource limits applied via preexec_fn (POSIX only).
    - Per-run scratch dir in volumes/tmp_exec/ is cleaned up after each run.

    Sockets are denied by the runtime guard; that is still not a hard
    boundary. Use PHOTON_SANDBOX=remote for one (backend.py).
    """

    def execute(
        self,
        code: str,
        input_path: str,
        output_dir: str,
        timeout: int,
    ) -> ExecutionResult:
        # Step 1: AST safety check
        try:
            _check_ast_safety(code)
        except SyntaxError as e:
            return ExecutionResult(
                stdout="",
                stderr=f"SyntaxError: {e}",
                traceback=str(e),
                time_taken_seconds=0.0,
                file_exists=False,
                output_path=None,
                timed_out=False,
            )
        except ASTSecurityError as e:
            return ExecutionResult(
                stdout="",
                stderr=f"Blocked by sandbox policy: {e}",
                traceback=None,
                time_taken_seconds=0.0,
                file_exists=False,
                output_path=None,
                timed_out=False,
            )

        # Step 2: Create per-run scratch directory
        run_id = uuid.uuid4().hex
        scratch_dir = config.TMP_EXEC_DIR / run_id
        scratch_dir.mkdir(parents=True, exist_ok=True)

        # Ensure output_dir exists
        Path(output_dir).mkdir(parents=True, exist_ok=True)

        script_path = scratch_dir / "script.py"
        harness = _HARNESS_TEMPLATE.format(
            user_code=code,
            input_path=input_path,
            output_dir=output_dir,
            scratch_dir=str(scratch_dir),
            guard_source=_GUARD_SOURCE,
        )
        script_path.write_text(harness, encoding="utf-8")

        # Step 3: Build restricted environment. Native thread pools are capped
        # via env vars (limits.py) before the child imports numpy/cv2/torch.
        child_env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": "",
            "HOME": str(scratch_dir),
            "TMPDIR": str(scratch_dir),
            "TEMP": str(scratch_dir),
            "TMP": str(scratch_dir),
            **limits.thread_env(),
            "PHOTON_NO_DOTENV": "1",
        }

        # Step 4: Resource limits (POSIX only)
        preexec_fn = limits.make_preexec(timeout)

        # Step 5: Run the script
        t_start = time.time()
        timed_out = False
        proc_stdout = ""
        proc_stderr = ""

        try:
            result = subprocess.run(
                [sys.executable, str(script_path)],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(scratch_dir),
                env=child_env,
                preexec_fn=preexec_fn,
            )
            proc_stdout = result.stdout
            proc_stderr = result.stderr
            if result.returncode != 0:
                if result.returncode < 0:
                    import signal
                    sig_num = -result.returncode
                    try:
                        sig_name = signal.Signals(sig_num).name
                    except ValueError:
                        sig_name = f"Signal {sig_num}"
                    proc_stderr += f"\n[Process killed by {sig_name}]"
                else:
                    if not proc_stderr.strip():
                        proc_stderr += f"\n[Process exited with code {result.returncode}]"
        except subprocess.TimeoutExpired as e:
            timed_out = True
            proc_stdout = e.stdout.decode("utf-8", errors="replace") if e.stdout else ""
            proc_stderr = e.stderr.decode("utf-8", errors="replace") if e.stderr else ""
            proc_stderr += "\n[Execution timed out]"
        except Exception as e:
            proc_stderr = f"Executor error: {e}"

        elapsed = time.time() - t_start

        # Step 6: Parse harness JSON from stdout
        output_path: Optional[str] = None
        harness_traceback: Optional[str] = None

        for line in reversed(proc_stdout.strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    output_path = data.get("output_path")
                    if data.get("error"):
                        harness_traceback = data["error"]
                    # Remove harness JSON line from displayed stdout
                    proc_stdout = proc_stdout.replace(line, "").strip()
                    break
                except json.JSONDecodeError:
                    pass

        # Step 7: Verify output file exists
        file_exists = False
        if output_path and Path(output_path).is_file():
            file_exists = True

        # Step 8: Cleanup scratch dir
        try:
            shutil.rmtree(scratch_dir, ignore_errors=True)
        except Exception:
            pass

        return ExecutionResult(
            stdout=proc_stdout,
            stderr=proc_stderr,
            traceback=harness_traceback,
            time_taken_seconds=elapsed,
            file_exists=file_exists,
            output_path=output_path if file_exists else None,
            timed_out=timed_out,
        )
