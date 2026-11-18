"""Models (D-049): one API for every kind.

  GET    /api/models?kind=&status=   your models (kind sklearn | segmentation)
  GET    /api/models/options         what a segmentation upload may declare
  POST   /api/models                 upload a segmentation model: multipart weights=<file>,
                                     spec=<JSON>  -> 202, status "checking"
  GET    /api/models/<id>            one model (poll status: checking -> ready | failed)
  DELETE /api/models/<id>

scikit-learn models are created by runs ending in ml_save_model, not uploaded.
"""

from __future__ import annotations

import json

from flask import Blueprint, jsonify, request

from ..config import config
from ..core.auth.middleware import get_current_user_id, login_required
from ..core.models import records, user_models

model_bp = Blueprint("models", __name__)


@model_bp.route("/models/options", methods=["GET"])
@login_required
def options():
    return jsonify({
        "kinds": list(records.KINDS),
        "archs": list(user_models.ARCHS), "activations": list(user_models.ACTIVATIONS),
        "normalize": list(user_models.NORMALIZE), "colors": list(user_models.COLORS),
        "encoders": list(user_models.SUGGESTED_ENCODERS), "weight_exts": list(user_models.WEIGHT_EXTS),
        # browser uploads are also capped by the request limit (MAX_UPLOAD_MB)
        "max_mb": min(user_models.MAX_WEIGHTS_BYTES, config.MAX_UPLOAD_BYTES) >> 20,
    }), 200


@model_bp.route("/models", methods=["GET"])
@login_required
def list_models():
    kind = request.args.get("kind") or None
    if kind and kind not in records.KINDS:
        return jsonify({"error": f"kind must be one of {', '.join(records.KINDS)}"}), 400
    return jsonify({"models": records.list_for(get_current_user_id(), kind,
                                               request.args.get("status") or None)}), 200


@model_bp.route("/models", methods=["POST"])
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


@model_bp.route("/models/<model_id>", methods=["GET"])
@login_required
def get_model(model_id: str):
    row = records.get(model_id)
    if not row or row["user_id"] != get_current_user_id() or row["deleted_at"]:
        return jsonify({"error": "Model not found"}), 404
    return jsonify(records.public(row)), 200


@model_bp.route("/models/<model_id>", methods=["DELETE"])
@login_required
def delete_model(model_id: str):
    if not records.delete(get_current_user_id(), model_id):
        return jsonify({"error": "Model not found"}), 404
    return "", 204
