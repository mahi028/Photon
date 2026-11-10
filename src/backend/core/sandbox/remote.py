"""Client side of the remote sandbox (D-038): ships a job to a sandbox
worker (src/sandbox_worker) and unpacks its results locally.

Why: the in-process guard (guard.py) is a speed bump, not a boundary. The
worker runs the *same* local executors, but inside a gVisor (runsc) container
on another host or VM, with no secrets, no volumes/ mount and no route to the
app. Selected with PHOTON_SANDBOX=remote (see backend.py); callers don't
change.

Wire protocol (HTTP, bearer token PHOTON_SANDBOX_TOKEN):

  POST /v1/run-code       multipart: job=<json {code, timeout}>, input=<file>
  POST /v1/run-pipeline   multipart: job=<json {graph, custom_nodes, timeout,
                                     input_mode}>, input=<file>
  GET  /v1/health

  200 response: application/zip with
      result.json  the executor's result, every path RELATIVE to out/ or nodes/
      out/...      files the job wrote to its output dir
      nodes/...    per-node previews (pipelines only)

The client checks every zip entry (no absolute paths, no "..", no links, size
caps) before extracting into the caller's output_dir / node_preview_dir and
rewrites result paths back to absolute local ones.
"""

from __future__ import annotations

import io
import json
import os
import stat
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Optional

import requests

from ...config import config
from ...models.dto import ExecutionResult
from .base import Executor

MAX_RESULT_BYTES = int(os.getenv("PHOTON_SANDBOX_MAX_RESULT_MB", "512")) * 1024 * 1024
MAX_FILES = 2000


class RemoteSandboxError(Exception):
    pass


def _settings() -> tuple[str, str]:
    url = os.getenv("PHOTON_SANDBOX_URL", "").rstrip("/")
    token = os.getenv("PHOTON_SANDBOX_TOKEN", "")
    if not url or not token:
        raise RemoteSandboxError("PHOTON_SANDBOX=remote needs PHOTON_SANDBOX_URL and PHOTON_SANDBOX_TOKEN.")
    return url, token


def _post(endpoint: str, job: dict, input_path: str, timeout: int,
          extra_files: dict[str, str] | None = None) -> bytes:
    """extra_files: multipart field -> local path (staged assets, D-044)."""
    url, token = _settings()
    handles = []
    try:
        f = open(input_path, "rb")
        handles.append(f)
        files = {"input": (Path(input_path).name, f)}
        for field, path in (extra_files or {}).items():
            h = open(path, "rb")
            handles.append(h)
            files[field] = (Path(path).name, h)
        r = requests.post(
            f"{url}{endpoint}",
            headers={"Authorization": f"Bearer {token}"},
            data={"job": json.dumps(job)},
            files=files,
            # worker enforces the job timeout itself; allow transfer overhead
            timeout=(10, timeout + 60),
            stream=True,
        )
    finally:
        for h in handles:
            h.close()
    if r.status_code != 200:
        raise RemoteSandboxError(f"sandbox worker HTTP {r.status_code}: {r.text[:300]}")
    body = io.BytesIO()
    for chunk in r.iter_content(1 << 16):
        body.write(chunk)
        if body.tell() > MAX_RESULT_BYTES:
            raise RemoteSandboxError("sandbox result exceeds PHOTON_SANDBOX_MAX_RESULT_MB")
    return body.getvalue()


