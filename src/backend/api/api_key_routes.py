"""API key management: GET/POST /api/keys, DELETE /api/keys/<key_id>.

Session-cookie authenticated (@login_required) — keys are minted from the
web UI by a logged-in user and used by MCP clients as Bearer tokens.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..core.auth import api_keys
from ..core.auth.middleware import get_current_user_id, login_required

api_key_bp = Blueprint("api_keys", __name__)

_ALLOWED_EXPIRY_DAYS = {None, 7, 30, 90, 365}


@api_key_bp.route("/keys", methods=["GET"])
@login_required
def list_keys():
    return jsonify(api_keys.list_keys(get_current_user_id())), 200


@api_key_bp.route("/keys", methods=["POST"])
@login_required
def create_key():
    """Body: {name, expires_in_days: null|7|30|90|365, scope: read|run|write
    (default write)}. The response is the only
    time the plaintext key is ever returned."""
    # Strict JSON (no force=True): a cross-site form post can't produce
    # application/json without a CORS preflight.
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"error": "JSON body required"}), 400
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    expires = data.get("expires_in_days")
    if expires not in _ALLOWED_EXPIRY_DAYS:
        return jsonify({"error": "expires_in_days must be null, 7, 30, 90 or 365"}), 400
    scope = data.get("scope", "write")
    if scope not in api_keys.SCOPES:
        return jsonify({"error": "scope must be read, run or write"}), 400
    return jsonify(api_keys.create_key(get_current_user_id(), name, expires, scope)), 201


@api_key_bp.route("/keys/<key_id>", methods=["DELETE"])
@login_required
def revoke_key(key_id: str):
    if not api_keys.revoke_key(key_id, get_current_user_id()):
        return jsonify({"error": "Key not found or already revoked"}), 404
    return "", 204
