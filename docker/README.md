# Running Photon with Docker

Two services from one image (`docker/Dockerfile`), behind Caddy on one port:

| service | what | port |
|---|---|---|
| `caddy` | reverse proxy, the only published port: `/mcp` to `photon-mcp`, the rest to `photon` (`deploy/Caddyfile`) | `PHOTON_PORT` (8890) |
| `photon` | web UI + REST API (gunicorn, 1 worker × 32 threads) | `PHOTON_WEB_PORT` (10000), internal |
| `photon-mcp` | MCP server, streamable HTTP, API-key auth | `PHOTON_MCP_PORT` (10001), internal |

All ports are set in `../.env` (D-051), the same file a non-Docker run uses.

Both share the data folder: the SQLite database, uploads, outputs, saved and uploaded models, and the **model sidecar's Unix socket**. So whichever container starts the sidecar, both use the same one and a model is loaded on the GPU once. The optional gVisor sandbox worker has its own compose file (`docker/sandbox-worker/`).

## Requirements
- Docker with Compose **v2.24+** (needed for `env_file: required: false` and several `--env-file`).
- For GPUs: NVIDIA driver + `nvidia-container-toolkit`. On a CPU-only host, delete the `deploy:` block in `docker-compose.yml`.
- About 10 GB of disk for the image. torch and CUDA wheels are about 6 GB.

## Start
```bash
cd docker
cp photon.env.example photon.env     # set PHOTON_DATA_DIR, PHOTON_UID/GID
# app settings and ports (PHOTON_PORT, ...) live in ../.env
docker compose --env-file ../.env --env-file photon.env up -d --build
docker compose --env-file ../.env --env-file photon.env logs -f photon
```
Open `http://<host>:8890`. MCP clients use `http://<host>:8890/mcp` with an API key from the web UI (both on `PHOTON_PORT`).

## Things to know
- **`PHOTON_DATA_DIR` is mounted at the same absolute path inside the containers.** The database stores absolute paths of uploads, outputs and model weights. To reuse an existing install, point it at that install's `volumes/` (e.g. `/home/<you>/Photon/volumes`). With a different path, old sessions would point at files that don't exist inside the container.
- **Run as the folder's owner.** `PHOTON_UID`/`PHOTON_GID` should be the owner of `PHOTON_DATA_DIR` (`id -u`, `id -g`), so files stay editable from the host.
- **Set `FLASK_SECRET_KEY` in `../.env`.** Without it sessions are signed with the default key. Sign-up is closed unless `PHOTON_SIGNUP` says otherwise.
- **One gunicorn worker on purpose.** Run progress streams (SSE), the in-memory image registry and the batch pool live in the process. Don't raise `--workers`; threads give the concurrency.
- **The model sidecar is spawned by whichever container first runs a model op.** `MODEL_SIDECAR_IDLE_EXIT_SECONDS` (default here: 24 h) keeps it, with torch imported, alive between runs. Models still leave the GPU after `MODEL_IDLE_TIMEOUT_SECONDS`.
- **Server-side paths over MCP** (`upload_images(paths=...)`, `register_model(weights_path=...)`): set `PHOTON_MCP_READ_DIR` to a host folder, which is mounted read-only at the same path, and list it in `PHOTON_MCP_PATH_ROOTS`. Leave both empty to disable.
- **Model download caches** (DINOv2 via torch hub, timm) go to `PHOTON_DATA_DIR/cache/` so they survive rebuilds.
- **Sandbox.** The default `PHOTON_SANDBOX=local` runs custom code in a resource-limited subprocess inside the app container. For untrusted users, run the gVisor worker (`docker/sandbox-worker/README.md`) and set `PHOTON_SANDBOX=remote` in `../.env`. Uploaded segmentation models only run with `local`.
- **Health:** the app serves `GET /healthz`; the MCP check accepts the 401 that `/mcp` returns without a key.

## Rebuild after code changes
```bash
docker compose --env-file ../.env --env-file photon.env up -d --build
```
The CSS is built in the image (Tailwind stage), and Python dependencies come from `uv.lock` (extras `mcp` + `models`).
