"""API keys: per-user bearer tokens for programmatic access (the MCP server).

Format: ``pk_<43 url-safe chars>`` (32 random bytes). Only the SHA-256 hex
digest is stored; the plaintext is returned exactly once, by create_key().
A plain hash (not bcrypt) is right here: keys are high-entropy random, so
there is nothing to brute-force, and verification must be cheap because it
runs on every MCP request.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from ..session import store

KEY_PREFIX = "pk_"
_DISPLAY_PREFIX_LEN = 10          # "pk_" + 7 chars — enough to tell keys apart
_TOUCH_INTERVAL = timedelta(minutes=1)
MAX_NAME_LEN = 80


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def create_key(user_id: str, name: str, expires_in_days: Optional[int] = None) -> dict[str, Any]:
    """Mint a key. The returned dict holds the plaintext under 'key' — the only
    time it exists outside the caller's hands."""
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    key_id = uuid.uuid4().hex
    now = datetime.utcnow()
    expires_at = (now + timedelta(days=expires_in_days)).isoformat() if expires_in_days else None
    name = (name or "").strip()[:MAX_NAME_LEN] or "Untitled key"
    store.create_api_key(key_id, user_id, name, _hash(key), key[:_DISPLAY_PREFIX_LEN],
                         now.isoformat(), expires_at)
    return {"key_id": key_id, "key": key, "name": name, "prefix": key[:_DISPLAY_PREFIX_LEN],
            "created_at": now.isoformat(), "expires_at": expires_at}


def list_keys(user_id: str) -> list[dict[str, Any]]:
    now = datetime.utcnow().isoformat()
    out = []
    for k in store.list_api_keys(user_id):
        if k["revoked_at"]:
            status = "revoked"
        elif k["expires_at"] and k["expires_at"] <= now:
            status = "expired"
        else:
            status = "active"
        out.append({**k, "status": status})
    return out


def revoke_key(key_id: str, user_id: str) -> bool:
    return store.revoke_api_key(key_id, user_id, datetime.utcnow().isoformat())


def verify_key(key: str) -> Optional[dict[str, Any]]:
    """Return the key row for a valid (known, unrevoked, unexpired) key, else None.

    Updates last_used_at at most once per minute per key to avoid a write on
    every request.
    """
    if not key or not key.startswith(KEY_PREFIX):
        return None
    row = store.find_api_key_by_hash(_hash(key))
    if not row or row["revoked_at"]:
        return None
    now = datetime.utcnow()
    if row["expires_at"] and row["expires_at"] <= now.isoformat():
        return None
    last = row["last_used_at"]
    if not last or datetime.fromisoformat(last) < now - _TOUCH_INTERVAL:
        store.touch_api_key(row["key_id"], now.isoformat())
    return row
