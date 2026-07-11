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

    # Register blueprints
    from .api.upload_routes import upload_bp
    from .api.window_routes import window_bp
    from .api.chat_routes import chat_bp
    from .api.manual_routes import manual_bp
    from .api.download_routes import download_bp
    from .api.example_routes import example_bp
    from .api.shared_routes import shared_bp

    app.register_blueprint(upload_bp, url_prefix="/api")
    app.register_blueprint(window_bp, url_prefix="/api")
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
    @app.route("/")
    def index():
        from flask import render_template
        return render_template("index.html")

    return app
