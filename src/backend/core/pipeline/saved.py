"""Saved models and painted labels (D-044): the files a graph can load.

A graph refers to them by id in node params:

  ml_load_model  {"model_id": "<id>"}         a fitted model saved by ml_save_model
  load_labels    {"annotation_id": "<id>"}    a label image painted in the UI
                 {"annotation_id": ""}        the latest one painted on the input image

Before each run, stage() checks the caller may read each one and returns the
files to copy into the sandbox (executor `assets`). After the run,
register_saved() turns the child's ml_save_model files into saved-model rows.

Models are immutable: a new save is a new row and id. That is what lets a
published pipeline pin one (publish marks it published, so anyone running
that pipeline may load it, and deleting it only hides it from its owner).
Annotations are editable and private to their author.
"""

from __future__ import annotations

import io
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import numpy as np

from ...config import config
from ..session import store

MAX_CLASSES = 16
MAX_NAME_LEN = 80


class AssetError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def annotations_dir() -> Path:
    d = config.VOLUMES_DIR / "annotations"
    d.mkdir(parents=True, exist_ok=True)
    return d


def model_path(model_id: str) -> Path:
    """File of a saved (sklearn) model, wherever the models table says it is."""
    from ..models import records
    row = records.get(model_id, "sklearn")
    if not row:
        raise AssetError(f"Saved model '{model_id}' was not found.", 404)
    return records.file_path(row)


def annotation_path(annotation_id: str) -> Path:
    return annotations_dir() / f"{annotation_id}.png"


def _is_id(s: Any) -> bool:
    return isinstance(s, str) and len(s) == 32 and all(c in "0123456789abcdef" for c in s)


# ---------------------------------------------------------------------------
# What a graph loads
# ---------------------------------------------------------------------------

def model_ids(graph: dict) -> list[str]:
    return [str((n.get("params") or {}).get("model_id") or "")
            for n in graph.get("nodes") or [] if n.get("op") == "ml_load_model"]


def annotation_ids(graph: dict) -> list[str]:
    return [str((n.get("params") or {}).get("annotation_id") or "")
            for n in graph.get("nodes") or [] if n.get("op") == "load_labels"]


def can_read_model(user_id: str, row: Optional[dict]) -> bool:
    from ..models import records
    return records.can_use(user_id, row)


def stage(graph: dict, user_id: str, image_id: Optional[str] = None,
          image_size: Optional[tuple[int, int]] = None) -> dict[str, str]:
    """{asset key: file} for every saved model / label image the graph loads.

    Raises AssetError naming the first one the user can't read. image_id /
    image_size (w, h) are the image being processed: `annotation_id: ""`
    means its latest annotation, and every annotation must match its size.
    """
    assets: dict[str, str] = {}
    from ..models import records
    for mid in model_ids(graph):
        row = records.get(mid, "sklearn")
        if not can_read_model(user_id, row) or not records.file_path(row).is_file():
            raise AssetError(f"Saved model '{mid}' was not found or is not yours.", 404)
        assets[f"model:{mid}"] = str(records.file_path(row))
    for aid in annotation_ids(graph):
        if aid:
            row = store.get_annotation(aid) if _is_id(aid) else None
            if not row or row["user_id"] != user_id:
                raise AssetError(f"Annotation '{aid}' was not found or is not yours.", 404)
        else:
            rows = store.list_annotations(user_id, image_id) if image_id else []
            if not rows:
                continue          # load_labels reports "no painted labels for this image"
            row = rows[0]
        if image_size and (row["width"], row["height"]) != tuple(image_size):
            raise AssetError(f"Annotation '{row['name']}' is {row['width']}x{row['height']} but the "
                             f"image is {image_size[0]}x{image_size[1]}; labels must be painted on "
                             "the image being processed.")
        assets[f"labels:{aid}"] = str(annotation_path(row["annotation_id"]))
    return assets


# ---------------------------------------------------------------------------
# Saved models
# ---------------------------------------------------------------------------

def _versions() -> dict[str, str]:
    from importlib.metadata import version
    out = {}
    for dist in ("scikit-learn", "numpy", "skops"):
        try:
            out[dist] = version(dist)
        except Exception:
            pass
    return out


def register_saved(user_id: str, saved: list[dict], session_id: Optional[str] = None,
                   pipeline_title: Optional[str] = None) -> list[dict]:
    """Move the child's ml_save_model files into the models store (kind
    sklearn, D-049). Returns the new models."""
    import hashlib
    from ..models import records
    rows = []
    versions = _versions()
    for s in saved or []:
        src = Path(s.get("path") or "")
        if not src.is_file() or src.suffix != ".skops":
            continue
        mid = uuid.uuid4().hex
        dst, rel = records.new_file(mid, ".skops")
        shutil.move(str(src), dst)
        spec = {"estimator": str(s.get("estimator") or "?")[:80], "versions": versions,
                "session_id": session_id, "pipeline_title": (pipeline_title or "")[:200],
                "summary": str(s.get("summary") or "")[:300]}
        store.add_model({"model_id": mid, "user_id": user_id, "kind": "sklearn",
                         "name": str(s.get("name") or "model")[:MAX_NAME_LEN], "file": rel,
                         "size_bytes": dst.stat().st_size,
                         "sha256": hashlib.sha256(dst.read_bytes()).hexdigest(),
                         "status": "ready", "spec": spec, "created_at": datetime.utcnow().isoformat()})
        rows.append(records.public(store.get_model(mid)))
    return rows


