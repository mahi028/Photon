"""Client for the model sidecar.

`run_model_op()` is the only path by which a model op executes. It lazily
spawns the sidecar on first use (so an app that never touches a model never
starts a CUDA process), then hands arrays over as .npy files.

Spawning is guarded by an O_EXCL lock file so concurrent gunicorn workers —
or concurrent pipeline children — can't race two sidecars into existence.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

import numpy as np

from ...config import config

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 5.0
_SPAWN_WAIT_SECONDS = 60.0    # first import of torch can be slow
_CALL_TIMEOUT = 600.0


class SidecarError(RuntimeError):
    pass


def _socket_path() -> Path:
    return Path(config.MODEL_SIDECAR_SOCKET)


def _try_connect(timeout: float = _CONNECT_TIMEOUT) -> Optional[socket.socket]:
    path = _socket_path()
    if not path.exists():
        return None
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(path))
        return s
    except (ConnectionRefusedError, FileNotFoundError, socket.timeout, OSError):
        s.close()
        return None


def is_running() -> bool:
    s = _try_connect(1.0)
    if s is None:
        return False
    s.close()
    return True


def ensure_running() -> None:
    """Start the sidecar if it isn't up. Safe to call from multiple processes."""
    if is_running():
        return
    if not config.MODEL_SIDECAR_ENABLED:
        raise SidecarError("Model sidecar is disabled (MODEL_SIDECAR_ENABLED=0)")

    lock_path = _socket_path().with_suffix(".lock")
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # Another process is spawning it — wait for the socket to appear. If
        # the lock is stale (spawner died), reclaim it.
        if time.time() - lock_path.stat().st_mtime > _SPAWN_WAIT_SECONDS:
            logger.warning("Stale sidecar lock; reclaiming")
            lock_path.unlink(missing_ok=True)
            return ensure_running()
        _wait_for_socket()
        return

    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)

        stale = _socket_path()
        if stale.exists():
            stale.unlink()   # left behind by a crashed sidecar

        log_path = config.VOLUMES_DIR / "model_sidecar.log"
        logger.info("Spawning model sidecar (log: %s)", log_path)
        with open(log_path, "ab") as log:
            subprocess.Popen(
                [sys.executable, "-m", "src.backend.core.models.sidecar_server"],
                cwd=str(config.ROOT_DIR),
                stdout=log, stderr=log,
                start_new_session=True,   # survives the request that spawned it
            )
        _wait_for_socket()
    finally:
        lock_path.unlink(missing_ok=True)


def _wait_for_socket(timeout: float = _SPAWN_WAIT_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_running():
            return
        time.sleep(0.25)
    raise SidecarError(
        f"Model sidecar did not come up within {timeout:.0f}s — see "
        f"{config.VOLUMES_DIR / 'model_sidecar.log'}")


def request(payload: dict, timeout: float = _CALL_TIMEOUT) -> dict:
    """Send one request, return the parsed response."""
    ensure_running()
    s = _try_connect()
    if s is None:
        raise SidecarError("Could not connect to the model sidecar")
    try:
        s.settimeout(timeout)
        s.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        if not buf:
            raise SidecarError("Sidecar closed the connection without replying")
        return json.loads(buf.decode("utf-8"))
    finally:
        s.close()


def status() -> dict:
    return request({"cmd": "status"}).get("status", {})


def unload_all(force: bool = False) -> int:
    return request({"cmd": "unload_all", "force": force}).get("unloaded", 0)


def shutdown() -> None:
    try:
        request({"cmd": "shutdown"}, timeout=5)
    except Exception:
        pass


def run_model_op(op_name: str, inputs: dict[str, np.ndarray],
                 params: dict[str, Any]) -> dict[str, np.ndarray]:
    """Execute a model op in the sidecar and return its output arrays.

    Arrays are handed over as .npy files in a shared scratch dir — they never
    go through the socket.
    """
    ipc_dir = Path(config.MODEL_IPC_DIR)
    ipc_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{os.getpid()}_{time.time_ns()}"
    written: list[Path] = []

    try:
        in_paths = {}
        for port, arr in inputs.items():
            p = ipc_dir / f"in_{op_name}_{port}_{stamp}.npy"
            np.save(p, np.asarray(arr), allow_pickle=False)
            written.append(p)
            in_paths[port] = str(p)

        resp = request({
            "cmd": "run_op", "op": op_name, "params": params,
            "inputs": in_paths, "out_dir": str(ipc_dir),
        })
        if not resp.get("ok"):
            raise SidecarError(resp.get("error", "model op failed"))

        outputs = {}
        for port, path in (resp.get("outputs") or {}).items():
            outputs[port] = np.load(path, allow_pickle=False)
            written.append(Path(path))
        return outputs
    finally:
        for p in written:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
