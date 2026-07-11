# Stage 1: Build CSS
FROM node:24-alpine AS frontend-builder
WORKDIR /app

# Install npm dependencies
COPY package*.json ./
RUN npm ci

# Copy tailwind config and frontend source
COPY tailwind.config.js ./
COPY src/frontend/ ./src/frontend/

# Build the CSS
RUN npm run build:css


# Stage 2: Python Backend
FROM python:3.12-slim
WORKDIR /app

# Install system dependencies (e.g. for OpenCV)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN pip install uv

# Copy python dependency files
COPY pyproject.toml uv.lock ./

# Sync dependencies using uv (this creates /app/.venv)
RUN uv sync --frozen --no-dev

# Copy the rest of the application
COPY src/ ./src/

# Copy the built CSS from the frontend-builder stage
COPY --from=frontend-builder /app/src/frontend/static/css/output.css ./src/frontend/static/css/output.css

# Make volumes directory
RUN mkdir -p volumes

# Set environment variables
ENV FLASK_APP=src.backend.app
ENV FLASK_RUN_HOST=0.0.0.0
ENV FLASK_RUN_PORT=5000
ENV PYTHONUNBUFFERED=1
# Make sure the uv virtual environment is active (add it to PATH)
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 5000

# Start the application using Gunicorn (Production WSGI server)
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "src.backend.wsgi:app"]
