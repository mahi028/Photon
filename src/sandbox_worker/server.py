"""Photon sandbox worker (D-038): runs untrusted jobs for a Photon app that
has PHOTON_SANDBOX=remote. Protocol: core/sandbox/remote.py.

Meant to run inside a gVisor (runsc) container (docker/sandbox-worker/):
no secrets, no access to the app's volumes/, egress blocked by the network
it sits on. Inside, each job still goes through the local executors (AST
check, rlimits, runtime guard), so a sandbox escape has to beat both layers
and then lands in an empty container.

    python -m src.sandbox_worker.server --host 0.0.0.0 --port 8891

Env:
  PHOTON_SANDBOX_TOKEN         required; shared with the app
  PHOTON_SANDBOX_WORKER_SLOTS  concurrent jobs (default 2); extra requests get 503
  PHOTON_VOLUMES_DIR           worker scratch (default: a temp dir)
  MODEL_SIDECAR_ENABLED        model ops run on this worker's own sidecar/GPU
"""

from __future__ import annotations

import argparse
import hmac
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

# Worker state never lives next to the app's: default to a private temp dir.
os.environ.setdefault("PHOTON_VOLUMES_DIR", tempfile.mkdtemp(prefix="photon-worker-"))
os.environ["PHOTON_NO_DOTENV"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from flask import Flask, Response, jsonify, request  # noqa: E402

from src.backend.config import config  # noqa: E402

MAX_JOB_BYTES = int(os.getenv("PHOTON_SANDBOX_MAX_INPUT_MB", str(config.MAX_UPLOAD_MB))) * 1024 * 1024
_slots = threading.BoundedSemaphore(int(os.getenv("PHOTON_SANDBOX_WORKER_SLOTS", "2")))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_JOB_BYTES + (16 << 20)


def _authorized() -> bool:
    token = os.getenv("PHOTON_SANDBOX_TOKEN", "")
    got = request.headers.get("Authorization", "")
    return bool(token) and hmac.compare_digest(got.encode(), f"Bearer {token}".encode())


def _zip_result(result: dict, dirs: dict[str, Path]) -> Response:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("result.json", json.dumps(result))
        for top, d in dirs.items():
            for p in sorted(d.rglob("*")):
                if p.is_file() and not p.is_symlink():
                    zf.write(p, f"{top}/{p.relative_to(d).as_posix()}")
    return Response(buf.getvalue(), mimetype="application/zip")


def _rel(path, base: Path):
    if not path:
        return None
    try:
        return Path(path).resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return None


class _Job:
    """Per-request scratch: job.json, the input file, out/ and nodes/."""

    def __enter__(self):
        if "job" not in request.form or "input" not in request.files:
            raise ValueError("need multipart fields 'job' and 'input'")
        self.job = json.loads(request.form["job"])
        self.dir = Path(tempfile.mkdtemp(prefix="job_", dir=config.TMP_EXEC_DIR))
        name = Path(request.files["input"].filename or "input.png").name
        self.input = self.dir / ("input" + Path(name).suffix.lower())
        request.files["input"].save(self.input)
        self.out = self.dir / "out"
        self.nodes = self.dir / "nodes"
        self.out.mkdir()
        self.nodes.mkdir()
        self.timeout = max(1, min(int(self.job.get("timeout") or 60), 3600))
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.dir, ignore_errors=True)


def _guarded(fn):
    def wrapper():
        if not _authorized():
            return jsonify({"error": "unauthorized"}), 401
        if not _slots.acquire(blocking=False):
            return jsonify({"error": "worker busy"}), 503
        try:
            with _Job() as job:
                return fn(job)
        except (ValueError, json.JSONDecodeError) as e:
            return jsonify({"error": str(e)}), 400
        finally:
            _slots.release()
    wrapper.__name__ = fn.__name__
    return wrapper


@app.get("/v1/health")
def health():
    gpu = None
    try:
        import torch
        gpu = torch.cuda.is_available()
    except Exception:
        pass
    return jsonify({"ok": True, "gpu": gpu})


@app.post("/v1/run-code")
@_guarded
def run_code(job: _Job):
    from src.backend.core.sandbox.subprocess_executor import SubprocessExecutor
    r = SubprocessExecutor().execute(str(job.job.get("code", "")), str(job.input), str(job.out), job.timeout)
    d = r.to_dict()
    d["output_path"] = _rel(r.output_path, job.out)
    return _zip_result(d, {"out": job.out})


@app.post("/v1/run-pipeline")
@_guarded
def run_pipeline(job: _Job):
    from src.backend.core.pipeline.executor import run_pipeline_subprocess
    r = run_pipeline_subprocess(job.job.get("graph") or {}, str(job.input), str(job.out), str(job.nodes),
                                custom_nodes=job.job.get("custom_nodes") or {}, timeout=job.timeout,
                                input_mode=job.job.get("input_mode") or "RGB")
    r["previews"] = {nid: {**p, "path": _rel(p.get("path"), job.nodes)}
                     for nid, p in (r.get("previews") or {}).items()}
    r["finals"] = [{**f, "path": _rel(f.get("path"), job.out)} for f in r.get("finals") or []]
    return _zip_result(r, {"out": job.out, "nodes": job.nodes})


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m src.sandbox_worker.server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8891)
    args = ap.parse_args(argv)
    if not os.getenv("PHOTON_SANDBOX_TOKEN"):
        sys.exit("PHOTON_SANDBOX_TOKEN is required.")
    from waitress import serve
    serve(app, host=args.host, port=args.port, threads=8)


if __name__ == "__main__":
    main()
