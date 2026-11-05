<div align="center">
  <img src="src/frontend/static/assets/logo.png" alt="Photon Logo" height="80" />
</div>

<p align="center"><strong>AI-Powered Image Processing</strong></p>

Photon is an autonomous, natural-language image processing studio. Upload any image, describe how you want it transformed in plain English, and watch the AI write, execute, and self-correct the required Python code in real-time to deliver the result.

## Features

- **Conversational Processing**: Chat with your images. Ask for edge detection, color manipulation, or complex scientific filters, and the integrated LLM handles the implementation.
- **Autonomous Execution Loop**: Photon writes Python, runs it in a secure sandbox, analyzes the output, and automatically fixes errors—up to 6 self-correction attempts per prompt.
- **Batch Processing Support**: Apply the same transformation pipeline across multiple images simultaneously with a single prompt.
- **Universal Format Support**: Works flawlessly with standard RGB/RGBA formats, grayscale, and even float32 multispectral `.npy` arrays.
- **Manual Mode**: Need exact control? Drop into the built-in code editor to write or tweak the Python execution script yourself.
- **Visual Version Control**: Interactive carousels, side-by-side comparisons, and filmstrips make it easy to track every iteration of your image.
- **Export Ready**: Download your selected full-resolution outputs packaged in a neat ZIP archive.

## Quick Start

### Option A: Docker (Recommended)

1. **Configure Environment**

   ```bash
   cp .env.example .env
   ```

   Edit `.env` to select your preferred LLM provider (`gemini` or `openai`) and insert your API key.

2. **Run with Docker Compose**
   ```bash
   docker-compose -f docker/docker-compose.yml up --build -d
   ```
   _Photon is now running! Open `http://localhost:8889` in your browser._

### Option B: Local Setup

**1. Install Dependencies**

```bash
uv sync
npm install
```

**2. Configure Environment**

```bash
cp .env.example .env
```

Edit `.env` to select your preferred LLM provider (`gemini` or `openai`) and insert your API key. (Supports Google Gemini SDK, official OpenAI, or any OpenAI-compatible endpoint like Ollama or Groq).

**3. Build UI Assets**

```bash
npm run build:css
```

**4. Launch Photon**

```bash
uv run src/backend/wsgi.py
```

or for Production server

```bash
uv run gunicorn -b 0.0.0.0:80 -w 4 src.backend.wsgi:app # on linux

uv run waitress-serve --listen=0.0.0.0:80 src.backend.wsgi:app # on windows
```

_Alternatively:_ `flask --app src/backend/app run --debug`

Open `http://localhost:8889` in your browser (the dev server; gunicorn/waitress use whatever `-b`/`--listen` you pass).

## MCP Server (use Photon from any LLM)

Photon's pipeline layer is also exposed as an [MCP](https://modelcontextprotocol.io) server, so Claude Code, Claude Desktop, or any MCP client can drive it directly. The client LLM plans the work: it reads the op catalog, writes a pipeline graph, validates it, runs it, looks at the previews and iterates. Photon's own LLM loop is not used.

```bash
uv sync --extra mcp
```

**Streamable HTTP** (default `0.0.0.0:8890`; override with `--host/--port` or `PHOTON_MCP_HOST/PHOTON_MCP_PORT`). Every request needs a per-user API key:

1. Log in to the web UI, open the user menu, choose **API keys**, and create a key. It is shown once.
2. Start the server and register it with your client:

```bash
uv run --extra mcp python -m src.mcp_server --transport http
claude mcp add --transport http photon http://<host>:8890/mcp \
  --header "Authorization: Bearer pk_..."
```

Each call acts as the key's owner, so results appear in that user's sessions. Keys can expire (7/30/90/365 days) and can be revoked from the same page; only a SHA-256 hash is stored. If clients reach the server through a proxy or port-forward, set `PHOTON_MCP_PUBLIC_URL` (e.g. `http://172.20.23.234:10227/mcp`) so the API keys page shows the right URL in its copy-paste command.

**stdio** (local, launched by the client, no key; acts as `PHOTON_MCP_USER`, a username or email, which can go in `.env`):

```bash
claude mcp add photon -- uv run --directory /path/to/Photon --extra mcp python -m src.mcp_server
```

Tools: `upload_image`, `create_session`, `attach_image`, `detach_image`, `delete_session`, `list_sessions`, `get_session`, `get_output`, `download_output`, `list_ops`, `validate_pipeline`, `run_pipeline`, `list_pipelines`, `get_pipeline`, `save_pipeline`, `publish_pipeline`, `unpublish_pipeline`, `fork_pipeline`. Read-only tools carry `readOnlyHint` and the two destructive ones `destructiveHint`, so clients can auto-approve the former and prompt for the latter. `delete_session` also needs the session's exact title as `confirm_title`.

**Public pipelines.** A pipeline published from the editor (or with `publish_pipeline`) is visible to everyone on the instance: in the **Public** section of the Pipelines page, and to any MCP client via `list_pipelines(scope="public")`. Each publish is an immutable version that freezes the graph and the code of the custom nodes it uses, so colleagues can run it with `run_pipeline(published_id=..., version=...)` even after you edit or delete your own copy. **Fork** copies a version, custom nodes included, into your own library to edit.

Getting images in and out from a remote client:

- **In:** `upload_image(data_base64=..., filename="scan.png")` works from anywhere. `url` must be a public http(s) address; loopback, private, link-local and similar addresses are refused, redirects included, and the same rule now applies to the web UI's URL upload (set `PHOTON_ALLOW_PRIVATE_URLS=1` to allow internal hosts). `path` reads the server's disk: always allowed over stdio, but over HTTP only inside directories listed in `PHOTON_MCP_PATH_ROOTS` (separated by `:`), and never inside `volumes/`.
- **Out:** tool results include small JPEG previews. `download_output` returns the full-resolution file as an embedded resource, up to `PHOTON_MCP_MAX_DOWNLOAD_MB` (default 25).

Scope is deliberately narrow: only trusted catalog ops (plus already-saved custom nodes) run. There is no `run_code` and no tool to save new custom nodes, since both execute arbitrary Python and the subprocess sandbox should not face prompt-injected code until the Docker executor ships. It shares `volumes/` and `sessions.db` with the web app, so both can run at once.

## The Execution Contract

Whether you are using manual mode or observing the AI, Photon relies on a strict execution contract for the sandbox:

```python
def main(input_path: str, output_path_dir: str) -> str:
    """Read input_path, transform, save result into output_path_dir, return full saved path."""
    ...
```

_(You can view example code from within the application interface.)_

## License

This product is released under the [MIT License](LICENSE).
