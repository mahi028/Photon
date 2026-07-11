"""Download route: POST /api/download — zip of selected full-res output images."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from flask import Blueprint, request, jsonify, send_file

from ..core.session.manager import get_manager

download_bp = Blueprint("download", __name__)


@download_bp.route("/download", methods=["POST"])
def download_zip():
    """Download a zip of selected full-res output images.

    Request: {"image_ids": ["uuid", ...]}
    Response: application/zip streamed binary.
    """
    data = request.get_json(force=True)
    image_ids_requested = set(data.get("image_ids", []))

    if not image_ids_requested:
        return jsonify({"error": "image_ids list is empty"}), 400

    # Collect all GeneratedImages across all windows
    # list_windows() returns summaries (dicts), so we must hydrate each one
    all_outputs = {}
    manager = get_manager()
    for summary in manager.list_windows():
        window = manager.get_window(summary["window_id"])
        if not window:
            continue
        for output in window.outputs:
            all_outputs[output.image_id] = output

    zip_buffer = io.BytesIO()
    found_any = False

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for image_id in image_ids_requested:
            output = all_outputs.get(image_id)
            if not output:
                continue
            path = Path(output.path)
            if not path.is_file():
                continue
            arcname = f"{output.description}_{image_id[:8]}{path.suffix}"
            zf.write(path, arcname=arcname)
            found_any = True

    if not found_any:
        return jsonify({"error": "No matching files found for the given image_ids"}), 404

    zip_buffer.seek(0)
    return send_file(
        zip_buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name="feature_explorer_outputs.zip",
    )
