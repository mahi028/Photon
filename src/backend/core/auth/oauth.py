"""Google OAuth client setup via Authlib — no-op when not configured.

Username/email/password is the baseline auth method and never depends on
this module. This registers Google as an *additional* sign-in option only
when GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET are both set.
"""

from __future__ import annotations

from authlib.integrations.flask_client import OAuth

from ...config import config

oauth = OAuth()


def init_oauth(app) -> None:
    """Register the Google OAuth provider, only if credentials are configured.

    If GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET are unset, this is a no-op — no
    client is registered, and the guarded routes in auth_routes.py 404 instead
    of raising when a client isn't present.
    """
    if not config.GOOGLE_OAUTH_ENABLED:
        return
    oauth.init_app(app)
    oauth.register(
        name="google",
        server_metadata_url=(
            "https://accounts.google.com/.well-known/openid-configuration"
        ),
        client_kwargs={"scope": "openid email profile"},
    )
