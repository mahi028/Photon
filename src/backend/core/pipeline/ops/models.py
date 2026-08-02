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
