"""gunicorn settings for `gunicorn src.backend.wsgi:app` (read from the cwd).

Host and port come from .env (PHOTON_WEB_HOST / PHOTON_WEB_PORT), like the rest
of Photon's ports (D-051); the public port is Caddy's (deploy/Caddyfile).
"""
import os
from pathlib import Path

if os.getenv("PHOTON_NO_DOTENV") != "1":
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")

bind = f"{os.getenv('PHOTON_WEB_HOST', '127.0.0.1')}:{os.getenv('PHOTON_WEB_PORT', '10000')}"
# ONE worker on purpose: live run updates (SSE queues), the in-memory image
# registry and the batch pool live in the process. Threads give concurrency.
worker_class = "gthread"
workers = 1
threads = 32
timeout = 300
graceful_timeout = 30
accesslog = "-"
