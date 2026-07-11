"""Subprocess-based sandbox executor (MVP implementation).

Runs user/LLM code in a child process with:
- AST import/builtin safety check (pre-execution, no subprocess launched if blocked)
- Restricted environment variables
- subprocess timeout
- Resource limits via resource.setrlimit on POSIX; best-effort on Windows
- Per-run scratch directory (auto-cleaned after output copy)

TODO(spec): Network isolation is advisory only (import blocklist) at this level.
For true network isolation, switch to DockerExecutor (docker_executor.py stub).
"""

from __future__ import annotations

import ast
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from textwrap import dedent
from typing import Optional

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
    }
)

# Dangerous builtins
_BLOCKED_BUILTINS: frozenset[str] = frozenset(
    {"eval", "exec", "__import__", "compile", "open", "breakpoint"}
)

# Allowed top-level module names (whitelist — anything else is blocked)
_ALLOWED_IMPORT_TOPS: frozenset[str] = frozenset(
    {
        "numpy",
        "np",
        "PIL",
        "cv2",
        "scipy",
        "skimage",
        "tifffile",
        "os",
        "pathlib",
        "math",
        "json",
        "re",
        "struct",
        "io",
        "typing",
        "dataclasses",
        "functools",
        "itertools",
        "collections",
        "copy",
        "warnings",
        "traceback",
        "time",
        "datetime",
        "enum",
        "abc",
        "contextlib",
    }
)


class ASTSecurityError(Exception):
    """Raised when AST check rejects the submitted code."""


def _check_ast_safety(code: str) -> None:
    """Parse code and raise ASTSecurityError if any forbidden construct is found.

    Checks:
    1. Blocked builtins (eval, exec, __import__, compile, open, breakpoint)
    2. Import of blocked modules
    3. Import of modules not in the allowlist

    Raises:
        ASTSecurityError: with a descriptive message if any check fails.
        SyntaxError: if the code cannot be parsed.
    """
    tree = ast.parse(code, mode="exec")

    for node in ast.walk(tree):
        # Check function/attribute calls for dangerous builtins
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _BLOCKED_BUILTINS:
                raise ASTSecurityError(
                    f"Use of '{node.func.id}' is blocked by sandbox policy."
                )
            if isinstance(node.func, ast.Attribute):
                # e.g. os.system, os.popen
                attr = node.func.attr
                if attr in {"system", "popen", "execv", "execvp", "execve", "execvpe", "exec"}:
                    raise ASTSecurityError(
                        f"Call to '{attr}' is blocked by sandbox policy."
                    )

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

    TODO(spec): True network isolation requires DockerExecutor. This executor
    relies on the import blocklist to prevent network calls — not a hard boundary.
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
        )
        script_path.write_text(harness, encoding="utf-8")

        # Step 3: Build restricted environment
        child_env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": "",
            "HOME": str(scratch_dir),
            "TMPDIR": str(scratch_dir),
            "TEMP": str(scratch_dir),
            "TMP": str(scratch_dir),
        }

        # Step 4: Resource limits (POSIX only)
        preexec_fn = None
        if platform.system() != "Windows":
            import resource

            def _set_limits():
                # CPU time: 2× timeout (soft) / 3× (hard)
                resource.setrlimit(
                    resource.RLIMIT_CPU,
                    (timeout * 2, timeout * 3),
                )
                # Address space: 2 GB
                try:
                    resource.setrlimit(
                        resource.RLIMIT_AS,
                        (2 * 1024**3, 2 * 1024**3),
                    )
                except (ValueError, resource.error):
                    pass
                # No core dumps
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

            preexec_fn = _set_limits

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
