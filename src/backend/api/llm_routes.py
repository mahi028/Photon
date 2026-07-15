"""LLM provider info + runtime model selection routes.

GET  /api/llm/info   — active provider, active model, and (openrouter only)
                       the list of models available for the configured API key.
POST /api/llm/model  — switch the active OpenRouter model at runtime.

Model list is fetched from GET {OPENROUTER_BASE_URL}/models with the API key
and cached in memory (TTL below) — OpenRouter serves the catalog scoped to the
key's permissions.
"""

from __future__ import annotations

import logging
import threading
import time

import requests
from flask import Blueprint, jsonify, request

from ..config import config
from ..core.llm.client import get_openrouter_model, set_openrouter_model

llm_bp = Blueprint("llm", __name__)
logger = logging.getLogger(__name__)

_MODELS_CACHE_TTL_SECONDS = 3600
_models_cache: dict = {"ids": None, "fetched_at": 0.0}
_models_lock = threading.Lock()


def _fetch_openrouter_models() -> list[str]:
    """Return cached OpenRouter model ids, refreshing if stale."""
    with _models_lock:
        age = time.time() - _models_cache["fetched_at"]
        if _models_cache["ids"] is not None and age < _MODELS_CACHE_TTL_SECONDS:
            return _models_cache["ids"]

        headers = {}
        if config.OPENROUTER_API_KEY:
            headers["Authorization"] = f"Bearer {config.OPENROUTER_API_KEY}"
        resp = requests.get(
            f"{config.OPENROUTER_BASE_URL}/models", headers=headers, timeout=15
        )
        resp.raise_for_status()
        ids = sorted(m["id"] for m in resp.json().get("data", []))
        _models_cache["ids"] = ids
        _models_cache["fetched_at"] = time.time()
        return ids


@llm_bp.route("/llm/info", methods=["GET"])
def llm_info():
    """Active provider/model; includes selectable model list for openrouter."""
    provider = config.LLM_PROVIDER.lower()
    info = {
        "provider": provider,
        "model": get_openrouter_model() if provider == "openrouter" else config.active_model,
        "model_selectable": provider == "openrouter",
        "models": [],
    }
    if provider == "openrouter":
        try:
            info["models"] = _fetch_openrouter_models()
        except Exception as e:
            logger.error("OpenRouter model list fetch failed: %s", e)
            info["models_error"] = str(e)
    return jsonify(info), 200


@llm_bp.route("/llm/model", methods=["POST"])
def set_model():
    """Switch the active OpenRouter model (runtime only; resets on restart)."""
    if config.LLM_PROVIDER.lower() != "openrouter":
        return jsonify({"error": "Model selection is only available for the openrouter provider"}), 400

    data = request.get_json(force=True)
    model = (data.get("model") or "").strip()
    if not model:
        return jsonify({"error": "model is required"}), 400

    try:
        available = _fetch_openrouter_models()
    except Exception as e:
        return jsonify({"error": f"Could not validate model against OpenRouter catalog: {e}"}), 502
    if model not in available:
        return jsonify({"error": f"Unknown OpenRouter model: {model}"}), 400

    set_openrouter_model(model)
    return jsonify({"status": "ok", "model": model}), 200
