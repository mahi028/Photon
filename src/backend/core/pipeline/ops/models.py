"""Model-backed ops.

These run in the model sidecar, not in the sandboxed pipeline child. Note what
is absent: no weight loading, no `.cuda()`, no device management. The `model`
argument arrives already loaded, and ModelManager owns its lifetime — lazy
load, refcount while in use, unload after the idle timeout, LRU eviction under
the VRAM budget.

That absence is enforced, not stylistic: core/models/guard.py rejects direct
model loading in this directory and the app refuses to start. To add a model,
declare it in core/models/definitions.py — see CODING_STYLE.md "Adding a model".
"""

from __future__ import annotations

import numpy as np

from ..registry import Param, Port, register_model_op
from ..types import PortType

# VOC/COCO classes DeepLabV3 was trained on; index 0 is background.
_VOC_CLASSES = [
    "background", "aeroplane", "bicycle", "bird", "boat", "bottle", "bus",
    "car", "cat", "chair", "cow", "diningtable", "dog", "horse", "motorbike",
    "person", "pottedplant", "sheep", "sofa", "train", "tvmonitor",
]


def _to_float_chw(image: np.ndarray):
    """HWC uint8/float image -> normalized (1,3,H,W) float tensor on the model's device."""
    import torch

    arr = np.asarray(image)
    if arr.ndim == 2:
        arr = np.stack([arr] * 3, axis=-1)
    if arr.shape[2] == 4:
        arr = arr[:, :, :3]
    arr = arr.astype(np.float32)
    if arr.max() > 1.0:
        arr = arr / 255.0
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    arr = (arr - mean) / std
    return torch.from_numpy(arr.transpose(2, 0, 1)).unsqueeze(0)


