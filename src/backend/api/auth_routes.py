"""Authentication routes: username/password signup+login (always available),
Google OAuth login/callback (only when configured), logout.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from flask import Blueprint, abort, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from ..config import config
from ..core.auth.oauth import oauth
from ..core.auth.signup_policy import policy as signup_policy
from ..core.session import store

auth_bp = Blueprint("auth", __name__)

_MIN_PASSWORD_LENGTH = 8


@auth_bp.route("/auth/login")
def login_page():
    """Render the login/signup page. Google button shown only if enabled."""
    return render_template("login.html", google_oauth_enabled=config.GOOGLE_OAUTH_ENABLED,
                           signup=signup_policy.describe(), error=request.args.get("error", ""))


@auth_bp.route("/auth/config")
def auth_config():
    """Lets the frontend know whether to render the Google sign-in option."""
    return jsonify({"google_oauth_enabled": config.GOOGLE_OAUTH_ENABLED, **signup_policy.describe()})


@auth_bp.route("/auth/signup", methods=["POST"])
def signup():
    """Create a username+email+password account.

    No email verification yet (no email-sending service available) —
    email_verified stays 0 until an OTP-based flow exists.
    """
    data = request.get_json(force=True)
    username = (data.get("username") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not username or not email or len(password) < _MIN_PASSWORD_LENGTH:
        return jsonify({
            "error": f"username, email, and a password of at least "
                     f"{_MIN_PASSWORD_LENGTH} characters are required"
        }), 400
    denied = signup_policy.check_password_signup(email, data.get("invite_code"))
    if denied:
        return jsonify({"error": denied}), 403
    if store.find_user_by_username_or_email(username) or store.find_user_by_username_or_email(email):
        return jsonify({"error": "username or email already in use"}), 409

    user_id = uuid.uuid4().hex
    now = datetime.utcnow().isoformat()
    store.create_user(
        user_id=user_id,
        username=username,
        email=email,
        password_hash=generate_password_hash(password),
        provider=None,
        provider_id=None,
        display_name=username,
        avatar_url="",
        created_at=now,
    )
    session["user_id"] = user_id
    session["display_name"] = username
    session["avatar_url"] = ""
    return jsonify({"user_id": user_id, "username": username}), 201


@auth_bp.route("/auth/login", methods=["POST"])
def login():
    """Username-or-email + password login."""
    data = request.get_json(force=True)
    identifier = (data.get("username_or_email") or "").strip()
    password = data.get("password") or ""

    user = store.find_user_by_username_or_email(identifier)
    if not user:
        return jsonify({"error": "Invalid credentials"}), 401
    if not user["password_hash"]:
        return jsonify({"error": "This account uses Google sign-in — no password is set"}), 401
    if not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Invalid credentials"}), 401

    now = datetime.utcnow().isoformat()
    store.update_user_login(user["user_id"], now)
    session["user_id"] = user["user_id"]
    session["display_name"] = user["display_name"]
    session["avatar_url"] = user["avatar_url"]
    return jsonify({"user_id": user["user_id"], "username": user["username"]}), 200


@auth_bp.route("/auth/login/google")
def login_google():
    """Redirect to Google's consent screen. 404s if OAuth isn't configured."""
    if not config.GOOGLE_OAUTH_ENABLED:
        abort(404)
    redirect_uri = url_for("auth.callback_google", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@auth_bp.route("/auth/callback/google")
def callback_google():
    """Handle Google OAuth callback. Create/update user (no password), set session."""
    if not config.GOOGLE_OAUTH_ENABLED:
        abort(404)
    token = oauth.google.authorize_access_token()
    userinfo = token.get("userinfo", {})

    provider_id = userinfo["sub"]
    email = (userinfo.get("email") or "").lower()
    name = userinfo.get("name", email)
    avatar = userinfo.get("picture", "")

    user = store.find_user_by_provider("google", provider_id)
    now = datetime.utcnow().isoformat()
    if user:
        store.update_user_login(user["user_id"], now)
        user_id = user["user_id"]
    else:
        denied = signup_policy.check_google_signup(email, bool(userinfo.get("email_verified")))
        if denied:
            return redirect(url_for("auth.login_page", error=denied))
        user_id = uuid.uuid4().hex
        # username is NOT NULL UNIQUE — derive from email local-part, dedupe on collision
        username = _unique_username_from_email(email)
        store.create_user(
            user_id=user_id,
            username=username,
            email=email,
            password_hash=None,   # Google-only account — no password
            provider="google",
            provider_id=provider_id,
            display_name=name,
            avatar_url=avatar,
            created_at=now,
        )

    session["user_id"] = user_id
    session["display_name"] = name
    session["avatar_url"] = avatar
    return redirect("/")


def _unique_username_from_email(email: str) -> str:
    base = (email.split("@")[0] or "user").strip() or "user"
    candidate = base
    n = 1
    while store.find_user_by_username_or_email(candidate):
        n += 1
        candidate = f"{base}{n}"
    return candidate


@auth_bp.route("/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login_page"))


@auth_bp.route("/auth/me")
def current_user_info():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not authenticated"}), 401
    return jsonify({
        "user_id": user_id,
        "display_name": session.get("display_name"),
        "avatar_url": session.get("avatar_url"),
    })
