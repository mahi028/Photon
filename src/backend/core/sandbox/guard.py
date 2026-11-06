"""Runtime guard for sandbox child processes (PEP 578 audit hook).

The AST check (subprocess_executor._check_ast_safety) rejects obviously bad
code at save/submit time, but static checks on Python are always bypassable
(getattr with computed names, pickle, C extensions...). This hook is the
second layer: it sees the *operations* the interpreter actually performs and
denies the dangerous ones, however the code got there.

Installed in both sandbox children (run-code harness, pipeline child) after
trusted imports and before any user/custom-node code runs:

  * no new processes (subprocess, os.system/exec*/spawn*/posix_spawn/fork),
    no signals to other processes;
  * no sockets except AF_UNIX connects to an explicit allow-list (the model
    sidecar), no ctypes, no unpickling (pickle.find_class);
  * file opens: writes only under `write_roots`; reads only under
    `read_roots` + `write_roots` (Python's own install, the repo's src/, the
    input image...). So .env, sessions.db and other users' uploads/outputs
    are unreadable. Paths are realpath'd, so symlink tricks resolve to their
    targets; filesystem mutations (rename, remove, link, chmod...) need every
    path inside write_roots;
  * os.environ is wiped down to a small allow-list at install time.

NOT a security boundary on its own (PEP 578 says as much): C extensions can
do I/O without raising audit events (e.g. cv2.imread is plain fopen), and a
determined attacker with arbitrary code may find an interpreter escape.
That is what the container/VM executor (Phase 2) is for. This module must
stay stdlib-only and self-contained: the run-code harness inlines its source.
"""

import os
import sys

_SAFE_ENV = ("PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL",
             "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
             "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS", "PHOTON_NO_DOTENV")

_DENY_PREFIXES = (
    "subprocess.", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.fork",
    "os.forkpty", "os.kill", "os.killpg", "os.startfile", "pty.", "ctypes.",
    "pickle.find_class", "webbrowser.", "socket.bind", "socket.sendto", "socket.sendmsg",
    "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr",
    "os.setuid", "os.setgid",
)

# events whose args are (path...) that MUTATE the filesystem: every path arg
# (strings/bytes/PathLike) must be inside write_roots.
_MUTATE_EVENTS = frozenset({
    "os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.chmod", "os.chown",
    "os.symlink", "os.link", "os.truncate", "os.utime", "os.chflags", "os.lchflags",
    "os.setxattr", "os.removexattr", "shutil.rmtree", "shutil.copyfile",
    "shutil.copymode", "shutil.copystat", "shutil.copytree", "shutil.move",
    "shutil.chown", "shutil.make_archive", "shutil.unpack_archive",
})

# events that only READ/inspect a path: path must be readable.
_READ_EVENTS = frozenset({"os.listdir", "os.scandir", "os.chdir", "glob.glob",
                          "glob.glob/2", "os.listxattr", "os.getxattr"})


def install(read_roots, write_roots, unix_sockets=()):
    """Install the hook. Irreversible for the life of the process."""
    realpath = os.path.realpath
    sep = os.sep

    def _norm(roots):
        out = []
        for r in roots:
            if r:
                rp = realpath(os.fspath(r))
                out.append(rp)
        return tuple(out)

    W = _norm(write_roots)
    R = _norm(read_roots) + W
    SOCKS = frozenset(_norm(unix_sockets))
    unix_family = getattr(__import__("socket"), "AF_UNIX", None) if unix_sockets else None
    deny_prefixes = _DENY_PREFIXES
    mutate_events = _MUTATE_EVENTS
    read_events = _READ_EVENTS
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC

    def _inside(path, roots):
        try:
            p = realpath(os.fsdecode(os.fspath(path)))
        except (TypeError, ValueError):
            return False
        for r in roots:
            if p == r or p.startswith(r + sep) or r == sep:
                return True
        return False

    def _deny(event, detail=""):
        raise PermissionError(f"Blocked by sandbox: {event}{' ' + str(detail) if detail else ''}")

    def hook(event, args):
        if event == "open":
            path, mode, flags = (tuple(args) + (None, None, None))[:3]
            if path is None or isinstance(path, int):
                return                                  # already-open fd
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or \
                      (isinstance(flags, int) and flags & write_flags)
            if not _inside(path, W if writing else R):
                _deny("write to" if writing else "read of", path)
            return
        if event in mutate_events:
            for a in args:
                if isinstance(a, (str, bytes, os.PathLike)) and not _inside(a, W):
                    _deny(event, a)
            return
        if event in read_events:
            for a in args[:1]:
                if isinstance(a, (str, bytes, os.PathLike)) and not _inside(a, R):
                    _deny(event, a)
            return
        if event == "sys.addaudithook":
            # RuntimeError (not PermissionError) is what makes CPython refuse
            # the new hook *silently* (hooks can only stack, never unhook ours).
            raise RuntimeError("Blocked by sandbox: sys.addaudithook")
        if event == "socket.__new__":
            family = args[1] if len(args) > 1 else None
            if unix_family is None or family != unix_family:
                _deny(event, f"family {family}")
            return
        if event == "socket.connect":
            addr = args[1] if len(args) > 1 else None
            if not (isinstance(addr, (str, bytes)) and realpath(os.fsdecode(addr)) in SOCKS):
                _deny(event, addr)
            return
        for p in deny_prefixes:
            if event.startswith(p):
                _deny(event)

    for k in list(os.environ):
        if k not in _SAFE_ENV:
            del os.environ[k]
    sys.addaudithook(hook)


def default_read_roots():
    """Python's own install dirs: stdlib + site-packages (lazy imports read
    .py/.pyi/data files from here), plus the few device/proc files libraries
    commonly touch."""
    import site
    roots = {sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix}
    try:
        roots.update(site.getsitepackages())
    except Exception:
        pass
    roots.update({"/dev/null", "/dev/urandom", "/proc/self", "/sys/devices/system/cpu",
                  "/etc/localtime", "/usr/share/zoneinfo"})
    return sorted(roots)


def source() -> str:
    """This module's source, for children that can't import the repo."""
    with open(__file__, encoding="utf-8") as f:
        return f.read()
