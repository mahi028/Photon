"""Composite preview generator for images with more than 4 channels.

Default strategy: mean projection across all channels → grayscale RGB.
This is purely for web preview — the actual saved output file keeps all channels.
"""

from __future__ import annotations

import numpy as np
from PIL import Image as PILImage


def make_composite_preview(arr: np.ndarray) -> PILImage.Image:
    """Create a viewable 3-channel RGB PIL Image from a C>4 channel array.

    Strategy: mean-project all channels to a single grayscale channel,
    then replicate to RGB. This is a documented, simple default that makes no
    semantic assumptions about channel meaning.

    Args:
        arr: numpy array of shape (H, W, C) where C > 4.

    Returns:
        PIL RGB image suitable for JPEG/PNG saving.
    """
    if arr.ndim == 2:
        # Already grayscale — shouldn't reach here normally
        gray = arr.astype(np.float64)
    elif arr.ndim == 3:
        # Mean across all channels → single channel grayscale
        gray = arr.astype(np.float64).mean(axis=2)
    else:
        raise ValueError(f"Unexpected array ndim={arr.ndim} in make_composite_preview")

    # Normalize to [0, 255] uint8
    mn, mx = gray.min(), gray.max()
    if mx > mn:
        gray = (gray - mn) / (mx - mn) * 255.0
    else:
        gray = np.zeros_like(gray)

    gray_uint8 = gray.astype(np.uint8)
    return PILImage.fromarray(gray_uint8, mode="L").convert("RGB")
