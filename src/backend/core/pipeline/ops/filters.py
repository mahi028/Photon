"""Spatial filtering / edge ops (skimage.filters + scipy.ndimage)."""

from __future__ import annotations

import numpy as np
from skimage import filters as skfilters

from ..registry import Param, Port, register
from ..types import PortType


@register(
    name="gaussian_blur", category="filters",
    inputs=[Port("image", PortType.IMAGE_ANY)],
    outputs=[Port("image", PortType.IMAGE_ANY)],
    params=[Param("sigma", "float", 1.0, min=0.1, max=50.0)],
    doc="Gaussian smoothing (skimage.filters.gaussian).",
)
def gaussian_blur(image, sigma=1.0):
    channel_axis = -1 if image.ndim == 3 else None
    out = skfilters.gaussian(image, sigma=float(sigma), channel_axis=channel_axis)
    return {"image": (out * 255).astype(np.uint8) if out.max() <= 1.0 else out.astype(np.uint8)}


@register(
    name="median_filter", category="filters",
    inputs=[Port("image", PortType.IMAGE_GRAY)],
    outputs=[Port("image", PortType.IMAGE_GRAY)],
    params=[Param("radius", "int", 2, min=1, max=25)],
    doc="Median filter — removes salt-and-pepper noise while keeping edges.",
)
def median_filter(image, radius=2):
    from skimage.morphology import disk
    return {"image": skfilters.median(image, disk(int(radius))).astype(np.uint8)}


@register(
    name="sobel_edges", category="filters",
    inputs=[Port("image", PortType.IMAGE_GRAY)],
    outputs=[Port("edges", PortType.FLOAT_MAP)],
    doc="Sobel edge magnitude (skimage.filters.sobel).",
)
def sobel_edges(image):
    return {"edges": skfilters.sobel(image)}


@register(
    name="laplacian_edges", category="filters",
    inputs=[Port("image", PortType.IMAGE_GRAY)],
    outputs=[Port("edges", PortType.FLOAT_MAP)],
    params=[Param("ksize", "int", 3, min=1, max=31)],
    doc="Laplacian second-derivative edge response (skimage.filters.laplace).",
)
def laplacian_edges(image, ksize=3):
    return {"edges": skfilters.laplace(image, ksize=int(ksize))}


@register(
    name="unsharp_mask", category="filters",
    inputs=[Port("image", PortType.IMAGE_ANY)],
    outputs=[Port("image", PortType.IMAGE_ANY)],
    params=[Param("radius", "float", 1.0, min=0.1, max=50.0),
            Param("amount", "float", 1.0, min=0.0, max=10.0)],
    doc="Unsharp masking for sharpening (skimage.filters.unsharp_mask).",
)
def unsharp_mask(image, radius=1.0, amount=1.0):
    channel_axis = -1 if image.ndim == 3 else None
    out = skfilters.unsharp_mask(image, radius=float(radius), amount=float(amount),
                                 channel_axis=channel_axis)
    return {"image": (out * 255).astype(np.uint8)}


@register(
    name="normalize_map", category="filters",
    inputs=[Port("map", PortType.FLOAT_MAP)],
    outputs=[Port("image", PortType.IMAGE_GRAY)],
    doc="Min-max normalize a float map into a uint8 grayscale image.",
)
def normalize_map(map):
    v = np.asarray(map, dtype=np.float64)
    lo, hi = float(v.min()), float(v.max())
    if hi - lo < 1e-12:
        return {"image": np.zeros(v.shape, dtype=np.uint8)}
    return {"image": ((v - lo) / (hi - lo) * 255.0).astype(np.uint8)}
