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
from src.backend.core.pipeline.ops import ml as _ml  # noqa: E402
_ml.RUNTIME["assets"] = JOB.get("assets") or {}       # staged saved models / labels (D-044)
_ml.RUNTIME["save_dir"] = str(Path(JOB["output_dir"]) / "models")
if any(n.get("op") in ("ml_save_model", "ml_load_model") for n in JOB["graph"].get("nodes") or []):
    import skops.io  # noqa: E402,F401  (loads native libs via ctypes, which the guard blocks)
    import sklearn.utils.validation  # noqa: E402,F401
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

def _is_image(v):
    return (isinstance(v, np.ndarray) and v.ndim in (2, 3) and min(v.shape[:2]) >= 2
            and v.dtype.kind in "biuf")

def _summary(v, depth=0):
    """JSON-safe, size-capped view of a non-image value (D-041)."""
    if v is None or isinstance(v, (bool, int, float)):
        return v
    if isinstance(v, str):
        return v[:300]
    if isinstance(v, np.generic):
        return v.item()
    if isinstance(v, np.ndarray):
        if v.size <= 32 and v.dtype.kind in "biuf":
            return np.round(v, 6).tolist() if v.dtype.kind == "f" else v.tolist()
        if v.dtype.kind in "biuf" and v.size:
            return f"array shape={list(v.shape)} dtype={v.dtype} min={np.nanmin(v):.6g} max={np.nanmax(v):.6g}"
        return f"array shape={list(v.shape)} dtype={v.dtype}"
    if hasattr(v, "get_params") and hasattr(v, "fit"):          # sklearn model (D-042)
        try:
            from sklearn.utils.validation import check_is_fitted
            check_is_fitted(v)
            fitted = "fitted"
        except Exception:
            fitted = "not fitted"
        return f"<{type(v).__name__} {fitted}: {repr(v)[:200]}>"
    if depth >= 3:
        return f"<{type(v).__name__}>"
    if isinstance(v, (list, tuple)):
        out = [_summary(x, depth + 1) for x in v[:16]]
        return out + ([f"... {len(v) - 16} more"] if len(v) > 16 else [])
    if isinstance(v, dict):
        items = list(v.items())
        out = {str(k): _summary(x, depth + 1) for k, x in items[:32]}
        if len(items) > 32:
            out["..."] = f"{len(items) - 32} more keys"
        return out
    return f"<{type(v).__name__}>"

def main():
    graph = JOB["graph"]
    custom_nodes = JOB["custom_nodes"]
    node_dir = Path(JOB["node_preview_dir"])
    node_dir.mkdir(parents=True, exist_ok=True)

    img = _INPUT

    events = []
    previews = {}
    values = {}      # node -> port -> summary, for outputs that aren't images

    def on_node(nid, phase, payload):
        events.append({"node_id": nid, "phase": phase,
                       "message": payload.get("message")})
        if phase == "done":
            outs = payload.get("outputs") or {}
            for port_name, v in outs.items():
                if not _is_image(v):
                    try:
                        values.setdefault(nid, {})[port_name] = _summary(v)
                    except Exception:
                        values.setdefault(nid, {})[port_name] = f"<{type(v).__name__}>"
            images = [(k, v) for k, v in outs.items() if _is_image(v)]
            if not images:
                return
            port, value = images[0]
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
            if not _is_image(value):
                continue            # reported in `values`, not saved as an image
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
        "values": values,
        "saved_models": _ml.RUNTIME["saved"],
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
    assets: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Execute a graph out-of-process. Returns the child's result manifest
    plus wall-clock timing, or a synthetic error dict on timeout/crash.

    assets: {"model:<id>" | "labels:<id>": file} — saved models and painted
    labels the graph loads (D-044). Copied into the run's scratch dir, the
    only place the guarded child can read them from."""
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

    staged: dict[str, str] = {}
    if assets:
        import shutil
        adir = scratch / "assets"
        adir.mkdir()
        for i, (key, src) in enumerate(sorted(assets.items())):
            dst = adir / f"{i}{Path(src).suffix}"
            shutil.copyfile(src, dst)
            staged[key] = str(dst)

    job = {
        "repo_root": repo_root,
        "assets": staged,
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