def list_models(user_id: str) -> list[dict]:
    from ..models import records
    return records.list_for(user_id, "sklearn")


def delete_model(user_id: str, model_id: str) -> bool:
    from ..models import records
    return bool(records.get(model_id, "sklearn")) and records.delete(user_id, model_id)


def check_publishable(user_id: str, graph: dict) -> list[str]:
    """Saved models a graph loads (each must be the publisher's own, or
    already published); painted labels can't be published."""
    from ..models import records
    try:
        ids = records.check_publishable(user_id, model_ids(graph), "sklearn", "Saved model")
    except ValueError as e:
        raise AssetError(str(e))
    if any(a for a in annotation_ids(graph)):
        raise AssetError("Painted labels are private and tied to one image; a published pipeline "
                         "can use load_labels only with annotation_id empty (the runner's own labels).")
    return ids


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------

def _clean_classes(classes: Any) -> list[dict]:
    out = []
    for i, c in enumerate(classes or []):
        if not isinstance(c, dict):
            raise AssetError("classes must be a list of {value, name, color}")
        v = c.get("value", i + 1)
        if not isinstance(v, int) or not 1 <= v <= MAX_CLASSES:
            raise AssetError(f"class values must be 1..{MAX_CLASSES}")
        color = str(c.get("color") or "")
        if color and not (len(color) == 7 and color[0] == "#"
                          and all(ch in "0123456789abcdefABCDEF" for ch in color[1:])):
            raise AssetError("class colors must be #rrggbb")
        out.append({"value": v, "name": str(c.get("name") or f"class {v}")[:40], "color": color})
    return out[:MAX_CLASSES]


def decode_labels(png: bytes, size: tuple[int, int]) -> np.ndarray:
    """PNG bytes -> (h, w) uint8 labels. Accepts L, P (palette index = label)
    or RGBA where the alpha=0 pixels are unlabeled and the red channel holds the label.

    The labels may be painted at a reduced size (whole-slide images are too big
    to paint at full resolution in a browser): any size up to the image's with
    the same aspect ratio. load_labels scales them back up, nearest-neighbour."""
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(png))
        im.load()
    except Exception:
        raise AssetError("labels must be a PNG image") from None
    w, h = im.size
    W, H = size
    if w > W or h > H or abs(h - w * H / W) > 1.0:          # same aspect, ±1 px rounding
        raise AssetError(f"labels are {w}x{h} but the image is {W}x{H}: use the image's size, "
                         "or a smaller one with the same aspect ratio")
    if im.mode in ("L", "P"):
        a = np.asarray(im)
    elif im.mode in ("RGBA", "LA"):
        arr = np.asarray(im)
        a = np.where(arr[..., -1] > 0, arr[..., 0], 0)
    else:
        a = np.asarray(im.convert("L"))
    a = a.astype(np.uint8)
    if a.max(initial=0) > MAX_CLASSES:
        raise AssetError(f"label values must be 0..{MAX_CLASSES}")
    return a


def save_annotation(user_id: str, image_id: str, png: bytes, image_size: tuple[int, int],
                    name: str = "", classes: Any = None, annotation_id: Optional[str] = None) -> dict:
    labels = decode_labels(png, image_size)
    now = datetime.utcnow().isoformat()
    if annotation_id:
        row = store.get_annotation(annotation_id) if _is_id(annotation_id) else None
        if not row or row["user_id"] != user_id or row["image_id"] != image_id:
            raise AssetError("Annotation not found.", 404)
        created = row["created_at"]
    else:
        annotation_id, created = uuid.uuid4().hex, now
    from PIL import Image
    from PIL import PngImagePlugin
    tmp = annotation_path(annotation_id).with_suffix(".tmp.png")
    info = PngImagePlugin.PngInfo()
    info.add_text("photon:size", f"{image_size[0]}x{image_size[1]}")   # the image's size, for load_labels
    Image.fromarray(labels, mode="L").save(tmp, pnginfo=info)
    tmp.replace(annotation_path(annotation_id))
    store.upsert_annotation({"annotation_id": annotation_id, "user_id": user_id, "image_id": image_id,
                             "name": (name or "").strip()[:MAX_NAME_LEN] or "labels",
                             "width": image_size[0], "height": image_size[1],
                             "classes": _clean_classes(classes), "created_at": created,
                             "updated_at": now})
    out = store.get_annotation(annotation_id)
    scale = (image_size[0] * image_size[1]) / max(1, labels.size)          # painted at reduced size
    out["counts"] = {int(v): int(round(c * scale)) for v, c in zip(*np.unique(labels, return_counts=True)) if v}
    out["painted_size"] = [labels.shape[1], labels.shape[0]]
    return out


def delete_annotation(user_id: str, annotation_id: str) -> bool:
    if not _is_id(annotation_id) or not store.delete_annotation(annotation_id, user_id):
        return False
    annotation_path(annotation_id).unlink(missing_ok=True)
    return True
