"""Saved models and painted labels (D-044).

  GET    /api/models                         your saved models
  DELETE /api/models/<id>
  GET    /api/annotations?image_id=          your label images (for one image)
  POST   /api/annotations                    {image_id, png (base64 or data: URL), name,
                                              classes, annotation_id?} create or replace
  GET    /api/annotations/<id>/image         the label PNG (0 = unlabeled)
  DELETE /api/annotations/<id>
"""

from __future__ import annotations

import base64
import binascii

from flask import Blueprint, jsonify, request, send_file

from ..core.auth.middleware import get_current_user_id, login_required
from ..core.pipeline import saved
from ..core.session import store

saved_bp = Blueprint("saved", __name__)


@saved_bp.route("/models", methods=["GET"])
@login_required
def list_models():
    return jsonify({"models": saved.list_models(get_current_user_id())}), 200


@saved_bp.route("/models/<model_id>", methods=["DELETE"])
@login_required
def delete_model(model_id: str):
    if not saved.delete_model(get_current_user_id(), model_id):
        return jsonify({"error": "Model not found"}), 404
    return "", 204


@saved_bp.route("/annotations", methods=["GET"])
@login_required
def list_annotations():
    image_id = request.args.get("image_id") or None
    return jsonify({"annotations": store.list_annotations(get_current_user_id(), image_id)}), 200


@saved_bp.route("/annotations", methods=["POST"])
@login_required
def save_annotation():
    data = request.get_json(silent=True)        # strict JSON: no cross-site form posts
    if not isinstance(data, dict):
        return jsonify({"error": "JSON body required"}), 400
    user_id = get_current_user_id()
    image_id = str(data.get("image_id") or "")
    from .upload_routes import get_image_registry
    meta = get_image_registry().get(image_id)
    if not meta or not store.user_can_access_image(user_id, image_id):
        return jsonify({"error": "Image not found"}), 404
    raw = str(data.get("png") or "")
    if raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]
    try:
        png = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return jsonify({"error": "png must be base64"}), 400
    try:
        row = saved.save_annotation(user_id, image_id, png, (meta.shape[1], meta.shape[0]),
                                    name=str(data.get("name") or ""), classes=data.get("classes"),
                                    annotation_id=data.get("annotation_id") or None)
    except saved.AssetError as e:
        return jsonify({"error": str(e)}), e.status
    return jsonify(row), 200 if data.get("annotation_id") else 201


@saved_bp.route("/annotations/<annotation_id>/image", methods=["GET"])
@login_required
def annotation_image(annotation_id: str):
    row = store.get_annotation(annotation_id)
    if not row or row["user_id"] != get_current_user_id():
        return jsonify({"error": "Annotation not found"}), 404
    resp = send_file(saved.annotation_path(annotation_id), mimetype="image/png", max_age=0)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@saved_bp.route("/annotations/<annotation_id>", methods=["DELETE"])
@login_required
def delete_annotation(annotation_id: str):
    if not saved.delete_annotation(get_current_user_id(), annotation_id):
        return jsonify({"error": "Annotation not found"}), 404
    return "", 204
