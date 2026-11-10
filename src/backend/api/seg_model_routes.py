"""Uploaded segmentation models (D-047).

  GET    /api/seg-models/options      architectures, normalisations, suggested encoders, limits
  GET    /api/seg-models              your models
  POST   /api/seg-models              multipart: weights=<file>, spec=<JSON>  -> 202, status "checking"
  GET    /api/seg-models/<id>         one model (poll status: checking -> ready | failed)
  DELETE /api/seg-models/<id>
"""

from __future__ import annotations

import json

from flask import Blueprint, jsonify, request

from ..config import config
from ..core.auth.middleware import get_current_user_id, login_required
from ..core.models import user_models
from ..core.session import store

seg_model_bp = Blueprint("seg_models", __name__)


@seg_model_bp.route("/seg-models/options", methods=["GET"])
@login_required
def options():
    return jsonify({
        "archs": list(user_models.ARCHS), "activations": list(user_models.ACTIVATIONS),
        "normalize": list(user_models.NORMALIZE), "colors": list(user_models.COLORS),
        "encoders": list(user_models.SUGGESTED_ENCODERS), "weight_exts": list(user_models.WEIGHT_EXTS),
        # browser uploads are also capped by the request limit (MAX_UPLOAD_MB)
        "max_mb": min(user_models.MAX_WEIGHTS_BYTES, config.MAX_UPLOAD_BYTES) >> 20,
    }), 200


@seg_model_bp.route("/seg-models", methods=["GET"])
@login_required
def list_models():
    return jsonify({"models": [user_models.public(r) for r in store.list_seg_models(get_current_user_id())]}), 200


@seg_model_bp.route("/seg-models", methods=["POST"])
@login_required
def upload():
    f = request.files.get("weights")
    if f is None:
        return jsonify({"error": "multipart field 'weights' (the checkpoint file) is required"}), 400
    try:
        spec = json.loads(request.form.get("spec") or "{}")
    except ValueError:
        return jsonify({"error": "spec must be JSON"}), 400
    try:
        row = user_models.create(get_current_user_id(), spec, f.filename, f.stream)
    except user_models.SpecError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(row), 202


@seg_model_bp.route("/seg-models/<model_id>", methods=["GET"])
@login_required
def get_model(model_id: str):
    row = store.get_seg_model(model_id)
    if not row or row["user_id"] != get_current_user_id() or row["deleted_at"]:
        return jsonify({"error": "Model not found"}), 404
    return jsonify(user_models.public(row)), 200


@seg_model_bp.route("/seg-models/<model_id>", methods=["DELETE"])
@login_required
def delete_model(model_id: str):
    if not user_models.delete(get_current_user_id(), model_id):
        return jsonify({"error": "Model not found"}), 404
    return "", 204
