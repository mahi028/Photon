"""Example code API route: GET /api/examples/code."""

from __future__ import annotations

import inspect
from pathlib import Path
from flask import Blueprint, jsonify

example_bp = Blueprint("examples", __name__)

_EXPLANATION = """## Function Contract

Every code submission (LLM or manual) must define exactly this function:

```python
def main(input_path: str, output_path_dir: str) -> str:
    ...
```

- **input_path**: absolute path to the uploaded image (read-only).
- **output_path_dir**: directory where you must save your output file.
- **Return value**: the full path to the saved file (str).
- **Extension rule**: use `.png`/`.jpg` for ≤4 channels; `.npy` or `.tiff` for >4 channels.
- **Normalize**: always normalize data to uint8 [0, 255] before saving as PNG/JPEG.

Available libraries: numpy, PIL/Pillow, cv2, scipy, skimage, tifffile.
Standard library: os, pathlib, math, json, re, io.
"""


@example_bp.route("/examples/code", methods=["GET"])
def get_example_code():
    """Return the canonical example code and contract explanation."""
    example_path = Path(__file__).parent.parent / "examples" / "example_code.py"
    try:
        code = example_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return jsonify({"error": "Example code file not found"}), 500

    return jsonify({
        "code": code,
        "explanation": _EXPLANATION,
    }), 200
