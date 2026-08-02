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

    app.register_blueprint(auth_bp)
    app.register_blueprint(llm_bp, url_prefix="/api")
    app.register_blueprint(upload_bp, url_prefix="/api")
    app.register_blueprint(session_bp, url_prefix="/api")
    app.register_blueprint(pipeline_bp, url_prefix="/api")
    app.register_blueprint(chat_bp, url_prefix="/api")
    app.register_blueprint(manual_bp, url_prefix="/api")
    app.register_blueprint(download_bp, url_prefix="/api")
    app.register_blueprint(example_bp, url_prefix="/api")
    app.register_blueprint(shared_bp)

    # Serve preview images from volumes/uploads
    from flask import send_from_directory
    import os

    @app.route("/previews/<path:filename>")
    def serve_preview(filename: str):
        return send_from_directory(config.VOLUMES_DIR, filename)

    @app.route("/outputs/<path:filename>")
    def serve_output(filename: str):
        return send_from_directory(config.OUTPUTS_DIR, filename)

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

    return app
