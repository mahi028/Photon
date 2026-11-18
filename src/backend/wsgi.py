"""WSGI entrypoint for gunicorn and development server."""

import sys
from pathlib import Path

# Allow running as: python src/backend/wsgi.py
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.backend.app import create_app

app = create_app()

if __name__ == "__main__":
    from src.backend.config import config
    # Dev server; production: `gunicorn src.backend.wsgi:app` (gunicorn.conf.py).
    app.run(
        host=config.WEB_HOST,
        port=config.WEB_PORT,
        debug=True,
    )
