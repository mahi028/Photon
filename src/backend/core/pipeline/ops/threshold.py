"""Thresholding ops — produce boolean MASKs."""

from __future__ import annotations

import numpy as np
from skimage import filters as skfilters

from ..registry import Param, Port, register
from ..types import PortType


@register(
    name="otsu_threshold", category="threshold",
    inputs=[Port("image", PortType.IMAGE_GRAY)],
    outputs=[Port("mask", PortType.MASK)],
    params=[Param("nbins", "int", 256, min=2, max=1024),
            Param("invert", "bool", False, doc="Keep pixels BELOW the threshold instead.")],
    doc="Otsu automatic global thresholding (skimage.filters.threshold_otsu).",
)
def otsu_threshold(image, nbins=256, invert=False):
    t = skfilters.threshold_otsu(image, nbins=int(nbins))
    mask = image <= t if invert else image > t
    return {"mask": mask}


@register(
    name="manual_threshold", category="threshold",
    inputs=[Port("image", PortType.IMAGE_GRAY)],
    outputs=[Port("mask", PortType.MASK)],
    params=[Param("threshold", "int", 128, min=0, max=255),
            Param("invert", "bool", False)],
    doc="Fixed-value threshold.",
)
def manual_threshold(image, threshold=128, invert=False):
    mask = image <= threshold if invert else image > threshold
    return {"mask": mask}


@register(
    name="adaptive_threshold", category="threshold",
    inputs=[Port("image", PortType.IMAGE_GRAY)],
    outputs=[Port("mask", PortType.MASK)],
    params=[Param("block_size", "int", 35, min=3, max=999,
                  doc="Local window size; forced odd."),
            Param("offset", "float", 0.0, min=-64.0, max=64.0),
            Param("invert", "bool", False)],
    doc="Local adaptive thresholding (skimage.filters.threshold_local), for uneven illumination.",
)
def adaptive_threshold(image, block_size=35, offset=0.0, invert=False):
    bs = int(block_size)
    if bs % 2 == 0:
        bs += 1
    local = skfilters.threshold_local(image, block_size=bs, offset=float(offset))
    mask = image <= local if invert else image > local
    return {"mask": mask}


@register(
    name="threshold_float_map", category="threshold",
    inputs=[Port("map", PortType.FLOAT_MAP)],
    outputs=[Port("mask", PortType.MASK)],
    params=[Param("threshold", "float", 0.15, min=-1e6, max=1e6),
            Param("invert", "bool", False)],
    doc="Threshold an unnormalized float map (e.g. optical density) at an absolute value.",
)
def threshold_float_map(map, threshold=0.15, invert=False):
    arr = np.asarray(map, dtype=np.float64)
    mask = arr <= float(threshold) if invert else arr > float(threshold)
    return {"mask": mask}
