"""Upload route: POST /api/upload — multipart file or {url}."""

from __future__ import annotations

from flask import Blueprint, request, jsonify

from ..core.auth.middleware import login_required
from ..core.image.fetch import FetchError, fetch_url
from ..core.image.ingest import IngestError, ingest_image_bytes

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
@login_required
def upload_image():
    """Accept a multipart file or a JSON {url} field and process the upload."""
    # --- Determine source ---
    if "file" in request.files:
        f = request.files["file"]
        original_filename = f.filename or "upload"
        file_bytes = f.read()

    elif request.is_json and "url" in request.json:
        try:
            file_bytes, original_filename = fetch_url(str(request.json["url"]))
        except FetchError as e:
            return jsonify({"error": str(e)}), 400

    else:
        return jsonify({"error": "Provide either 'file' (multipart) or 'url' (JSON)."}), 400

    try:
        metadata = ingest_image_bytes(file_bytes, original_filename)
    except IngestError as e:
        return jsonify({"error": str(e)}), e.status

    _image_registry[metadata.image_id] = metadata

    return jsonify({
        "image_id": metadata.image_id,
        "metadata": metadata.to_dict(),
        "preview_url": f"/previews/uploads/{metadata.image_id}_preview.jpg",
    }), 200


@upload_bp.route("/images/<image_id>", methods=["GET"])
@login_required
def get_image_metadata(image_id: str):
    """Return metadata for a previously uploaded image."""
    meta = _image_registry.get(image_id)
    if not meta:
        return jsonify({"error": "Image not found"}), 404
    return jsonify(meta.to_dict()), 200
