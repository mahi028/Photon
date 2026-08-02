"""Download route: POST /api/sessions/<session_id>/download — zip of a single
session's output images (D-020, session-scoped download).
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from flask import Blueprint, request, jsonify, send_file

from ..core.auth.middleware import login_required, get_current_user_id
from ..core.session.manager import get_manager

download_bp = Blueprint("download", __name__)


@download_bp.route("/sessions/<session_id>/download", methods=["POST"])
@login_required
def download_zip(session_id: str):
    """Download a zip of selected full-res output images from ONE session. Owner only.

    Request: {"image_ids": ["uuid", ...]}  (omit or empty → all of this session's outputs)
    Response: application/zip streamed binary.
    """
    sess = get_manager().get_session_for_user(session_id, get_current_user_id())
    if not sess:
        return jsonify({"error": "Session not found"}), 404

    data = request.get_json(silent=True) or {}
    requested = set(data.get("image_ids") or [])

    outputs_by_id = {o.image_id: o for o in sess.outputs}
    image_ids = requested & outputs_by_id.keys() if requested else set(outputs_by_id.keys())

    if not image_ids:
        return jsonify({"error": "No matching outputs in this session"}), 404

    zip_buffer = io.BytesIO()
    found_any = False

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for image_id in image_ids:
            output = outputs_by_id[image_id]
            path = Path(output.path)
            if not path.is_file():
                continue
            arcname = f"{output.description}_{image_id[:8]}{path.suffix}"
            zf.write(path, arcname=arcname)
            found_any = True

    if not found_any:
        return jsonify({"error": "No matching files found on disk for the given image_ids"}), 404

    zip_buffer.seek(0)
    return send_file(
        zip_buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"session_{session_id[:8]}_outputs.zip",
    )
