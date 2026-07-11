"""Upload route: POST /api/upload — multipart file or {url}."""

from __future__ import annotations

import uuid
from pathlib import Path

import requests
from flask import Blueprint, request, jsonify, current_app

from ..config import config
from ..core.image.metadata import extract_metadata
from ..core.image.thumbnail import generate_thumbnail
from ..utils.validation import validate_extension, validate_size

upload_bp = Blueprint("upload", __name__)

# In-memory image registry: image_id -> ImageMetadata
# Backed by SQLite — restored on startup automatically.
_image_registry: dict = {}

def get_image_registry() -> dict:
    return _image_registry

def _restore_image_registry() -> None:
    """Load image metadata from SQLite into the in-memory registry on startup."""
    from ..core.session.store import load_all_images, init_db
    init_db()
    _image_registry.update(load_all_images())

# Restore immediately when this module is first imported
_restore_image_registry()


@upload_bp.route("/upload", methods=["POST"])
def upload_image():
    """Accept a multipart file or a JSON {url} field and process the upload."""
    image_id = uuid.uuid4().hex

    # --- Determine source ---
    if "file" in request.files:
        f = request.files["file"]
        original_filename = f.filename or "upload"
        ok, err = validate_extension(original_filename)
        if not ok:
            return jsonify({"error": err}), 400

        file_bytes = f.read()
        ok, err = validate_size(len(file_bytes))
        if not ok:
            return jsonify({"error": err}), 400

    elif request.is_json and "url" in request.json:
        url = request.json["url"]
        try:
            resp = requests.get(url, timeout=30, stream=True)
            resp.raise_for_status()
        except Exception as e:
            return jsonify({"error": f"Failed to fetch URL: {e}"}), 400

        # Derive filename from URL
        original_filename = url.rstrip("/").split("/")[-1] or "download"
        ok, err = validate_extension(original_filename)
        if not ok:
            return jsonify({"error": err}), 400

        file_bytes = resp.content
        ok, err = validate_size(len(file_bytes))
        if not ok:
            return jsonify({"error": err}), 400

    else:
        return jsonify({"error": "Provide either 'file' (multipart) or 'url' (JSON)."}), 400

    # --- Save original ---
    ext = Path(original_filename).suffix.lower()
    save_path = config.UPLOADS_DIR / f"{image_id}{ext}"
    save_path.write_bytes(file_bytes)

    # --- Generate preview ---
    preview_path = config.UPLOADS_DIR / f"{image_id}_preview.jpg"
    try:
        generate_thumbnail(save_path, preview_path)
    except Exception as e:
        save_path.unlink(missing_ok=True)
        return jsonify({"error": f"Could not read image: {e}"}), 422

    preview_url = f"/previews/uploads/{image_id}_preview.jpg"

    # --- Extract metadata ---
    try:
        metadata = extract_metadata(
            path=save_path,
            image_id=image_id,
            original_filename=original_filename,
            preview_path=str(preview_path),
        )
    except Exception as e:
        save_path.unlink(missing_ok=True)
        preview_path.unlink(missing_ok=True)
        return jsonify({"error": f"Metadata extraction failed: {e}"}), 422

    _image_registry[image_id] = metadata

    # Persist to SQLite so the registry survives server restarts
    from ..core.session.store import save_image
    save_image(metadata)

    return jsonify({
        "image_id": image_id,
        "metadata": metadata.to_dict(),
        "preview_url": preview_url,
    }), 200


@upload_bp.route("/images/<image_id>", methods=["GET"])
def get_image_metadata(image_id: str):
    """Return metadata for a previously uploaded image."""
    meta = _image_registry.get(image_id)
    if not meta:
        return jsonify({"error": "Image not found"}), 404
    return jsonify(meta.to_dict()), 200
