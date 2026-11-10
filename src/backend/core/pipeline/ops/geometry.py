"""Geometry ops (skimage.transform + numpy)."""

from __future__ import annotations

import numpy as np
from skimage import transform as sktransform

from ..registry import Param, Port, register
from ..types import PortType


@register(
    name="resize", category="geometry",
    inputs=[Port("image", PortType.IMAGE_ANY)],
    outputs=[Port("image", PortType.IMAGE_ANY)],
    params=[Param("scale", "float", 0.5, min=0.01, max=8.0)],
    doc="Rescale by a factor. Shrinking averages pixel areas (anti-aliased); enlarging is "
        "bilinear; masks stay masks (nearest).",
)
def resize(image, scale=0.5):
    # OpenCV, not skimage.rescale: on a 10k-px slide rescale's anti-aliasing
    # blur took ~8 s; INTER_AREA is the standard anti-aliased downscale and
    # takes ~0.2 s.
    import cv2
    a = np.asarray(image)
    s = float(scale)
    h, w = a.shape[:2]
    size = (max(1, int(round(w * s))), max(1, int(round(h * s))))
    if a.dtype == bool:
        return {"image": cv2.resize(a.astype(np.uint8), size, interpolation=cv2.INTER_NEAREST).astype(bool)}
    src = a if a.dtype in (np.uint8, np.uint16, np.float32) else a.astype(np.float32)
    channels = src.shape[2] if src.ndim == 3 else 0
    interp = cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR
    if channels > 4:            # cv2 handles <= 4 channels in one call reliably: do the rest per channel
        out = np.stack([cv2.resize(np.ascontiguousarray(src[..., c]), size, interpolation=interp)
                        for c in range(channels)], -1)
    else:
        out = cv2.resize(np.ascontiguousarray(src), size, interpolation=interp)
        if channels == 1:
            out = out[..., None]
    if out.dtype != a.dtype:
        out = np.rint(out) if np.issubdtype(a.dtype, np.integer) else out
        out = out.astype(a.dtype)
    return {"image": out}


@register(
    name="crop", category="geometry",
    inputs=[Port("image", PortType.IMAGE_ANY)],
    outputs=[Port("image", PortType.IMAGE_ANY)],
    params=[Param("top_pct", "float", 0.0, min=0.0, max=99.0),
            Param("left_pct", "float", 0.0, min=0.0, max=99.0),
            Param("height_pct", "float", 100.0, min=1.0, max=100.0),
            Param("width_pct", "float", 100.0, min=1.0, max=100.0)],
    doc="Crop a percentage-defined rectangle (resolution independent).",
)
def crop(image, top_pct=0.0, left_pct=0.0, height_pct=100.0, width_pct=100.0):
    h, w = image.shape[:2]
    t = int(h * float(top_pct) / 100.0)
    l = int(w * float(left_pct) / 100.0)
    bh = max(1, int(h * float(height_pct) / 100.0))
    bw = max(1, int(w * float(width_pct) / 100.0))
    return {"image": image[t:t + bh, l:l + bw]}


@register(
    name="rotate", category="geometry",
    inputs=[Port("image", PortType.IMAGE_ANY)],
    outputs=[Port("image", PortType.IMAGE_ANY)],
    params=[Param("angle", "float", 90.0, min=-360.0, max=360.0),
            Param("resize_canvas", "bool", True)],
    doc="Rotate by an arbitrary angle (skimage.transform.rotate).",
)
def rotate(image, angle=90.0, resize_canvas=True):
    out = sktransform.rotate(image, float(angle), resize=bool(resize_canvas),
                             preserve_range=True)
    return {"image": out.astype(image.dtype)}


@register(
    name="flip", category="geometry",
    inputs=[Port("image", PortType.IMAGE_ANY)],
    outputs=[Port("image", PortType.IMAGE_ANY)],
    params=[Param("axis", "choice", "horizontal", choices=["horizontal", "vertical"])],
    doc="Mirror the image horizontally or vertically.",
)
def flip(image, axis="horizontal"):
    return {"image": np.fliplr(image) if axis == "horizontal" else np.flipud(image)}
