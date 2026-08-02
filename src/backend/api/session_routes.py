"""Session CRUD routes: POST/GET/DELETE /api/sessions/*.

Replaces window_routes.py — a Session is user-owned and unifies the old
mode-locked Window (LLM chat + manual editor now coexist in one session,
toggled in the UI — see DECISIONS D-018). All routes are user-scoped via
@login_required + get_current_user_id().
"""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, request, jsonify

from ..config import config
from ..core.auth.middleware import login_required, get_current_user_id
from ..core.session.manager import get_manager

session_bp = Blueprint("sessions", __name__)


@session_bp.route("/sessions", methods=["POST"])
@login_required
def create_session():
    """Create a new session. Accepts image_ids (list) or image_id (single), optional title."""
    data = request.get_json(force=True)
    user_id = get_current_user_id()

    image_ids = data.get("image_ids", None)
    image_id = (data.get("image_id") or "").strip()
    if image_ids and isinstance(image_ids, list):
        image_ids = [str(i).strip() for i in image_ids if i]
    elif image_id:
        image_ids = [image_id]
    else:
        image_ids = []

    title = (data.get("title") or "").strip() or "Untitled"

    sess = get_manager().create_session(user_id=user_id, image_ids=image_ids, title=title)
    return jsonify({"session_id": sess.session_id}), 201


@session_bp.route("/sessions", methods=["GET"])
@login_required
def list_sessions():
    """List the current user's sessions (summaries, sorted by updated_at desc)."""
    sessions = get_manager().list_sessions(get_current_user_id())
    return jsonify(sessions), 200


@session_bp.route("/sessions/<session_id>", methods=["GET"])
@login_required
def get_session(session_id: str):
    """Get a single session's info. Owner only."""
    sess = get_manager().get_session_for_user(session_id, get_current_user_id())
    if not sess:
        return jsonify({"error": "Session not found"}), 404
    return jsonify(sess.to_dict()), 200


@session_bp.route("/sessions/<session_id>", methods=["PATCH"])
@login_required
def update_session(session_id: str):
    """Update session title. Owner only."""
    data = request.get_json(force=True)
    title = data.get("title")
    if title is None:
        return jsonify({"error": "title is required"}), 400
    ok = get_manager().rename_session(session_id, get_current_user_id(), title)
    if not ok:
        return jsonify({"error": "Session not found"}), 404
    return jsonify({"status": "ok"}), 200


@session_bp.route("/sessions/<session_id>", methods=["DELETE"])
@login_required
def delete_session(session_id: str):
    """Smart delete (owner only, D-020): everything this session owns is
    deleted — generated outputs AND source images — except an image still
    referenced by another (sibling) session, which is kept. Always a full
    delete regardless of sharing (D-019: clones are independent of the
    original); any active share links for this session cascade-delete."""
    deleted_image_ids = get_manager().delete_session(session_id, get_current_user_id())
    if deleted_image_ids is None:
        return jsonify({"error": "Session not found"}), 404

    # Drop the deleted images from the in-memory registry (api-layer runtime
    # state — core/session/manager.py doesn't reach into api/, see D-019).
    from .upload_routes import get_image_registry
    registry = get_image_registry()
    for image_id in deleted_image_ids:
        registry.pop(image_id, None)

    return "", 204


@session_bp.route("/sessions/<session_id>/images", methods=["POST"])
@login_required
def add_image(session_id: str):
    """Attach an already-uploaded image to an existing session. Owner only."""
    sess = get_manager().get_session_for_user(session_id, get_current_user_id())
    if not sess:
        return jsonify({"error": "Session not found"}), 404
    data = request.get_json(force=True)
    image_id = (data.get("image_id") or "").strip()
    if not image_id:
        return jsonify({"error": "image_id is required"}), 400
    sess = get_manager().add_image(session_id, image_id)
    return jsonify(sess.to_dict()), 200


@session_bp.route("/sessions/<session_id>/images/<image_id>", methods=["DELETE"])
@login_required
def remove_image(session_id: str, image_id: str):
    """Detach an image from a session (uploaded file itself is untouched). Owner only."""
    sess = get_manager().get_session_for_user(session_id, get_current_user_id())
    if not sess:
        return jsonify({"error": "Session not found"}), 404
    sess = get_manager().remove_image(session_id, image_id)
    return jsonify(sess.to_dict()), 200


@session_bp.route("/sessions/<session_id>/outputs", methods=["GET"])
@login_required
def list_outputs(session_id: str):
    """List all GeneratedImages for a session. Owner only."""
    sess = get_manager().get_session_for_user(session_id, get_current_user_id())
    if not sess:
        return jsonify({"error": "Session not found"}), 404

    result = []
    for o in sess.outputs:
        d = o.to_dict()
        try:
            rel = Path(o.preview_path).relative_to(config.OUTPUTS_DIR)
            d["preview_url"] = f"/outputs/{rel.as_posix()}"
        except (ValueError, TypeError):
            d["preview_url"] = None
        result.append(d)

    return jsonify(result), 200


@session_bp.route("/sessions/<session_id>/history", methods=["GET"])
@login_required
def get_history(session_id: str):
    """Get full conversation and metadata for a session. Owner only."""
    sess = get_manager().get_session_for_user(session_id, get_current_user_id())
    if not sess:
        return jsonify({"error": "Session not found"}), 404

    from .upload_routes import get_image_registry

    image_registry = get_image_registry()

    images_data = []
    for i_id in sess.image_ids:
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

    primary = images_data[0] if images_data else None

    data = {
        "title": sess.title,
        "image_id": sess.image_id,
        "image_ids": sess.image_ids,
        "images": images_data,
        "metadata": primary["metadata"] if primary else None,
        "preview_url": primary["preview_url"] if primary else None,
        "llm_conversation": [m.to_dict() for m in sess.llm_conversation],
        "current_code": sess.current_code,
    }
    return jsonify(data), 200


@session_bp.route("/sessions/<session_id>/share", methods=["POST"])
@login_required
def share_session(session_id: str):
    """Create a new share link. Body: {"reusable": true|false} (default false —
    one-time). Opening the link deep-clones this session into the visitor's
    own account (see D-019) — it does not grant access to this session itself."""
    data = request.get_json(silent=True) or {}
    reusable = bool(data.get("reusable", False))
    token = get_manager().share_session(session_id, get_current_user_id(), reusable)
    if not token:
        return jsonify({"error": "Session not found"}), 404
    return jsonify({"token": token, "url": f"/shared/{token}", "reusable": reusable}), 200


@session_bp.route("/sessions/<session_id>/share", methods=["DELETE"])
@login_required
def unshare_session(session_id: str):
    """Revoke ALL active share links for this session. Already-created clones
    are unaffected — they're independent sessions."""
    success = get_manager().revoke_shares(session_id, get_current_user_id())
    if not success:
        return jsonify({"error": "Session not found"}), 404
    return "", 204
