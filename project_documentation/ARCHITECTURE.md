# Architecture

## Component Map

```
Browser (Jinja2 + Tailwind + Alpine.js + CodeMirror 6
         + marked/DOMPurify/highlight.js/KaTeX/mermaid — see DESIGN.md "Code & Markdown Rendering")
   |  REST + SSE (EventSource)
   v
Flask App  (src/backend/)
   ├── app.py              — factory, blueprint registration
   ├── config.py           — env-driven config
   ├── wsgi.py             — gunicorn entrypoint
   ├── api/
   │   ├── upload_routes.py        POST /api/upload
   │   ├── window_routes.py        CRUD /api/windows
   │   ├── chat_routes.py          POST /message, GET /stream (SSE)
   │   ├── manual_routes.py        POST /run-code
   │   └── download_routes.py      POST /api/download
   ├── core/
   │   ├── image/           — metadata, thumbnail, composite (no HTTP)
   │   ├── sandbox/         — Executor ABC + SubprocessExecutor + DockerStub
   │   ├── llm/             — client, prompts, pydantic schema
   │   └── session/         — Window dataclass + in-memory registry
   ├── models/dto.py        — request/response dataclasses
   └── utils/               — slugify, validation
volumes/
   ├── uploads/             — original full-res, keyed by image_id
   ├── outputs/             — results, keyed by window_id/
   └── tmp_exec/            — per-run scratch (auto-cleaned)
```

## Data Flow

### Upload
```
Browser → POST /api/upload
  → validation (ext, size)
  → save to volumes/uploads/{image_id}.ext
  → metadata.py: load + introspect (shape, dtype, range, kind)
  → thumbnail.py: downsample → preview_{image_id}.jpg
  → composite.py: (if C>4) mean-projection preview
  → return {image_id, metadata, preview_url}
```

### LLM Window Loop
```
Browser → POST /api/windows/{id}/message {prompt}
  → build system prompt (image metadata + contract + whitelist)
  → LLM client.send_turn() → LLMTurnResult (JSON-validated)
  → SubprocessExecutor.execute(code, input_path, output_dir, timeout)
    → AST safety check → reject or run
    → subprocess.run → capture stdout/stderr → ExecutionResult
  → if file_exists & done → persist GeneratedImage → SSE "done"
  → else → loop (max MAX_LOOP_ITERATIONS)
  → SSE events stream progress to browser
```

### Manual Window Run
```
Browser → POST /api/windows/{id}/run-code {code}
  → validate window.mode == "manual"
  → SubprocessExecutor.execute(code, input_path, output_dir, timeout)
  → return ExecutionResult directly (no LLM, no retry)
  → if file_exists → persist GeneratedImage
```

## Interface Contracts

### Executor (sandbox/base.py)
```python
class Executor(ABC):
    def execute(self, code: str, input_path: str, output_dir: str,
                timeout: int) -> ExecutionResult: ...
```
All callers import `Executor`. Swap `SubprocessExecutor` → `DockerExecutor`
by changing one line in config/DI wiring.

### LLM Client (core/llm/client.py)
```python
def send_turn(conversation: list[Message], system_prompt: str) -> LLMTurnResult
```
Pydantic-validates the raw LLM JSON response. On validation failure,
returns a synthetic error result so the loop can feed it back without crashing.

## Key Design Decisions
- Flask never exec()s code in-process. All code runs via Executor.
- Both window modes share the same Executor, same function contract, same whitelist.
- Session state is durable: SQLite (`volumes/sessions.db`, absolute path from config) is the
  source of truth; `WindowManager` is a capped lazy-LRU cache over it (Section 5 of the spec).
  Every turn — including loop-internal execution-feedback turns (`internal=1`) — and every
  output is written through to SQLite in the request that produced it. Image bytes stay in
  volumes/; image metadata is persisted in the `images` table and rehydrated at startup.
- Metadata formatting is shared between system prompt and UI — one function.
