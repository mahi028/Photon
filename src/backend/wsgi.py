"""WSGI entrypoint for gunicorn and development server."""

import sys
from pathlib import Path

# Allow running as: python src/backend/wsgi.py
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.backend.app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=8889,
        debug=True,
    )
