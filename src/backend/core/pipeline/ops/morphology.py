"""Binary morphology ops — operate on MASKs (skimage.morphology)."""

from __future__ import annotations

import numpy as np
from scipy import ndimage as ndi
from skimage import morphology as skmorph

from ..registry import Param, Port, register
from ..types import PortType


def _footprint(radius: int):
    return skmorph.disk(max(1, int(radius)))


@register(
    name="morph_closing", category="morphology",
    inputs=[Port("mask", PortType.MASK)],
    outputs=[Port("mask", PortType.MASK)],
    params=[Param("radius", "int", 3, min=1, max=50)],
    doc="Binary closing (dilate then erode) — fills small gaps/holes in objects.",
)
def morph_closing(mask, radius=3):
    return {"mask": skmorph.closing(mask, _footprint(radius))}


@register(
    name="morph_opening", category="morphology",
    inputs=[Port("mask", PortType.MASK)],
    outputs=[Port("mask", PortType.MASK)],
    params=[Param("radius", "int", 3, min=1, max=50)],
    doc="Binary opening (erode then dilate) — removes small specks/noise.",
)
def morph_opening(mask, radius=3):
    return {"mask": skmorph.opening(mask, _footprint(radius))}


@register(
    name="morph_erode", category="morphology",
    inputs=[Port("mask", PortType.MASK)],
    outputs=[Port("mask", PortType.MASK)],
    params=[Param("radius", "int", 1, min=1, max=50)],
    doc="Binary erosion — shrinks objects.",
)
def morph_erode(mask, radius=1):
    return {"mask": skmorph.erosion(mask, _footprint(radius))}


@register(
    name="morph_dilate", category="morphology",
    inputs=[Port("mask", PortType.MASK)],
    outputs=[Port("mask", PortType.MASK)],
    params=[Param("radius", "int", 1, min=1, max=50)],
    doc="Binary dilation — grows objects.",
)
def morph_dilate(mask, radius=1):
    return {"mask": skmorph.dilation(mask, _footprint(radius))}


@register(
    name="remove_small_objects", category="morphology",
    inputs=[Port("mask", PortType.MASK)],
    outputs=[Port("mask", PortType.MASK)],
    params=[Param("min_size", "int", 64, min=1, max=1000000)],
    doc="Drop connected components smaller than min_size pixels.",
)
def remove_small_objects(mask, min_size=64):
    # Implemented directly rather than via skimage.morphology.remove_small_objects:
    # that function's `min_size` kwarg is deprecated in skimage 0.26 in favour of
    # a `max_size` parameter with inverted, off-by-one semantics. Labelling and
    # filtering by component size here keeps the meaning stable across versions.
    m = np.asarray(mask, dtype=bool)
    labels, n = ndi.label(m)
    if n == 0:
        return {"mask": m}
    sizes = np.bincount(labels.ravel())
    keep = np.flatnonzero(sizes >= int(min_size))
    keep = keep[keep != 0]   # drop background label
    return {"mask": np.isin(labels, keep)}


@register(
    name="fill_holes", category="morphology",
    inputs=[Port("mask", PortType.MASK)],
    outputs=[Port("mask", PortType.MASK)],
    doc="Fill enclosed holes inside objects (scipy.ndimage.binary_fill_holes).",
)
def fill_holes(mask):
    return {"mask": ndi.binary_fill_holes(np.asarray(mask, dtype=bool))}