def safe_extract(data: bytes, targets: dict[str, Path]) -> dict:
    """Extract a worker zip. `targets` maps a top-level folder in the zip
    ('out', 'nodes') to a local directory. Returns the parsed result.json.
    Anything else in the zip, or any suspicious entry, is an error."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise RemoteSandboxError(f"bad result archive: {e}")
    infos = zf.infolist()
    if len(infos) > MAX_FILES:
        raise RemoteSandboxError("too many files in sandbox result")
    if sum(i.file_size for i in infos) > MAX_RESULT_BYTES:
        raise RemoteSandboxError("sandbox result too large when unpacked")
    result = None
    for info in infos:
        name = PurePosixPath(info.filename)
        mode = (info.external_attr >> 16) & 0o170000
        if (name.is_absolute() or ".." in name.parts or "\\" in info.filename
                or (mode and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)))):
            raise RemoteSandboxError(f"unsafe path in sandbox result: {info.filename!r}")
        if info.is_dir():
            continue
        if str(name) == "result.json":
            result = json.loads(zf.read(info))
            continue
        top, rest = name.parts[0], name.parts[1:]
        if top not in targets or not rest:
            raise RemoteSandboxError(f"unexpected entry in sandbox result: {info.filename!r}")
        dest = targets[top].joinpath(*rest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(dest, "wb") as dst:
            dst.write(src.read())
    if result is None:
        raise RemoteSandboxError("sandbox result has no result.json")
    return result


def _local(rel: Optional[str], base: Path) -> Optional[str]:
    """Worker-relative path -> absolute local path (inside base only)."""
    if not rel:
        return None
    p = PurePosixPath(rel)
    if p.is_absolute() or ".." in p.parts:
        return None
    return str(base.joinpath(*p.parts))


class RemoteExecutor(Executor):
    """run-code jobs on the sandbox worker. Same contract as SubprocessExecutor."""

    def execute(self, code: str, input_path: str, output_dir: str, timeout: int) -> ExecutionResult:
        t0 = time.time()
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        try:
            data = _post("/v1/run-code", {"code": code, "timeout": timeout}, input_path, timeout)
            r = safe_extract(data, {"out": out})
        except (RemoteSandboxError, requests.RequestException) as e:
            return ExecutionResult(stdout="", stderr=f"Remote sandbox error: {e}", traceback=None,
                                   time_taken_seconds=time.time() - t0, file_exists=False,
                                   output_path=None, timed_out=False)
        output_path = _local(r.get("output_path"), out)
        exists = bool(output_path and Path(output_path).is_file())
        return ExecutionResult(stdout=r.get("stdout", ""), stderr=r.get("stderr", ""),
                               traceback=r.get("traceback"),
                               time_taken_seconds=float(r.get("time_taken_seconds") or time.time() - t0),
                               file_exists=exists, output_path=output_path if exists else None,
                               timed_out=bool(r.get("timed_out")))


def run_pipeline_remote(graph: dict, input_path: str, output_dir: str, node_preview_dir: str,
                        custom_nodes: dict | None = None, timeout: Optional[int] = None,
                        input_mode: str = "RGB", assets: dict[str, str] | None = None) -> dict[str, Any]:
    """Same signature and result shape as executor.run_pipeline_subprocess."""
    timeout = timeout or config.EXECUTION_TIMEOUT_SECONDS * 4
    t0 = time.time()
    out, nodes = Path(output_dir), Path(node_preview_dir)
    out.mkdir(parents=True, exist_ok=True)
    nodes.mkdir(parents=True, exist_ok=True)
    try:
        keys = sorted(assets or {})
        data = _post("/v1/run-pipeline", {"graph": graph, "custom_nodes": custom_nodes or {},
                                          "timeout": timeout, "input_mode": input_mode,
                                          "assets": {k: f"asset{i}" for i, k in enumerate(keys)}},
                     input_path, timeout,
                     extra_files={f"asset{i}": assets[k] for i, k in enumerate(keys)})
        r = safe_extract(data, {"out": out, "nodes": nodes})
    except (RemoteSandboxError, requests.RequestException) as e:
        return {"error": f"Remote sandbox error: {e}", "failed_node": None, "events": [],
                "previews": {}, "finals": [], "timed_out": False, "time_taken_seconds": time.time() - t0}
    r["previews"] = {nid: {**p, "path": _local(p.get("path"), nodes)}
                     for nid, p in (r.get("previews") or {}).items() if _local(p.get("path"), nodes)}
    r["finals"] = [{**f, "path": _local(f.get("path"), out)}
                   for f in r.get("finals") or [] if _local(f.get("path"), out)]
    r["saved_models"] = [{**m, "path": _local(m.get("path"), out)}
                         for m in r.get("saved_models") or [] if _local(m.get("path"), out)]
    return r
