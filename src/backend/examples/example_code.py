"""Canonical example code shown in the 'View Example Code' dialog.

This file is a REAL, TESTED script that runs successfully through the sandbox.
It demonstrates the function contract: main(input_path, output_path_dir) -> str.

Example: Convert image to grayscale and normalize to [0, 255] uint8.
Works for RGB, RGBA, grayscale, and multichannel inputs.
"""

import os
from pathlib import Path

import numpy as np
from PIL import Image


def main(input_path: str, output_path_dir: str) -> str:
    """Convert image to grayscale and save as PNG.

    For multichannel images (C > 3), mean-projects all channels to a single
    grayscale channel. For grayscale input, passes through unchanged.
    Normalizes to uint8 [0, 255] regardless of input dtype.

    Args:
        input_path: Path to the input image file.
        output_path_dir: Directory where the output file should be saved.

    Returns:
        Full path to the saved output file.
    """
    # --- Load ---
    ext = Path(input_path).suffix.lower()
    if ext == ".npy":
        arr = np.load(input_path, allow_pickle=False).astype(np.float64)
    else:
        arr = np.array(Image.open(input_path)).astype(np.float64)

    # --- Convert to grayscale ---
    if arr.ndim == 2:
        gray = arr
    elif arr.ndim == 3:
        # Mean across channels — works for RGB, RGBA, multichannel
        gray = arr.mean(axis=2)
    else:
        raise ValueError(f"Unexpected array ndim: {arr.ndim}")

    # --- Normalize to uint8 ---
    mn, mx = gray.min(), gray.max()
    if mx > mn:
        gray = (gray - mn) / (mx - mn) * 255.0
    else:
        gray = np.zeros_like(gray)

    gray_uint8 = gray.astype(np.uint8)

    # --- Save ---
    out_path = Path(output_path_dir) / "grayscale_output.png"
    Image.fromarray(gray_uint8, mode="L").save(str(out_path))

    return str(out_path)
