"""Window CRUD routes: POST/GET/DELETE /api/windows."""

from __future__ import annotations

from flask import Blueprint, request, jsonify

from ..core.session.manager import get_manager

window_bp = Blueprint("windows", __name__)


@window_bp.route("/windows", methods=["POST"])
def create_window():
    """Create a new window. Accepts image_ids (list) or image_id (single)."""
    data = request.get_json(force=True)
    mode = data.get("mode", "llm").strip()

    # Support both single image_id and batch image_ids
    image_ids = data.get("image_ids", None)
    image_id = data.get("image_id", "").strip()
    if image_ids and isinstance(image_ids, list):
        image_ids = [str(i).strip() for i in image_ids if i]
        image_id = image_ids[0]
    elif image_id:
        image_ids = [image_id]
    else:
        return jsonify({"error": "image_id or image_ids is required"}), 400

    if mode not in ("llm", "manual"):
        return jsonify({"error": "mode must be 'llm' or 'manual'"}), 400

    window = get_manager().create_window(image_id=image_id, image_ids=image_ids, mode=mode)
    return jsonify({"window_id": window.window_id}), 201


@window_bp.route("/windows", methods=["GET"])
def list_windows():
    """List all active windows (summaries)."""
    windows = get_manager().list_windows()
    return jsonify(windows), 200


@window_bp.route("/windows/<window_id>", methods=["GET"])
def get_window(window_id: str):
    """Get a single window's info."""
    window = get_manager().get_window(window_id)
    if not window:
        return jsonify({"error": "Window not found"}), 404
    return jsonify(window.to_dict()), 200


@window_bp.route("/windows/<window_id>", methods=["DELETE"])
def delete_window(window_id: str):
    """Delete a window (in-memory only; files retained on disk)."""
    removed = get_manager().delete_window(window_id)
    if not removed:
        return jsonify({"error": "Window not found"}), 404
    return "", 204


@window_bp.route("/windows/<window_id>/outputs", methods=["GET"])
def list_outputs(window_id: str):
    """List all GeneratedImages for a window."""
    window = get_manager().get_window(window_id)
    if not window:
        return jsonify({"error": "Window not found"}), 404

    from pathlib import Path
    from ..config import config

    result = []
    for o in window.outputs:
        d = o.to_dict()
        # Build a browser-accessible preview_url from the filesystem preview_path
        try:
            rel = Path(o.preview_path).relative_to(config.OUTPUTS_DIR)
            d["preview_url"] = f"/outputs/{rel.as_posix()}"
        except (ValueError, TypeError):
            d["preview_url"] = None
        result.append(d)

    return jsonify(result), 200


@window_bp.route("/windows/<window_id>/history", methods=["GET"])
def get_history(window_id: str):
    """Get full conversation and metadata for a window."""
    window = get_manager().get_window(window_id)
    if not window:
        return jsonify({"error": "Window not found"}), 404

    # Resolve image metadata for the frontend to reconstruct the tab
    from .upload_routes import get_image_registry
    from pathlib import Path
    from ..config import config
    
    image_registry = get_image_registry()
    
    images_data = []
    for i_id in window.image_ids:
        img_meta = image_registry.get(i_id)
        if not img_meta:
            continue
            
        p = Path(img_meta.preview_path)
        try:
            rel = p.relative_to(config.UPLOADS_DIR)
            preview_url = f"/previews/uploads/{rel.as_posix()}"
        except ValueError:
            preview_url = None
            
        images_data.append({
            "image_id": i_id,
            "metadata": img_meta.to_dict(),
            "preview_url": preview_url
        })
        
    # Primary image is the first one
    primary = images_data[0] if images_data else None

    data = {
        "mode": window.mode,
        "image_id": window.image_id,
        "image_ids": window.image_ids,
        "images": images_data,
        "metadata": primary["metadata"] if primary else None,
        "preview_url": primary["preview_url"] if primary else None,
        "llm_conversation": [m.to_dict() for m in window.llm_conversation],
        "current_code": window.current_code,
        "is_shared": window.is_shared,
        "share_token": window.share_token,
    }
    return jsonify(data), 200


@window_bp.route("/windows/<window_id>/share", methods=["POST"])
def share_window(window_id: str):
    token = get_manager().share_window(window_id)
    if not token:
        return jsonify({"error": "Window not found"}), 404
    return jsonify({"share_token": token, "url": f"/shared/{token}"}), 200


@window_bp.route("/windows/<window_id>/share", methods=["DELETE"])
def unshare_window(window_id: str):
    success = get_manager().unshare_window(window_id)
    if not success:
        return jsonify({"error": "Window not found"}), 404
    return "", 204
