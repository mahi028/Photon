# Sandbox worker (gVisor, optional GPU)

Status: **built and tested without gVisor** (worker + protocol + app switch,
`tests/test_remote_sandbox.py`). **Not yet run under `runsc`**: the dev box is
itself a container (no Docker, no KVM). Use the checklist at the end to do that.

## What it is

By default (`PHOTON_SANDBOX=local`) custom nodes and run-code snippets run as
rlimited subprocesses on the app host, behind an AST check and a runtime
audit-hook guard (`src/backend/core/sandbox/guard.py`). That's a speed bump,
not a boundary: the code shares a kernel with the app and could reach its
files through a C-extension escape.

`PHOTON_SANDBOX=remote` sends every job to this worker instead:

```
app (Flask / MCP)  --HTTP + bearer token-->  sandbox-worker container (runsc)
  input image                                  same local executors inside:
  <-- zip: result.json + outputs --            AST check, rlimits, guard
```

The worker holds no secrets, has no copy of `volumes/` and keeps nothing
between jobs. gVisor puts a user-space kernel between the job and the host
kernel. So an escape would have to beat the guard, then gVisor, and would
still land in an empty container.

## Host setup

1. Install gVisor: <https://gvisor.dev/docs/user_guide/install/>
   (`runsc install` registers it with Docker).
2. GPU (optional): host NVIDIA driver plus nvidia-container-toolkit. Then
   enable nvproxy in `/etc/docker/daemon.json`; see `daemon.json.example`.
   Restart Docker. Check that your driver version is on gVisor's nvproxy
   support list.
3. Block egress from the worker. Add a rule to the `DOCKER-USER` iptables
   chain that drops traffic from the container's subnet, except replies to
   the app. Jobs have no reason to reach the network, and the runtime guard
   already denies sockets. This rule is the outer wall.

## Run

```bash
export PHOTON_SANDBOX_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
PHOTON_SANDBOX_BIND=10.0.0.5 docker compose -f docker/sandbox-worker/docker-compose.yml up -d --build
curl http://10.0.0.5:8891/v1/health        # {"ok": true, "gpu": true}
```

`gpu` is `null` for the first minute or so: it comes from a one-off
`import torch` probe in a separate process, so health checks never wait on it.

App side (`.env`), then restart the Flask app and the MCP server:

```
PHOTON_SANDBOX=remote
PHOTON_SANDBOX_URL=http://10.0.0.5:8891
PHOTON_SANDBOX_TOKEN=<same token>
```

For a CPU-only worker, delete the `deploy.resources` block and set
`SANDBOX_MEMORY_LIMIT_GB=8`. On a GPU worker, leave it at `0`: CUDA can't live
under RLIMIT_AS, so the container's `mem_limit` caps memory instead.

## Adding libraries (tensorflow, ...)

Custom nodes can only import what's in `pyproject.toml` `[project].dependencies`
(`dep_sync.py`). Run `uv add tensorflow`, then rebuild **both** the app (so
the AST allow-list knows the import) and this image (so it's installed).

## Checklist for the first real run

- [ ] `docker info` shows the `runsc` runtime; `docker run --rm --runtime=runsc hello-world` works
- [ ] `curl .../v1/health` shows `gpu: true` (GPU hosts)
- [ ] Run a preset pipeline and a run-code snippet with `PHOTON_SANDBOX=remote`
- [ ] Run a custom node that uses `torch.cuda` (for example, a conv on the GPU)
- [ ] Run a model-op pipeline. Model ops use the worker's own sidecar, so weights download into `/home/sandbox` (tmpfs). If you want a persistent cache, mount a volume there instead.
- [ ] `PHOTON_SANDBOX=remote uv run pytest tests/test_sandbox.py -m slow` against the worker, so the guard attack set also runs under gVisor
- [ ] From inside the container, egress is refused: `curl https://example.com` fails
