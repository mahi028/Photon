"""Model sidecar server — the long-lived process that owns GPU models.

Why it exists: pipeline ops run in a throwaway subprocess per run, and
production runs gunicorn with multiple workers. Neither can hold a model
across requests, so a "keep warm for 5 minutes then unload" policy is
impossible without a process that outlives both. This is that process, and
there is exactly one of it per host (see sidecar.ensure_running()).

Protocol: Unix socket, one request per connection, newline-delimited JSON.
Arrays never cross the socket — the client writes inputs as .npy into a shared
scratch dir and the server writes outputs back the same way, so only paths are
serialized.

Requests:
    {"cmd": "ping"}
    {"cmd": "status"}
    {"cmd": "run_op", "op": "<op name>", "params": {...},
     "inputs": {"<port>": "<path.npy>"}, "out_dir": "<dir>"}
    {"cmd": "unload_all"}
    {"cmd": "shutdown"}

Run with:  python -m src.backend.core.models.sidecar_server
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import time
import traceback
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [sidecar] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def _handle(request: dict) -> dict:
    """Dispatch one request. Imports stay lazy so `ping` is cheap."""
    from ...config import config
    from ..models.manager import ModelLoadError, get_manager

    cmd = request.get("cmd")

    if cmd == "ping":
        return {"ok": True, "pid": os.getpid()}

    if cmd == "status":
        return {"ok": True, "status": get_manager().status()}

    if cmd == "unload_all":
        return {"ok": True, "unloaded": get_manager().unload_all(force=bool(request.get("force")))}

    if cmd == "run_op":
        import numpy as np
        from ..pipeline import registry

        op_name = request["op"]
        spec = registry.get_op(op_name)
        if spec is None:
            return {"ok": False, "error": f"Unknown op '{op_name}'"}
        if not spec.requires_model:
            return {"ok": False, "error": f"Op '{op_name}' is not a model op"}

        kwargs = {port: np.load(path, allow_pickle=False)
                  for port, path in (request.get("inputs") or {}).items()}
        kwargs.update(request.get("params") or {})

        manager = get_manager()
        try:
            # acquire() holds a reference for the whole call, so the idle
            # reaper can never unload the model mid-inference.
            with manager.acquire(spec.requires_model) as model:
                outputs = spec.fn(model=model, **kwargs)
        except ModelLoadError as e:
            return {"ok": False, "error": str(e)}

        out_dir = Path(request["out_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = {}
        for port, value in outputs.items():
            p = out_dir / f"{op_name}_{port}_{os.getpid()}_{time.time_ns()}.npy"
            np.save(p, np.asarray(value), allow_pickle=False)
            paths[port] = str(p)
        return {"ok": True, "outputs": paths}

    return {"ok": False, "error": f"Unknown command '{cmd}'"}


def _serve_connection(conn: socket.socket, last_activity: list) -> None:
    try:
        conn.settimeout(600)
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                return
            buf += chunk
        request = json.loads(buf.decode("utf-8"))
        last_activity[0] = time.monotonic()

        if request.get("cmd") == "shutdown":
            conn.sendall(json.dumps({"ok": True, "shutting_down": True}).encode() + b"\n")
            os._exit(0)

        try:
            response = _handle(request)
        except Exception as e:
            logger.exception("Request failed")
            response = {"ok": False, "error": f"{type(e).__name__}: {e}",
                        "traceback": traceback.format_exc(limit=4)}
        conn.sendall(json.dumps(response).encode() + b"\n")
        last_activity[0] = time.monotonic()
    except Exception:
        logger.exception("Connection handling failed")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def serve(socket_path: Path) -> None:
    from ...config import config

    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        socket_path.unlink()

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    server.listen(16)
    os.chmod(socket_path, 0o600)
    logger.info("Listening on %s (pid %d)", socket_path, os.getpid())

    last_activity = [time.monotonic()]

    # Exit when nothing has been loaded or requested for a long time, so an
    # idle host isn't left holding a Python+CUDA process forever.
    idle_exit = config.MODEL_SIDECAR_IDLE_EXIT_SECONDS
    if idle_exit:
        def _idle_watch():
            from ..models.manager import get_manager
            while True:
                time.sleep(30)
                quiet = time.monotonic() - last_activity[0]
                if quiet > idle_exit and not get_manager().status()["loaded"]:
                    logger.info("Idle %.0fs with no models loaded — exiting", quiet)
                    os._exit(0)
        threading.Thread(target=_idle_watch, name="sidecar-idle-exit", daemon=True).start()

    # Warm the manager so its reaper thread starts ticking immediately.
    from ..models.manager import get_manager
    get_manager()

    try:
        while True:
            conn, _ = server.accept()
            threading.Thread(target=_serve_connection, args=(conn, last_activity),
                             daemon=True).start()
    finally:
        try:
            socket_path.unlink()
        except Exception:
            pass


def main() -> None:
    # Allow `python -m src.backend.core.models.sidecar_server` from the repo root.
    from ...config import config
    serve(Path(config.MODEL_SIDECAR_SOCKET))


if __name__ == "__main__":
    sys.exit(main())
