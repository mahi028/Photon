# syntax=docker/dockerfile:1.7
# Photon app image: web UI + API (gunicorn) and, from the same image, the MCP
# server. Build from the repo root:
#   docker build -f docker/Dockerfile -t photon .
# or use docker/docker-compose.yml (see docker/README.md).
#
# CUDA: torch/torchvision come from the cu121 wheel index (pyproject.toml) and
# bundle their CUDA runtime, so a slim base is enough; the host only needs the
# NVIDIA driver + nvidia-container-toolkit (compose reserves the GPU).

# ---------------------------------------------------------------------------
# 1. Tailwind CSS (output.css is generated, not committed)
# ---------------------------------------------------------------------------
FROM node:22-alpine AS css
WORKDIR /build
COPY package.json package-lock.json tailwind.config.js ./
RUN npm ci --no-audit --no-fund
COPY src/frontend/ ./src/frontend/
RUN npm run build:css

# ---------------------------------------------------------------------------
# 2. Python environment (uv, locked): app + MCP server + uploaded-model support
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS deps
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra mcp --extra models

# ---------------------------------------------------------------------------
# 3. Runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim
# libgl/glib: OpenCV. tini: PID 1 that reaps the model sidecar and the
# sandbox child processes the app spawns.
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=deps /app/.venv /app/.venv
COPY src/ ./src/
COPY --from=css /build/src/frontend/static/css/output.css ./src/frontend/static/css/output.css
COPY pyproject.toml gunicorn.conf.py ./
RUN touch src/__init__.py \
    && python -c "import compileall, sys; sys.exit(not compileall.compile_dir('src', quiet=1))" \
    && chmod -R a+rX /app

# PHOTON_VOLUMES_DIR is set by compose: the data folder is mounted at the SAME
# path as on the host, because the database stores absolute file paths.
# HOME=/tmp: the container runs as the host user (compose `user:`), who has no
# home in the image. Model download caches go to the data folder (compose).
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PHOTON_NO_DOTENV=1 \
    PHOTON_VOLUMES_DIR=/data \
    FLASK_ENV=production \
    HOME=/tmp \
    PHOTON_WEB_HOST=0.0.0.0 \
    PHOTON_WEB_PORT=10000 \
    PHOTON_MCP_HOST=0.0.0.0 \
    PHOTON_MCP_PORT=10001

# Internal ports (PHOTON_WEB_PORT / PHOTON_MCP_PORT, overridable from .env); only
# the Caddy service publishes a port (PHOTON_PORT), see docker-compose.yml.
EXPOSE 10000 10001
ENTRYPOINT ["/usr/bin/tini", "--"]

# gunicorn.conf.py: bind from PHOTON_WEB_HOST/PORT, ONE worker on purpose (live
# run updates, the image registry and the batch pool live in the process).
CMD ["gunicorn", "src.backend.wsgi:app"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ['PHOTON_WEB_PORT']}/healthz\", timeout=4)"
