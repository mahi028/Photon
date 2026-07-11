"""Image metadata extraction — supports any channel count and dtype."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import numpy as np

from ...models.dto import ImageMetadata


def _load_array(path: Path) -> np.ndarray:
    """Load an image file into a numpy array, handling all supported formats."""
    ext = path.suffix.lower()

    if ext == ".npy":
        return np.load(str(path), allow_pickle=False)

    if ext in (".tif", ".tiff"):
        try:
            import tifffile
            return tifffile.imread(str(path))
        except Exception:
            pass  # fall through to Pillow

    # Standard formats via Pillow
    from PIL import Image as PILImage
    img = PILImage.open(str(path))
    # Preserve mode — don't force RGB conversion
    arr = np.array(img)
    return arr


def _guess_kind(channel_count: int, dtype: str) -> str:
    """Guess the semantic type of image from channel count."""
    if channel_count == 1:
        return "grayscale"
    if channel_count == 3:
        return "rgb"
    if channel_count == 4:
        return "rgba"
    return "multichannel"


def format_metadata_block(metadata: ImageMetadata) -> str:
    """Format metadata as a pretty-printed JSON string for display.

    Used in BOTH the LLM system prompt and the UI metadata block —
    single source of truth for key names and values.
    """
    import json
    return json.dumps(metadata.to_dict(), indent=2)


def extract_metadata(
    path: Path,
    image_id: str,
    original_filename: str,
    preview_path: str,
) -> ImageMetadata:
    """Load image at path and compute full ImageMetadata.

    Args:
        path: Absolute path to the saved image file.
        image_id: UUID assigned to this upload.
        original_filename: Original filename from the user.
        preview_path: Path to the downsampled preview image.

    Returns:
        Populated ImageMetadata dataclass.
    """
    arr = _load_array(path)

    # Normalize shape to always be at least 2D
    if arr.ndim == 2:
        shape = arr.shape  # (H, W)
        channel_count = 1
    elif arr.ndim == 3:
        shape = arr.shape  # (H, W, C)
        channel_count = arr.shape[2]
    else:
        # Edge case: 1D or 4D+ arrays from exotic scientific data
        shape = arr.shape
        channel_count = arr.shape[-1] if arr.ndim > 2 else 1

    dtype = str(arr.dtype)
    size_bytes = path.stat().st_size
    size_mb = size_bytes / (1024 * 1024)
    guessed_kind = _guess_kind(channel_count, dtype)

    # Per-image value range (observed min/max)
    arr_float = arr.astype(np.float64)
    value_range = (float(arr_float.min()), float(arr_float.max()))

    return ImageMetadata(
        image_id=image_id,
        original_filename=original_filename,
        shape=tuple(int(s) for s in shape),
        dtype=dtype,
        channel_count=channel_count,
        size_bytes=size_bytes,
        size_mb=size_mb,
        guessed_kind=guessed_kind,
        value_range=value_range,
        path=str(path),
        preview_path=preview_path,
    )
