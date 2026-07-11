"""Thumbnail generation — downsample any image to a web-safe preview."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image as PILImage

from ...config import config


def _array_to_pil_rgb(arr: np.ndarray) -> PILImage.Image:
    """Convert a numpy array of any shape/dtype to an 8-bit RGB PIL image.

    For grayscale (2D) arrays → replicate to 3 channels.
    For RGBA (H,W,4) → drop alpha for preview.
    For multichannel (C>4) → call composite module.
    """
    if arr.ndim == 2:
        # Grayscale
        arr_norm = _normalize_to_uint8(arr)
        return PILImage.fromarray(arr_norm, mode="L").convert("RGB")

    if arr.ndim == 3:
        c = arr.shape[2]
        if c == 1:
            arr_norm = _normalize_to_uint8(arr[:, :, 0])
            return PILImage.fromarray(arr_norm, mode="L").convert("RGB")
        if c == 3:
            arr_norm = _normalize_to_uint8(arr)
            return PILImage.fromarray(arr_norm, mode="RGB")
        if c == 4:
            arr_norm = _normalize_to_uint8(arr[:, :, :3])
            return PILImage.fromarray(arr_norm, mode="RGB")
        # c > 4 — use composite
        from .composite import make_composite_preview
        return make_composite_preview(arr)

    # Fallback for exotic shapes
    flat = arr.reshape(arr.shape[0], arr.shape[1], -1)
    return _array_to_pil_rgb(flat[:, :, :3] if flat.shape[2] >= 3 else flat[:, :, 0:1])


def _normalize_to_uint8(arr: np.ndarray) -> np.ndarray:
    """Scale array to uint8 [0, 255] preserving relative values."""
    arr = arr.astype(np.float64)
    mn, mx = arr.min(), arr.max()
    if mx == mn:
        return np.zeros_like(arr, dtype=np.uint8)
    arr = (arr - mn) / (mx - mn) * 255.0
    return arr.astype(np.uint8)


def generate_thumbnail(
    source_path: Path,
    output_path: Path,
    max_long_edge: int = config.PREVIEW_MAX_LONG_EDGE,
) -> Path:
    """Generate a downsampled JPEG preview of any image.

    Args:
        source_path: Path to the source image file.
        output_path: Path where the thumbnail should be saved (.jpg).
        max_long_edge: Maximum length of the longest dimension in pixels.

    Returns:
        output_path (for chaining / confirmation).
    """
    # Load raw array
    ext = source_path.suffix.lower()
    if ext == ".npy":
        arr = np.load(str(source_path), allow_pickle=False)
    elif ext in (".tif", ".tiff"):
        try:
            import tifffile
            arr = tifffile.imread(str(source_path))
        except Exception:
            arr = np.array(PILImage.open(str(source_path)))
    else:
        arr = np.array(PILImage.open(str(source_path)))

    img_pil = _array_to_pil_rgb(arr)

    # Resize preserving aspect ratio
    w, h = img_pil.size
    long_edge = max(w, h)
    if long_edge > max_long_edge:
        scale = max_long_edge / long_edge
        new_w = max(1, int(w * scale))
        new_h = max(1, int(h * scale))
        img_pil = img_pil.resize((new_w, new_h), PILImage.LANCZOS)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    img_pil.save(str(output_path), format="JPEG", quality=85, optimize=True)
    return output_path