@register_model_op(
    name="dinov2_features", category="features", model="dinov2_vits14",
    inputs=[Port("image", PortType.IMAGE_ANY)],
    outputs=[Port("features", PortType.FEATURE_MAP)],
    params=[Param("resize_to", "int", 518, min=126, max=1022,
                  doc="Image is resized to this (multiple of 14) before encoding.")],
    doc="DINOv2 ViT-S/14 patch features as an (H/14, W/14, 384) FEATURE_MAP. "
        "Self-supervised, robust to appearance changes; pair with feature_mean "
        "or feature_channel to view.",
)
def dinov2_features(image, model, resize_to=518):
    import torch
    import torch.nn.functional as F

    size = max(126, (int(resize_to) // 14) * 14)
    x = _to_float_chw(image)
    x = F.interpolate(x, size=(size, size), mode="bilinear", align_corners=False)
    x = x.to(next(model.parameters()).device)

    with torch.no_grad():
        out = model.forward_features(x)
    patches = out["x_norm_patchtokens"][0]           # (N, C)

    n, c = patches.shape
    grid = size // 14
    fmap = patches.reshape(grid, grid, c).float().cpu().numpy()
    return {"features": fmap}


@register_model_op(
    name="segment_foreground", category="masking", model="deeplabv3_resnet50",
    inputs=[Port("image", PortType.IMAGE_ANY)],
    outputs=[Port("mask", PortType.MASK)],
    params=[
        Param("target_class", "choice", "any", choices=["any"] + _VOC_CLASSES[1:],
              doc="Keep this class only, or 'any' for every non-background class."),
        Param("threshold", "float", 0.5, min=0.0, max=1.0,
              doc="Minimum softmax confidence for a pixel to count as foreground."),
    ],
    doc="Model-based foreground segmentation (DeepLabV3/ResNet-50, 20 object "
        "classes). Feed the mask to apply_mask for background removal.",
)
def segment_foreground(image, model, target_class="any", threshold=0.5):
    import torch
    import torch.nn.functional as F

    arr = np.asarray(image)
    h, w = arr.shape[:2]
    x = _to_float_chw(arr).to(next(model.parameters()).device)

    with torch.no_grad():
        logits = model(x)["out"]
    logits = F.interpolate(logits, size=(h, w), mode="bilinear", align_corners=False)
    probs = torch.softmax(logits, dim=1)[0]          # (C, H, W)

    if target_class == "any":
        fg = 1.0 - probs[0]                          # everything but background
    else:
        fg = probs[_VOC_CLASSES.index(target_class)]

    return {"mask": (fg >= float(threshold)).cpu().numpy().astype(bool)}


# ---------------------------------------------------------------------------
# Uploaded segmentation models (D-047)
# ---------------------------------------------------------------------------

def _seg_preprocess(image: np.ndarray, spec: dict):
    """Image -> (1, C, H, W) float tensor at the spec's input size."""
    import cv2
    import torch
    a = np.asarray(image)
    if a.dtype == np.uint16:
        a = (a / 257).astype(np.uint8)
    elif a.dtype != np.uint8:
        f = a.astype(np.float32)
        a = (np.clip(f, 0, 1) * 255 if f.max() <= 1.0 else np.clip(f, 0, 255)).astype(np.uint8)
    if spec["color"] == "gray":
        a = a if a.ndim == 2 else cv2.cvtColor(a[..., :3], cv2.COLOR_RGB2GRAY)
        a = a[..., None]
    else:
        a = np.stack([a] * 3, -1) if a.ndim == 2 else a[..., :3]
        if spec["color"] == "bgr":
            a = a[..., ::-1]
    H, W = spec["input_size"]
    x = cv2.resize(np.ascontiguousarray(a), (W, H), interpolation=cv2.INTER_AREA)
    x = x.reshape(H, W, -1).astype(np.float32) / 255.0
    if spec["normalize"] == "imagenet":
        x = (x - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
    elif spec["normalize"] == "custom":
        x = (x - np.array(spec["mean"], np.float32)) / np.array(spec["std"], np.float32)
    return torch.from_numpy(x.transpose(2, 0, 1).copy()).unsqueeze(0)


def _seg_postprocess(mask: np.ndarray, pp: dict) -> np.ndarray:
    import cv2
    m = mask.astype(np.uint8) * 255
    r = int(pp.get("closing_radius") or 0)
    if r:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    frac = float(pp.get("min_component_frac") or 0)
    if frac > 0 or pp.get("fill_holes"):
        n, lab, st, _ = cv2.connectedComponentsWithStats((m > 0).astype(np.uint8), 8)
        keep = np.zeros(n, bool)
        keep[1:] = st[1:, cv2.CC_STAT_AREA] >= frac * m.size
        m = np.where(keep[lab], 255, 0).astype(np.uint8)
        if pp.get("fill_holes"):
            from scipy import ndimage as ndi
            m = (ndi.binary_fill_holes(m > 0) * 255).astype(np.uint8)
    if pp.get("smooth"):
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
        m = cv2.GaussianBlur(m, (0, 0), 2)
        m = np.where(m < 127, 0, 255).astype(np.uint8)
    return m > 0


@register_model_op(
    name="segment_with_model", category="masking", model="__user__",
    inputs=[Port("image", PortType.IMAGE_ANY)],
    outputs=[Port("mask", PortType.MASK), Port("probability", PortType.FLOAT_MAP),
             Port("labels", PortType.ARRAY)],
    params=[
        Param("model_id", "str", None, required=True,
              doc="An uploaded segmentation model (Models page, or list_models(kind='segmentation') over MCP)."),
        Param("threshold", "float", None, nullable=True, min=0.0, max=1.0,
              doc="Probability cut-off for the mask. Empty: the model's own setting."),
        Param("postprocess", "bool", True,
              doc="Apply the model's post-processing (closing, small components, hole filling)."),
    ],
    doc="Run an uploaded segmentation model (e.g. a tissue/background SegFormer). The image is "
        "resized to the model's input size and normalised as declared; outputs come back at the "
        "image's size: mask (foreground), probability (of foreground), labels (class per pixel).",
)
def segment_with_model(image, model, model_id=None, threshold=None, postprocess=True):
    import cv2
    import torch

    spec = model.spec
    arr = np.asarray(image)
    h, w = arr.shape[:2]
    x = _seg_preprocess(arr, spec).to(model.device)
    with torch.no_grad():
        logits = model.net(x)[0].float()                 # (C, H, W)
    if spec["activation"] == "sigmoid":
        prob = torch.sigmoid(logits).cpu().numpy()
        fg = prob[0]
        labels_small = None
    else:
        prob = torch.softmax(logits, 0).cpu().numpy()
        fg = prob[spec["foreground_class"]]
        labels_small = prob.argmax(0).astype(np.uint8)
    fg = cv2.resize(fg.astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR)
    t = spec["threshold"] if threshold is None else float(threshold)
    mask = fg > t
    if postprocess:
        mask = _seg_postprocess(mask, spec["postprocess"])
    if labels_small is None:
        labels = mask.astype(np.int32)
    else:
        labels = cv2.resize(labels_small, (w, h), interpolation=cv2.INTER_NEAREST).astype(np.int32)
    return {"mask": mask, "probability": np.clip(fg, 0, 1), "labels": labels}
