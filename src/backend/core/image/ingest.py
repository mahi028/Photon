"""Image ingest: validate raw bytes, save the original, build preview + metadata.

Shared by POST /api/upload and the MCP server's upload_image tool so both
entry points apply the same extension/size checks and persist identically.
No HTTP here.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from ...config import config
from ...models.dto import ImageMetadata
from ...utils.validation import validate_extension, validate_size
from .metadata import extract_metadata
from .thumbnail import generate_thumbnail


class IngestError(Exception):
    """Upload rejected. `status` mirrors the HTTP code the upload route returns."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def ingest_image_bytes(file_bytes: bytes, original_filename: str) -> ImageMetadata:
    """Validate, save, preview and register one image. Returns its metadata.

    The caller is responsible for adding the result to the in-memory image
    registry (api/upload_routes.py) if it keeps one; SQLite is written here.
    """
    ok, err = validate_extension(original_filename)
    if not ok:
        raise IngestError(err, 400)
    ok, err = validate_size(len(file_bytes))
    if not ok:
        raise IngestError(err, 400)

    image_id = uuid.uuid4().hex
    ext = Path(original_filename).suffix.lower()
    save_path = config.UPLOADS_DIR / f"{image_id}{ext}"
    save_path.write_bytes(file_bytes)

    preview_path = config.UPLOADS_DIR / f"{image_id}_preview.jpg"
    try:
        generate_thumbnail(save_path, preview_path)
    except Exception as e:
        save_path.unlink(missing_ok=True)
        raise IngestError(f"Could not read image: {e}", 422) from e

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
        raise IngestError(f"Metadata extraction failed: {e}", 422) from e

    from ..session.store import save_image
    save_image(metadata)
    return metadata
