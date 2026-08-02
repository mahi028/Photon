"""Mask composition / application ops."""

from __future__ import annotations

import numpy as np
from scipy import ndimage as ndi

from ..registry import Param, Port, register
from ..types import PortType


@register(
    name="apply_mask", category="masking",
    inputs=[Port("image", PortType.IMAGE_ANY), Port("mask", PortType.MASK)],
    outputs=[Port("image", PortType.IMAGE_ANY)],
    params=[Param("background", "int", 0, min=0, max=255,
                  doc="Value written where the mask is False.")],
    doc="Keep image pixels where mask is True; fill the rest with a constant.",
)
def apply_mask(image, mask, background=0):
    out = np.array(image, copy=True)
    m = np.asarray(mask, dtype=bool)
    if out.ndim == 3:
        out[~m] = int(background)
    else:
        out[~m] = int(background)
    return {"image": out}


@register(
    name="mask_invert", category="masking",
    inputs=[Port("mask", PortType.MASK)],
    outputs=[Port("mask", PortType.MASK)],
    doc="Logical NOT of a mask.",
)
def mask_invert(mask):
    return {"mask": ~np.asarray(mask, dtype=bool)}


@register(
    name="mask_combine", category="masking",
    inputs=[Port("mask_a", PortType.MASK), Port("mask_b", PortType.MASK)],
    outputs=[Port("mask", PortType.MASK)],
    params=[Param("mode", "choice", "and", choices=["and", "or", "xor", "subtract"])],
    doc="Combine two masks with a boolean operation.",
)
def mask_combine(mask_a, mask_b, mode="and"):
    a = np.asarray(mask_a, dtype=bool)
    b = np.asarray(mask_b, dtype=bool)
    out = {"and": a & b, "or": a | b, "xor": a ^ b, "subtract": a & ~b}[mode]
    return {"mask": out}


@register(
    name="largest_component", category="masking",
    inputs=[Port("mask", PortType.MASK)],
    outputs=[Port("mask", PortType.MASK)],
    params=[Param("count", "int", 1, min=1, max=100,
                  doc="Keep the N largest connected components.")],
    doc="Keep only the largest connected component(s) of a mask.",
)
def largest_component(mask, count=1):
    labels, n = ndi.label(np.asarray(mask, dtype=bool))
    if n == 0:
        return {"mask": np.zeros_like(mask, dtype=bool)}
    sizes = ndi.sum(np.ones_like(labels), labels, index=range(1, n + 1))
    keep = np.argsort(sizes)[::-1][:int(count)] + 1
    return {"mask": np.isin(labels, keep)}


@register(
    name="mask_overlay", category="masking",
    inputs=[Port("image", PortType.IMAGE_ANY), Port("mask", PortType.MASK)],
    outputs=[Port("image", PortType.IMAGE_RGB)],
    params=[Param("alpha", "float", 0.4, min=0.0, max=1.0),
            Param("color", "choice", "red", choices=["red", "green", "blue", "yellow"])],
    doc="Blend a coloured overlay onto the image where the mask is True (for review).",
)
def mask_overlay(image, mask, alpha=0.4, color="red"):
    arr = np.asarray(image)
    rgb = np.dstack([arr] * 3).astype(np.float64) if arr.ndim == 2 else arr.astype(np.float64)
    rgb_color = {"red": (255, 0, 0), "green": (0, 255, 0),
                 "blue": (0, 0, 255), "yellow": (255, 255, 0)}[color]
    m = np.asarray(mask, dtype=bool)
    for c in range(3):
        chan = rgb[:, :, c]
        chan[m] = chan[m] * (1 - float(alpha)) + rgb_color[c] * float(alpha)
    return {"image": np.clip(rgb, 0, 255).astype(np.uint8)}


@register(
    name="mask_to_image", category="masking",
    inputs=[Port("mask", PortType.MASK)],
    outputs=[Port("image", PortType.IMAGE_GRAY)],
    doc="Render a boolean mask as a black/white uint8 image (explicit binary output).",
)
def mask_to_image(mask):
    return {"image": (np.asarray(mask, dtype=np.uint8) * 255)}
