"""Uploaded segmentation models (D-047): weights + a declarative spec, no code.

A user uploads a checkpoint and describes it:

    {"name": "tissue (histology)", "arch": "Segformer", "encoder": "mit_b0",
     "in_channels": 3, "classes": 1, "activation": "sigmoid",
     "input_size": [512, 768], "normalize": "imagenet", "color": "rgb",
     "threshold": 0.5, "foreground_class": 1,
     "postprocess": {"closing_radius": 3, "min_component_frac": 0.003,
                     "fill_holes": true, "smooth": false}}

Nothing the user sends is executed:
  * the network is built by Photon from an allow-listed segmentation_models_pytorch
    architecture (smp.create_model, encoder_weights=None: no downloads);
  * weights are read with safetensors or torch.load(weights_only=True), which
    only rebuilds tensors and plain containers (no pickled objects/code), then
    loaded with strict=True;
  * pre/post-processing are fixed Photon functions driven by the spec's values.
Uploads are checked in a separate CPU-only process (build, strict load, one
forward pass) before they can be used.

Runs go through the model sidecar like the built-in models (ModelManager:
lazy load, idle unload, VRAM budget) under the name "user:<model_id>". The
sidecar is outside the sandbox, so access is checked in the parent before a
run and handed over as a grant (see issue_grants / check_grant): a file the
sandboxed child can't read or write, named by a random token the child passes
along. The sidecar refuses a user model without a matching, unexpired grant.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ...config import config

ARCHS = ("Segformer", "Unet", "UnetPlusPlus", "DeepLabV3", "DeepLabV3Plus", "FPN",
         "Linknet", "PSPNet", "MAnet", "PAN", "UPerNet")
ACTIVATIONS = ("sigmoid", "softmax")
NORMALIZE = {"imagenet": ([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
             "zero_one": None, "custom": None}
COLORS = ("rgb", "bgr", "gray")
WEIGHT_EXTS = (".pt", ".pth", ".bin", ".ckpt", ".safetensors")
MAX_WEIGHTS_BYTES = int(os.getenv("PHOTON_MAX_MODEL_MB", "500")) * 1024 * 1024
GRANT_TTL_SECONDS = 6 * 3600
OP_NAME = "segment_with_model"
SUGGESTED_ENCODERS = ("mit_b0", "mit_b1", "mit_b2", "mit_b3", "mit_b4", "mit_b5",
                      "resnet18", "resnet34", "resnet50", "resnet101", "efficientnet-b0",
                      "efficientnet-b4", "mobilenet_v2", "timm-efficientnet-b0", "tu-convnext_tiny")


class SpecError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------

def _num(v, kind, lo, hi, what):
    try:
        x = kind(v)
    except (TypeError, ValueError):
        raise SpecError(f"{what} must be a number")
    if not lo <= x <= hi:
        raise SpecError(f"{what} must be between {lo} and {hi}")
    return x


def validate_spec(raw: Any) -> dict:
    """Clean and check a spec. Raises SpecError naming the bad field."""
    if not isinstance(raw, dict):
        raise SpecError("spec must be a JSON object")
    s: dict[str, Any] = {}
    s["name"] = str(raw.get("name") or "").strip()[:80]
    if not s["name"]:
        raise SpecError("name is required")
    s["description"] = str(raw.get("description") or "").strip()[:1000]
    arch = raw.get("arch")
    if arch not in ARCHS:
        raise SpecError(f"arch must be one of {', '.join(ARCHS)}")
    s["arch"] = arch
    enc = str(raw.get("encoder") or "")
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{2,64}", enc):
        raise SpecError("encoder must be an encoder name such as mit_b0 or resnet34")
    s["encoder"] = enc
    s["in_channels"] = _num(raw.get("in_channels", 3), int, 1, 4, "in_channels")
    s["classes"] = _num(raw.get("classes", 1), int, 1, 64, "classes")
    act = raw.get("activation") or ("sigmoid" if s["classes"] == 1 else "softmax")
    if act not in ACTIVATIONS:
        raise SpecError("activation must be sigmoid or softmax")
    if act == "softmax" and s["classes"] < 2:
        raise SpecError("softmax needs at least 2 classes")
    s["activation"] = act
    size = raw.get("input_size") or [512, 512]
    if not (isinstance(size, (list, tuple)) and len(size) == 2):
        raise SpecError("input_size must be [height, width]")
    s["input_size"] = [_num(size[0], int, 32, 4096, "input height"),
                       _num(size[1], int, 32, 4096, "input width")]
    if any(v % 32 for v in s["input_size"]):
        raise SpecError("input_size values must be multiples of 32")
    norm = raw.get("normalize") or "imagenet"
    if norm not in NORMALIZE:
        raise SpecError("normalize must be imagenet, zero_one or custom")
    s["normalize"] = norm
    if norm == "custom":
        mean, std = raw.get("mean"), raw.get("std")
        if not (isinstance(mean, list) and isinstance(std, list) and len(mean) == len(std) == s["in_channels"]):
            raise SpecError(f"custom normalize needs mean and std lists of {s['in_channels']} numbers")
        s["mean"] = [_num(m, float, -1000, 1000, "mean") for m in mean]
        s["std"] = [_num(v, float, 1e-6, 1000, "std") for v in std]
    color = raw.get("color") or ("gray" if s["in_channels"] == 1 else "rgb")
    if color not in COLORS:
        raise SpecError("color must be rgb, bgr or gray")
    if (color == "gray") != (s["in_channels"] == 1):
        raise SpecError("color gray goes with in_channels 1, rgb/bgr with 3")
    s["color"] = color
    s["threshold"] = _num(raw.get("threshold", 0.5), float, 0.0, 1.0, "threshold")
    s["foreground_class"] = _num(raw.get("foreground_class", 1), int, 0, s["classes"] - 1 if act == "softmax" else 1,
                                 "foreground_class")
    pp = raw.get("postprocess") or {}
    if not isinstance(pp, dict):
        raise SpecError("postprocess must be an object")
    s["postprocess"] = {
        "closing_radius": _num(pp.get("closing_radius", 0), int, 0, 50, "closing_radius"),
        "min_component_frac": _num(pp.get("min_component_frac", 0.0), float, 0.0, 0.5, "min_component_frac"),
        "fill_holes": bool(pp.get("fill_holes", False)),
        "smooth": bool(pp.get("smooth", False)),
    }
    s["modality"] = str(raw.get("modality") or "").strip()[:40]   # e.g. "histology", "BFI": a hint for users
    return s


# ---------------------------------------------------------------------------
# Building and loading (trusted code; never the user's)
# ---------------------------------------------------------------------------

def build(spec: dict):
    import segmentation_models_pytorch as smp
    return smp.create_model(arch=spec["arch"], encoder_name=spec["encoder"], encoder_weights=None,
                            in_channels=spec["in_channels"], classes=spec["classes"])


def read_state_dict(path: Path) -> dict:
    """Tensors only: safetensors, or torch.load(weights_only=True). Unwraps the
    usual checkpoint wrappers and DataParallel 'module.' prefixes."""
    import torch
    path = Path(path)
    if path.suffix == ".safetensors":
        from safetensors.torch import load_file
        sd = load_file(str(path), device="cpu")
    else:
        sd = torch.load(str(path), map_location="cpu", weights_only=True)
    for key in ("state_dict", "model_state_dict", "model", "net", "weights"):
        if isinstance(sd, dict) and isinstance(sd.get(key), dict) and not torch.is_tensor(sd.get(key)):
            sd = sd[key]
            break
    if not isinstance(sd, dict) or not sd or not all(torch.is_tensor(v) for v in sd.values()):
        raise SpecError("the file is not a state_dict (a mapping of parameter names to tensors)")
    if all(k.startswith("module.") for k in sd):
        sd = {k[len("module."):]: v for k, v in sd.items()}
    return sd


class Handle:
    """What ModelManager holds for a user model: the network plus its spec."""

    def __init__(self, net, spec: dict, device: str):
        self.net, self.spec, self.device = net, spec, device

    def parameters(self):
        return self.net.parameters()


def load(spec: dict, weights: Path, device: str = "cpu") -> Handle:
    net = build(spec)
    sd = read_state_dict(weights)
    result = net.load_state_dict(sd, strict=False)
    if result.missing_keys or result.unexpected_keys:
        miss, extra = result.missing_keys, result.unexpected_keys
        raise SpecError("weights don't match this architecture/encoder/classes: "
                        f"{len(miss)} missing (e.g. {miss[:3]}), {len(extra)} unexpected (e.g. {extra[:3]})")
    return Handle(net.to(device).eval(), spec, device)


def check(spec: dict, weights: Path) -> dict:
    """Build, strict-load and run one forward pass at the input size (CPU)."""
    import torch
    h = load(spec, weights, "cpu")
    H, W = spec["input_size"]
    with torch.no_grad():
        y = h.net(torch.zeros(1, spec["in_channels"], H, W))
    if tuple(y.shape) != (1, spec["classes"], H, W):
        raise SpecError(f"model output is {tuple(y.shape)}, expected (1, {spec['classes']}, {H}, {W})")
    n = sum(p.numel() for p in h.net.parameters())
    return {"parameters": int(n), "output_shape": list(y.shape)}


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def weights_dir() -> Path:
    d = config.VOLUMES_DIR / "model_weights"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _is_id(s: Any) -> bool:
    return isinstance(s, str) and len(s) == 32 and all(c in "0123456789abcdef" for c in s)


def public(row: dict) -> dict:
    return {k: row[k] for k in ("model_id", "name", "spec", "size_bytes", "sha256", "status", "error",
                                "info", "created_at", "published")} | {"description": row["spec"].get("description", "")}


def create(user_id: str, spec_raw: Any, filename: str, stream, run_check: bool = True) -> dict:
    """Store an upload (validating in the background). `stream`: binary file object."""
    from ..session import store
    spec = validate_spec(spec_raw)
    from ..housekeeping import QuotaError, check_quota
    try:
        check_quota(user_id)                                # D-048
    except QuotaError as e:
        raise SpecError(str(e)) from None
    ext = Path(filename or "").suffix.lower()
    if ext not in WEIGHT_EXTS:
        raise SpecError(f"weights file must be one of {', '.join(WEIGHT_EXTS)}")
    mid = uuid.uuid4().hex
    path = weights_dir() / f"{mid}{ext}"
    h = hashlib.sha256()
    size = 0
    with open(path, "wb") as f:
        while True:
            chunk = stream.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_WEIGHTS_BYTES:
                f.close()
                path.unlink(missing_ok=True)
                raise SpecError(f"weights larger than {MAX_WEIGHTS_BYTES >> 20} MB")
            h.update(chunk)
            f.write(chunk)
    if not size:
        path.unlink(missing_ok=True)
        raise SpecError("empty weights file")
    store.add_seg_model({"model_id": mid, "user_id": user_id, "name": spec["name"], "spec": spec,
                         "weights_file": path.name, "size_bytes": size, "sha256": h.hexdigest(),
                         "status": "checking", "created_at": datetime.utcnow().isoformat()})
    if run_check:
        import threading
        threading.Thread(target=run_check_now, args=(mid,), daemon=True).start()
    return public(store.get_seg_model(mid))


def run_check_now(model_id: str) -> dict:
    """Check an upload in a separate CPU-only process; record ready / failed."""
    from ..session import store
    row = store.get_seg_model(model_id)
    if not row:
        return {}
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp"),
           "CUDA_VISIBLE_DEVICES": "", "PHOTON_NO_DOTENV": "1", "OMP_NUM_THREADS": "4",
           "PHOTON_VOLUMES_DIR": str(config.VOLUMES_DIR)}
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "src.backend.core.models.user_models", "check",
             str(weights_dir() / row["weights_file"]), json.dumps(row["spec"])],
            capture_output=True, text=True, timeout=900, cwd=str(config.ROOT_DIR), env=env)
        last = (proc.stdout.strip().splitlines() or ["{}"])[-1]
        res = json.loads(last) if last.startswith("{") else {}
        if not res and proc.returncode:
            res = {"ok": False, "error": (proc.stderr.strip().splitlines() or ["check failed"])[-1][:500]}
    except subprocess.TimeoutExpired:
        res = {"ok": False, "error": "check timed out (15 min)"}
    except Exception as e:                       # noqa: BLE001
        res = {"ok": False, "error": f"check could not run: {e}"}
    if res.get("ok"):
        store.update_seg_model(model_id, status="ready", error=None, info=res.get("info") or {})
    else:
        store.update_seg_model(model_id, status="failed", error=str(res.get("error") or "check failed")[:1000])
    return res


def recheck_stuck() -> None:
    """Uploads left 'checking' by a restart get checked again."""
    from ..session import store
    import threading
    for mid in store.seg_models_with_status("checking"):
        threading.Thread(target=run_check_now, args=(mid,), daemon=True).start()


def can_use(user_id: str, row: Optional[dict]) -> bool:
    return bool(row) and row["status"] == "ready" and not row["deleted_at"] and (
        row["published"] or row["user_id"] == user_id)


def delete(user_id: str, model_id: str) -> bool:
    from ..session import store
    row = store.get_seg_model(model_id) if _is_id(model_id) else None
    if not row or row["user_id"] != user_id or row["deleted_at"]:
        return False
    store.update_seg_model(model_id, deleted_at=datetime.utcnow().isoformat())
    if not row["published"]:
        (weights_dir() / row["weights_file"]).unlink(missing_ok=True)
    return True


def model_ids(graph: dict) -> list[str]:
    return [str((n.get("params") or {}).get("model_id") or "") for n in graph.get("nodes") or []
            if n.get("op") == OP_NAME]


def check_publishable(user_id: str, graph: dict) -> list[str]:
    from ..session import store
    ids = model_ids(graph)
    for mid in ids:
        row = store.get_seg_model(mid) if _is_id(mid) else None
        if not row or row["status"] != "ready" or not (row["published"] or (row["user_id"] == user_id
                                                                             and not row["deleted_at"])):
            raise SpecError(f"Segmentation model '{mid}' isn't one of yours (or isn't ready), so it can't be published.")
    return ids


# ---------------------------------------------------------------------------
# Per-run grants (parent -> sidecar; the sandboxed child only carries tokens)
# ---------------------------------------------------------------------------

def grants_dir() -> Path:
    d = config.VOLUMES_DIR / "model_grants"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def issue_grants(graph: dict, user_id: str) -> dict[str, str]:
    """{model_id: token} for every user model the graph runs. Raises SpecError
    naming the first the user may not use."""
    from ..session import store
    out: dict[str, str] = {}
    for mid in model_ids(graph):
        if mid in out:
            continue
        row = store.get_seg_model(mid) if _is_id(mid) else None
        if not can_use(user_id, row):
            raise SpecError(f"Segmentation model '{mid}' was not found, isn't ready, or is not yours.")
        token = secrets.token_hex(24)
        (grants_dir() / token).write_text(json.dumps({"model_id": mid, "expires": time.time() + GRANT_TTL_SECONDS}))
        out[mid] = token
    return out


def revoke_grants(grants: dict[str, str]) -> None:
    for token in (grants or {}).values():
        if re.fullmatch(r"[0-9a-f]{48}", str(token)):
            (grants_dir() / token).unlink(missing_ok=True)


def check_grant(token: Any, model_id: Any) -> bool:
    if not (isinstance(token, str) and re.fullmatch(r"[0-9a-f]{48}", token) and _is_id(model_id)):
        return False
    p = grants_dir() / token
    try:
        g = json.loads(p.read_text())
    except (OSError, ValueError):
        return False
    return g.get("model_id") == model_id and float(g.get("expires", 0)) > time.time()


# ---------------------------------------------------------------------------
# Sidecar side: ModelSpec for "user:<id>"
# ---------------------------------------------------------------------------

def model_spec(model_id: str):
    from ..session import store
    from .registry import ModelSpec
    row = store.get_seg_model(model_id) if _is_id(model_id) else None
    if not row or row["status"] != "ready" or (row["deleted_at"] and not row["published"]):
        return None
    weights = weights_dir() / row["weights_file"]
    spec = row["spec"]
    H, W = spec["input_size"]
    vram = int(200 + 4 * row["size_bytes"] / 2**20 + H * W * 4 * 64 / 2**20)   # rough: weights + activations
    return ModelSpec(name=f"user:{model_id}", loader=lambda device: load(spec, weights, device),
                     vram_mb=vram, doc=row["name"])


if __name__ == "__main__":
    # python -m src.backend.core.models.user_models check <weights> <spec json>
    if len(sys.argv) == 4 and sys.argv[1] == "check":
        try:
            info = check(validate_spec(json.loads(sys.argv[3])), Path(sys.argv[2]))
            print(json.dumps({"ok": True, "info": info}))
        except Exception as e:                   # noqa: BLE001
            print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"[:1000]}))
        sys.exit(0)
    sys.exit("usage: python -m src.backend.core.models.user_models check <weights> <spec-json>")
