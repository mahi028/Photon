# Photon sandbox worker (D-038). Runs untrusted custom nodes / run-code jobs.
# Build from the repo root:  docker build -f docker/sandbox-worker/Dockerfile -t photon-sandbox-worker .
# Run it under gVisor (runtime: runsc), see docker/sandbox-worker/README.md.
#
# CUDA: torch/torchvision come from the cu121 wheel index (pyproject.toml) and
# bundle their CUDA runtime, so a slim base is enough; the host provides only
# the NVIDIA driver, passed through by gVisor's nvproxy.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv \
    && useradd --create-home --uid 10001 sandbox

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Code only: no .env, no volumes/, no frontend, no MCP server.
COPY src/backend/ ./src/backend/
COPY src/sandbox_worker/ ./src/sandbox_worker/
RUN touch src/__init__.py

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PHOTON_NO_DOTENV=1 \
    PHOTON_VOLUMES_DIR=/work \
    MODEL_SIDECAR_ENABLED=1

USER sandbox
EXPOSE 8891
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8891/v1/health', timeout=3)"
CMD ["python", "-m", "src.sandbox_worker.server", "--host", "0.0.0.0", "--port", "8891"]
