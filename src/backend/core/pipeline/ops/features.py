"""Feature-map ops.

MIND (Modality Independent Neighbourhood Descriptor) — Heinrich et al., 2012.
Classical, model-free: describes each pixel by how self-similar its local
neighbourhood is in several directions, which makes it robust to contrast/
modality differences. No library ships it, so this is a real implementation
(everything else in the registry is a thin wrapper).
"""

from __future__ import annotations

import numpy as np
from scipy import ndimage as ndi

from ..registry import Param, Port, register
from ..types import PortType

# Six-neighbourhood offsets (dy, dx) used as the descriptor's search pattern.
_OFFSETS = [(0, 1), (0, -1), (1, 0), (-1, 0), (1, 1), (-1, -1)]


@register(
    name="mind_descriptor", category="features",
    inputs=[Port("image", PortType.IMAGE_GRAY)],
    outputs=[Port("features", PortType.FEATURE_MAP),
             Port("saliency", PortType.FLOAT_MAP)],
    params=[Param("sigma", "float", 0.5, min=0.1, max=5.0,
                  doc="Gaussian smoothing applied to patch-distance maps."),
            Param("patch_radius", "int", 1, min=1, max=5,
                  doc="Half-size of the box-filter patch used for SSD.")],
    doc="MIND self-similarity descriptor (Heinrich 2012): 6-channel FEATURE_MAP "
        "plus a scalar saliency map (descriptor variance). Contrast/modality invariant.",
)
def mind_descriptor(image, sigma=0.5, patch_radius=1):
    img = np.asarray(image, dtype=np.float64)
    if img.max() > 1.0:
        img = img / 255.0

    size = 2 * int(patch_radius) + 1

    # Patch-wise SSD to each shifted copy = the descriptor's raw distances.
    dists = []
    for dy, dx in _OFFSETS:
        shifted = ndi.shift(img, (dy, dx), order=1, mode="nearest")
        diff2 = (img - shifted) ** 2
        patch_ssd = ndi.uniform_filter(diff2, size=size)
        dists.append(ndi.gaussian_filter(patch_ssd, sigma=float(sigma)))

    dist = np.stack(dists, axis=-1)                     # (H, W, 6)

    # Variance estimate V(x): mean patch distance, floored to stay stable in
    # flat regions (the original paper clamps to a fraction of the mean).
    var = dist.mean(axis=-1, keepdims=True)
    floor = max(float(var.mean()) * 1e-3, 1e-9)
    var = np.clip(var, floor, None)

    mind = np.exp(-dist / var)
    # Normalize each pixel's descriptor by its max component (paper convention).
    mind = mind / np.clip(mind.max(axis=-1, keepdims=True), 1e-12, None)

    return {"features": mind, "saliency": mind.var(axis=-1)}


@register(
    name="feature_channel", category="features",
    inputs=[Port("features", PortType.FEATURE_MAP)],
    outputs=[Port("map", PortType.FLOAT_MAP)],
    params=[Param("channel", "int", 0, min=0, max=4095,
                  doc="Channel index to extract (clamped to the available range).")],
    doc="Extract a single channel from a FEATURE_MAP as a viewable float map.",
)
def feature_channel(features, channel=0):
    arr = np.asarray(features)
    idx = min(max(0, int(channel)), arr.shape[-1] - 1)
    return {"map": arr[:, :, idx]}


@register(
    name="feature_mean", category="features",
    inputs=[Port("features", PortType.FEATURE_MAP)],
    outputs=[Port("map", PortType.FLOAT_MAP)],
    doc="Collapse a FEATURE_MAP to a single map by averaging over channels.",
)
def feature_mean(features):
    return {"map": np.asarray(features).mean(axis=-1)}
