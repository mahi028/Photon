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
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from ...config import config
from ..sandbox import limits

_CHILD = r'''
import json, sys, traceback
from pathlib import Path
import numpy as np

sys.path.insert(0, JOB["repo_root"])
from src.backend.core.pipeline import runner  # noqa: E402
from src.backend.core.pipeline.types import PortType, infer_type  # noqa: E402
from src.backend.core.sandbox import guard  # noqa: E402
from PIL import Image  # noqa: E402

# Read the input while the process is still unrestricted-by-hook, then lock
# down before any custom node code is compiled or run (D-036).
_INPUT = np.asarray(Image.open(JOB["input_path"]).convert(JOB["input_mode"]))
import src.backend.core.models.sidecar  # noqa: E402,F401  (model ops import it lazily)
guard.install(read_roots=guard.default_read_roots() + JOB["read_roots"],
              write_roots=JOB["write_roots"], unix_sockets=JOB["unix_sockets"])

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
    graph = JOB["graph"]
    custom_nodes = JOB["custom_nodes"]
    node_dir = Path(JOB["node_preview_dir"])
    node_dir.mkdir(parents=True, exist_ok=True)

    img = _INPUT

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


def _uses_model_ops(graph: dict) -> bool:
    from . import registry
    for n in graph.get("nodes") or []:
        if n.get("op"):
            try:
                if registry.get_op(n["op"]).requires_model:
                    return True
            except Exception:
                pass
    return False


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
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    Path(node_preview_dir).mkdir(parents=True, exist_ok=True)

    # Model ops reach the sidecar over its Unix socket. Start it here, in the
    # unrestricted parent: the guarded child may connect but never spawn.
    unix_sockets: list[str] = []
    if _uses_model_ops(graph) and config.MODEL_SIDECAR_ENABLED:
        from ..models import sidecar
        try:
            sidecar.ensure_running()
        except Exception:
            pass                       # child reports the failure on the node
        unix_sockets.append(str(config.MODEL_SIDECAR_SOCKET))
        config.MODEL_IPC_DIR.mkdir(parents=True, exist_ok=True)

    job = {
        "repo_root": repo_root,
        "graph": graph,
        "custom_nodes": custom_nodes or {},
        "input_path": input_path,
        "input_mode": input_mode,
        "output_dir": output_dir,
        "node_preview_dir": node_preview_dir,
        "result_path": str(result_path),
        # Sandbox guard (core/sandbox/guard.py): reads beyond Python's own
        # install are limited to the repo's src/ (lazy op imports); writes to
        # this run's dirs. .env, sessions.db, other users' files: unreadable.
        "read_roots": [str(config.ROOT_DIR / "src")],
        "write_roots": [str(scratch), output_dir, node_preview_dir]
                       + ([str(config.MODEL_IPC_DIR)] if unix_sockets else []),
        "unix_sockets": unix_sockets,
    }
    script = scratch / "run.py"
    # JSON is not Python (true/false/null), so embed it as a string literal
    # and parse it in the child.
    script.write_text(f"import json as _json\nJOB = _json.loads({json.dumps(job)!r})\n{_CHILD}",
                      encoding="utf-8")

    child_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(scratch),
        "TMPDIR": str(scratch),
        **limits.thread_env(),
        "PHOTON_NO_DOTENV": "1",
        "MODEL_SIDECAR_ENABLED": "1" if config.MODEL_SIDECAR_ENABLED else "0",
    }
    preexec_fn = limits.make_preexec(timeout)

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
        return {"error": f"Pipeline process produced no result. stderr: {stderr[-1500:]}",
                "failed_node": None, "events": [], "previews": {}, "finals": [],
                "timed_out": False, "time_taken_seconds": elapsed}

    data = json.loads(result_path.read_text())
    data["timed_out"] = False
    data["time_taken_seconds"] = elapsed
    return data
