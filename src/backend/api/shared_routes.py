"""Share-as-clone: GET /shared/<token>.

Opening a share link does NOT grant access to the original session. It deep-
clones the session (with full file duplication of uploads + outputs) into the
visitor's own account and redirects them into their new, independent copy.
Requires the visitor to be logged in — the clone has to belong to *someone*.

Security: tokens are secrets.token_urlsafe(32) (256-bit entropy) and only
exist in share_links if actually created via POST /sessions/<id>/share — a
random guess looks up nothing and 404s. One-time links (reusable=False) 410
once used_count > 0; reusable links can be opened any number of times, each
producing an independent clone.
"""

from flask import Blueprint, abort, redirect

from ..core.auth.middleware import login_required, get_current_user_id
from ..core.session import store
from ..core.session.manager import get_manager

shared_bp = Blueprint("shared", __name__)


@shared_bp.route("/shared/<token>")
@login_required
def open_shared(token: str):
    """Resolve a share token, clone the session into the current user, redirect in."""
    link = store.get_share_link(token)
    if not link:
        abort(404)  # not a token we ever issued

    if not link["reusable"] and link["used_count"] > 0:
        abort(410)  # one-time link already used

    manager = get_manager()
    cloned = manager.clone_session(link["session_id"], get_current_user_id())
    if not cloned:
        # Source session was deleted after the link was created.
        abort(404)

    store.increment_share_link_usage(token)

    # Register the cloned images into the runtime image registry (the store
    # already has them; upload_routes' in-memory registry is what chat/manual
    # routes actually read from — see core/upload_routes.py).
    from .upload_routes import get_image_registry
    registry = get_image_registry()
    for image_id in cloned.image_ids:
        meta = store.load_image(image_id)
        if meta:
            registry[image_id] = meta

    return redirect(f"/session/{cloned.session_id}")
