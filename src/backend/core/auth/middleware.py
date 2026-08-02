"""Auth middleware: login_required decorator and current_user helper."""

from __future__ import annotations

from functools import wraps

from flask import jsonify, redirect, request, session, url_for


def get_current_user_id() -> str | None:
    """Return user_id from Flask session, or None."""
    return session.get("user_id")


def login_required(f):
    """Decorator that enforces authentication.

    API routes → 401 JSON. Page routes → redirect to login.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if not get_current_user_id():
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "Authentication required"}), 401
            return redirect(url_for("auth.login_page"))
        return f(*args, **kwargs)
    return decorated
