"""Color / intensity ops — thin wrappers over skimage + numpy."""

from __future__ import annotations

import numpy as np
from skimage import color as skcolor, exposure

from ..registry import Param, Port, register
from ..types import PortType


@register(
    name="to_grayscale", category="color",
    inputs=[Port("image", PortType.IMAGE_RGB)],
    outputs=[Port("gray", PortType.IMAGE_GRAY)],
    doc="RGB to grayscale (skimage.color.rgb2gray), returned as uint8.",
)
def to_grayscale(image):
    g = skcolor.rgb2gray(image)
    return {"gray": (g * 255).astype(np.uint8)}


@register(
    name="optical_density", category="color",
    inputs=[Port("image", PortType.IMAGE_ANY)],
    outputs=[Port("od", PortType.FLOAT_MAP)],
    params=[Param("background", "float", 255.0, min=1.0, max=65535.0,
                  doc="Incident/background intensity I0.")],
    doc="Beer-Lambert optical density: OD = -log10(I / I0). Standard for stain quantification.",
)
def optical_density(image, background=255.0):
    arr = image.astype(np.float64)
    if arr.ndim == 3:
        arr = arr.mean(axis=2)
    arr = np.clip(arr, 1e-6, None)
    od = -np.log10(arr / float(background))
    return {"od": np.clip(od, 0.0, None)}


@register(
    name="hed_deconvolution", category="color",
    inputs=[Port("image", PortType.IMAGE_RGB)],
    outputs=[Port("channel", PortType.FLOAT_MAP)],
    params=[Param("stain", "choice", "hematoxylin",
                  choices=["hematoxylin", "eosin", "dab"],
                  doc="Which deconvolved stain channel to return.")],
    doc="H&E/DAB colour deconvolution (skimage.color.rgb2hed); returns one stain channel.",
)
def hed_deconvolution(image, stain="hematoxylin"):
    hed = skcolor.rgb2hed(image)
    idx = {"hematoxylin": 0, "eosin": 1, "dab": 2}[stain]
    return {"channel": hed[:, :, idx]}


@register(
    name="adjust_gamma", category="color",
    inputs=[Port("image", PortType.IMAGE_ANY)],
    outputs=[Port("image", PortType.IMAGE_ANY)],
    params=[Param("gamma", "float", 1.0, min=0.01, max=10.0)],
    doc="Gamma correction (skimage.exposure.adjust_gamma). <1 brightens, >1 darkens.",
)
def adjust_gamma(image, gamma=1.0):
    return {"image": exposure.adjust_gamma(image, gamma=gamma)}


@register(
    name="adjust_brightness", category="color",
    inputs=[Port("image", PortType.IMAGE_ANY)],
    outputs=[Port("image", PortType.IMAGE_ANY)],
    params=[Param("offset", "int", 0, min=-255, max=255)],
    doc="Add a constant offset to pixel intensities (clipped to valid range).",
)
def adjust_brightness(image, offset=0):
    out = image.astype(np.int32) + int(offset)
    return {"image": np.clip(out, 0, 255).astype(np.uint8)}


@register(
    name="adjust_contrast", category="color",
    inputs=[Port("image", PortType.IMAGE_ANY)],
    outputs=[Port("image", PortType.IMAGE_ANY)],
    params=[Param("factor", "float", 1.0, min=0.0, max=5.0)],
    doc="Scale contrast around the mean intensity.",
)
def adjust_contrast(image, factor=1.0):
    arr = image.astype(np.float64)
    mean = arr.mean()
    out = (arr - mean) * float(factor) + mean
    return {"image": np.clip(out, 0, 255).astype(np.uint8)}


@register(
    name="clahe", category="color",
    inputs=[Port("image", PortType.IMAGE_GRAY)],
    outputs=[Port("image", PortType.IMAGE_GRAY)],
    params=[Param("clip_limit", "float", 0.01, min=0.001, max=1.0)],
    doc="Contrast Limited Adaptive Histogram Equalization (skimage.exposure.equalize_adapthist).",
)
def clahe(image, clip_limit=0.01):
    out = exposure.equalize_adapthist(image, clip_limit=float(clip_limit))
    return {"image": (out * 255).astype(np.uint8)}


@register(
    name="invert", category="color",
    inputs=[Port("image", PortType.IMAGE_GRAY)],
    outputs=[Port("image", PortType.IMAGE_GRAY)],
    doc="Invert intensities (255 - x).",
)
def invert(image):
    return {"image": (255 - image.astype(np.uint8))}


@register(
    name="stretch_contrast", category="color",
    inputs=[Port("image", PortType.IMAGE_GRAY)],
    outputs=[Port("image", PortType.IMAGE_GRAY)],
    params=[Param("low_pct", "float", 2.0, min=0.0, max=49.0),
            Param("high_pct", "float", 98.0, min=51.0, max=100.0)],
    doc="Percentile-based contrast stretch (skimage.exposure.rescale_intensity).",
)
def stretch_contrast(image, low_pct=2.0, high_pct=98.0):
    lo, hi = np.percentile(image, (float(low_pct), float(high_pct)))
    out = exposure.rescale_intensity(image, in_range=(lo, hi))
    return {"image": out.astype(np.uint8)}
