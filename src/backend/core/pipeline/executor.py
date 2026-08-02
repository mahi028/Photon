"""Run a pipeline graph in a resource-limited subprocess.

Why a subprocess: registry ops are trusted code, but a bad param (huge kernel,
giant upscale) or an LLM-authored custom node can still hang or exhaust RAM.
Reuses the same containment posture as the code sandbox (D-013 BLAS caps,
rlimits, timeout) but deliberately WITHOUT the import whitelist — registry ops
legitimately import skimage/scipy/cv2. Custom-node safety comes from the AST
check applied when the node is saved.

Protocol: parent writes a JSON job file, child writes per-node preview PNGs +
a JSON result manifest, parent reads it back. Arrays never cross the process
boundary except as saved images.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from ...config import config

_CHILD = r'''
import json, sys, traceback
from pathlib import Path
import numpy as np

sys.path.insert(0, JOB["repo_root"])
from src.backend.core.pipeline import runner  # noqa: E402
from src.backend.core.pipeline.types import PortType, infer_type  # noqa: E402

def _to_preview_array(value):
    """Best-effort conversion of any port value to a saveable uint8 image."""
    arr = np.asarray(value)
    if arr.dtype == bool:
        return (arr.astype(np.uint8) * 255)
    if arr.ndim == 3 and arr.shape[2] not in (3, 4):
        arr = arr.mean(axis=2)          # collapse feature maps for preview
    if np.issubdtype(arr.dtype, np.floating):
        lo, hi = float(np.nanmin(arr)), float(np.nanmax(arr))
        if hi - lo < 1e-12:
            return np.zeros(arr.shape, dtype=np.uint8)
        arr = (arr - lo) / (hi - lo) * 255.0
    return np.clip(arr, 0, 255).astype(np.uint8)

def main():
    from PIL import Image
    graph = JOB["graph"]
    custom_nodes = JOB["custom_nodes"]
    node_dir = Path(JOB["node_preview_dir"])
    node_dir.mkdir(parents=True, exist_ok=True)

    img = np.asarray(Image.open(JOB["input_path"]).convert(JOB["input_mode"]))

    events = []
    previews = {}

    def on_node(nid, phase, payload):
        events.append({"node_id": nid, "phase": phase,
                       "message": payload.get("message")})
        if phase == "done":
            outs = payload.get("outputs") or {}
            if not outs:
                return
            port, value = next(iter(outs.items()))
            try:
                p = node_dir / f"{nid}.jpg"
                Image.fromarray(_to_preview_array(value)).convert("RGB").save(p, quality=85)
                previews[nid] = {"port": port, "path": str(p)}
            except Exception:
                pass   # a preview failing must never fail the run

    result = runner.run(graph, img, custom_nodes=custom_nodes, on_node=on_node)

    finals = []
    for nid in result["terminal"]:
        outs = result["results"].get(nid) or {}
        for port, value in outs.items():
            out_path = Path(JOB["output_dir"]) / f"{nid}_{port}.png"
            Path(JOB["output_dir"]).mkdir(parents=True, exist_ok=True)
            Image.fromarray(_to_preview_array(value)).save(out_path)
            finals.append({"node_id": nid, "port": port, "path": str(out_path)})

    json.dump({
        "error": result["error"],
        "failed_node": result["failed_node"],
        "events": events,
        "previews": previews,
        "finals": finals,
    }, open(JOB["result_path"], "w"))

try:
    main()
except Exception:
    json.dump({"error": traceback.format_exc(limit=3), "failed_node": None,
               "events": [], "previews": {}, "finals": []},
              open(JOB["result_path"], "w"))
'''


def run_pipeline_subprocess(
    graph: dict,
    input_path: str,
    output_dir: str,
    node_preview_dir: str,
    custom_nodes: dict[str, dict] | None = None,
    timeout: Optional[int] = None,
    input_mode: str = "RGB",
) -> dict[str, Any]:
    """Execute a graph out-of-process. Returns the child's result manifest
    plus wall-clock timing, or a synthetic error dict on timeout/crash."""
    timeout = timeout or config.EXECUTION_TIMEOUT_SECONDS * 4  # pipelines are multi-op
    repo_root = str(config.ROOT_DIR)

    scratch = Path(tempfile.mkdtemp(prefix="pipe_", dir=str(config.TMP_EXEC_DIR)))
    result_path = scratch / "result.json"
    job = {
        "repo_root": repo_root,
        "graph": graph,
        "custom_nodes": custom_nodes or {},
        "input_path": input_path,
        "input_mode": input_mode,
        "output_dir": output_dir,
        "node_preview_dir": node_preview_dir,
        "result_path": str(result_path),
    }
    script = scratch / "run.py"
    script.write_text(f"JOB = {json.dumps(job)}\n{_CHILD}", encoding="utf-8")

    blas = str(max(1, config.SANDBOX_BLAS_THREADS))
    child_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(scratch),
        "TMPDIR": str(scratch),
        "OMP_NUM_THREADS": blas,
        "OPENBLAS_NUM_THREADS": blas,
        "MKL_NUM_THREADS": blas,
        "VECLIB_MAXIMUM_THREADS": blas,
        "NUMEXPR_NUM_THREADS": blas,
    }

    preexec_fn = None
    if platform.system() != "Windows":
        import resource

        def _set_limits():
            n = max(1, config.SANDBOX_BLAS_THREADS)
            resource.setrlimit(resource.RLIMIT_CPU, (timeout * 2 * n, timeout * 3 * n))
            try:
                lim = config.SANDBOX_MEMORY_LIMIT_GB * 1024 ** 3
                resource.setrlimit(resource.RLIMIT_AS, (lim, lim))
            except (ValueError, resource.error):
                pass
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

        preexec_fn = _set_limits

    t0 = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(scratch), env=child_env, preexec_fn=preexec_fn,
        )
        stderr = proc.stderr
    except subprocess.TimeoutExpired:
        return {"error": f"Pipeline timed out after {timeout}s.", "failed_node": None,
                "events": [], "previews": {}, "finals": [], "timed_out": True,
                "time_taken_seconds": time.time() - t0}
    except Exception as e:
        return {"error": f"Executor error: {e}", "failed_node": None, "events": [],
                "previews": {}, "finals": [], "timed_out": False,
                "time_taken_seconds": time.time() - t0}

    elapsed = time.time() - t0
    if not result_path.is_file():
        return {"error": f"Pipeline process produced no result. stderr: {stderr[:500]}",
                "failed_node": None, "events": [], "previews": {}, "finals": [],
                "timed_out": False, "time_taken_seconds": elapsed}

    data = json.loads(result_path.read_text())
    data["timed_out"] = False
    data["time_taken_seconds"] = elapsed
    return data
