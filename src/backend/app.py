"""Flask application factory."""

from flask import Flask
from .config import config


def create_app() -> Flask:
    """Create and configure the Flask application."""
    import os
    from pathlib import Path
    
    # Absolute paths are safer to avoid working directory / import path confusion
    current_dir = Path(__file__).parent
    frontend_dir = current_dir.parent / "frontend"
    
    app = Flask(
        __name__,
        template_folder=str(frontend_dir / "templates"),
        static_folder=str(frontend_dir / "static"),
        static_url_path="/static",
    )

    app.secret_key = config.SECRET_KEY
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_BYTES

    # OAuth setup — no-op if GOOGLE_CLIENT_ID/SECRET are unset (config.GOOGLE_OAUTH_ENABLED)
    from .core.auth.oauth import init_oauth
    init_oauth(app)

    from .core.auth.signup_policy import policy as signup_policy
    if signup_policy.warning():
        app.logger.warning("SECURITY: %s See core/auth/signup_policy.py.", signup_policy.warning())

    # Build the library-op catalogue (D-041, ~3 s of imports + docstring
    # parsing) off the request path, so the first search doesn't pay for it.
    import threading
    from .core.pipeline import library as _library
    threading.Thread(target=_library.all_specs, name="library-warmup", daemon=True).start()

    # Batch runs whose process died (a restart, a crash) are taken over by the
    # first process that serves a request: not at import, because a dev
    # reloader's watcher process imports the app too but never serves (D-046).
    _recovered = {"done": False}

    @app.before_request
    def _recover_batches_once():
        if not _recovered["done"]:
            _recovered["done"] = True
            from .core.pipeline import batch as _batch
            threading.Thread(target=_batch.recover, name="batch-recover", daemon=True).start()

    # Register blueprints
    from .api.auth_routes import auth_bp
    from .api.upload_routes import upload_bp
    from .api.session_routes import session_bp
    from .api.pipeline_routes import pipeline_bp
    from .api.chat_routes import chat_bp
    from .api.manual_routes import manual_bp
    from .api.download_routes import download_bp
    from .api.example_routes import example_bp
    from .api.shared_routes import shared_bp
    from .api.llm_routes import llm_bp
    from .api.api_key_routes import api_key_bp
    from .api.saved_routes import saved_bp
    from .api.batch_routes import batch_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(llm_bp, url_prefix="/api")
    app.register_blueprint(upload_bp, url_prefix="/api")
    app.register_blueprint(session_bp, url_prefix="/api")
    app.register_blueprint(pipeline_bp, url_prefix="/api")
    app.register_blueprint(chat_bp, url_prefix="/api")
    app.register_blueprint(manual_bp, url_prefix="/api")
    app.register_blueprint(download_bp, url_prefix="/api")
    app.register_blueprint(example_bp, url_prefix="/api")
    app.register_blueprint(api_key_bp, url_prefix="/api")
    app.register_blueprint(saved_bp, url_prefix="/api")
    app.register_blueprint(batch_bp, url_prefix="/api")
    app.register_blueprint(shared_bp)

    # Serve preview images from volumes/uploads
    from flask import send_from_directory
    import os

    # File routes (D-037). These used to serve anything under volumes/ with
    # no login (sessions.db included). Now: login required, uploads only via
    # /previews/uploads/<image file>, outputs only under a session you own.
    # Anything else is a 404 so existence isn't leaked.
    from flask import abort
    from .core.auth.middleware import get_current_user_id, login_required as _login_required
    from .core.session import store as _store
    from .core.session.manager import get_manager as _get_manager

    @app.route("/previews/uploads/<path:filename>")
    @_login_required
    def serve_preview(filename: str):
        if "/" in filename:
            abort(404)
        image_id = _store.image_id_for_upload_file(filename)
        if not image_id or not _store.user_can_access_image(get_current_user_id(), image_id):
            abort(404)
        return send_from_directory(config.UPLOADS_DIR, filename)

    @app.route("/outputs/<session_id>/<path:filename>")
    @_login_required
    def serve_output(session_id: str, filename: str):
        if not _get_manager().get_session_for_user(session_id, get_current_user_id()):
            abort(404)
        return send_from_directory(config.OUTPUTS_DIR / session_id, filename)

    # Main index route
    from .core.auth.middleware import login_required

    @app.route("/")
    @login_required
    def index():
        from flask import render_template
        return render_template("index.html")

    # Deep link into a specific session — inert until Phase 6 wires the
    # sidebar/session-workspace frontend to read active_session_id.
    @app.route("/session/<session_id>")
    @login_required
    def session_view(session_id: str):
        from flask import render_template
        return render_template("index.html", active_session_id=session_id)

    @app.route("/settings/api-keys")
    @login_required
    def api_keys_view():
        from flask import render_template
        return render_template("api_keys.html", mcp_public_url=config.MCP_PUBLIC_URL)

    @app.route("/batches")
    @login_required
    def batches_view():
        from flask import render_template
        return render_template("batches.html")

    @app.route("/batches/<batch_id>")
    @login_required
    def batch_view(batch_id: str):
        from flask import render_template
        return render_template("batch.html", batch_id=batch_id)

    # --- v3 pipeline editor ---
    @app.route("/pipelines")
    @login_required
    def pipelines_view():
        from flask import render_template
        return render_template("pipelines.html")

    @app.route("/pipelines/<pipeline_id>")
    @login_required
    def pipeline_editor_view(pipeline_id: str):
        from flask import render_template
        return render_template("pipeline_editor.html", pipeline_id=pipeline_id)

    @app.route("/published/<published_id>")
    @login_required
    def published_pipeline_view(published_id: str):
        """Read-only editor view of a published pipeline version (D-035)."""
        from flask import render_template, request
        version = request.args.get("version", type=int)
        return render_template("pipeline_editor.html", pipeline_id=None,
                               published_id=published_id, published_version=version)

    return app
