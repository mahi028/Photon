"""Routes for viewing shared windows."""

from flask import Blueprint, render_template, abort

from ..core.session.manager import get_manager

shared_bp = Blueprint("shared", __name__)


@shared_bp.route("/shared/<token>")
def view_shared_window(token: str):
    """Render the focused single-window view for a shared link."""
    # We first need to resolve the token to a window_id
    # To avoid cyclic imports or exposing store directly if not needed, we can add it to manager
    # Or just import store here:
    from ..core.session.store import resolve_share_token
    
    window_id = resolve_share_token(token)
    if not window_id:
        abort(404)
        
    window = get_manager().get_window(window_id)
    if not window or not window.is_shared:
        abort(404)
        
    return render_template("shared_window.html", token=token, window_id=window_id)
