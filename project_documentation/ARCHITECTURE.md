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
   │   ├── auth_routes.py           signup/login (always) + Google OAuth (optional)/logout/me
   │   ├── upload_routes.py         POST /api/upload
   │   ├── session_routes.py        CRUD /api/sessions (owner-scoped, @login_required)
   │   ├── pipeline_routes.py       /api/ops, /api/pipelines, /api/custom-nodes, run-pipeline
   │   ├── chat_routes.py           POST /message, GET /stream (SSE)
   │   ├── manual_routes.py         POST /run-code
   │   └── download_routes.py       POST /api/sessions/<id>/download (one session's outputs)
   ├── core/
   │   ├── auth/             — oauth.py (no-op unless GOOGLE_CLIENT_ID/SECRET set),
   │   │                        middleware.py (login_required, get_current_user_id)
   │   ├── image/            — metadata, thumbnail, composite (no HTTP)
   │   ├── models/           — v3 P4: model registry, ModelManager (lazy load /
   │   │                        idle unload / VRAM budget), policy guard, sidecar
   │   ├── pipeline/         — v3: op registry (40 ops incl. 2 model ops), typed
   │   │                        ports, DAG runner, subprocess executor, presets
   │   ├── sandbox/          — Executor ABC + SubprocessExecutor + DockerStub + dep_sync.py
   │   ├── llm/              — client, prompts, pydantic schema
   │   └── session/          — Session dataclass + SQLite store + lazy-LRU manager (see below)
   ├── models/dto.py        — request/response dataclasses
   └── utils/               — slugify, validation
volumes/
   ├── uploads/             — original full-res, keyed by image_id
   ├── outputs/             — results: outputs/{session_id}/{run_id}/slot{N}/ (unique dir per run+slot)
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

### LLM Chat Turn (any session)
```
Browser → POST /api/sessions/{id}/message {prompt}   [@login_required, owner-scoped]
  → build system prompt (image metadata + op catalog + user's custom nodes + contract)
  → response_type "pipeline" (v3 P2, preferred when the request decomposes into
    catalog ops): save any inline custom nodes (AST-checked) → runner.validate()
    → invalid: feed issues back as an internal turn, retry within the budget
    → valid: run_pipeline_for_session() → outputs linked by source_turn_index
  → LLM client.send_turn() → LLMTurnResult (JSON-validated)
  → SubprocessExecutor.execute(code, input_path, output_dir, timeout)
    → AST safety check → reject or run
    → subprocess.run → capture stdout/stderr → ExecutionResult
  → if file_exists & done → persist GeneratedImage → SSE "done"
  → else → loop (max MAX_LOOP_ITERATIONS)
  → SSE events stream progress to browser
```

### Manual Run (any session)
```
Browser → POST /api/sessions/{id}/run-code {code}   [@login_required, owner-scoped]
  → SubprocessExecutor.execute(code, input_path, output_dir, timeout)
  → return ExecutionResult directly (no LLM, no retry)
  → if file_exists → persist GeneratedImage
```
A session has no locked "mode" (see DECISIONS D-018) — the two flows above are
just two write paths into the same session's `llm_conversation`/`current_code`/
`outputs`, freely interleaved.

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
- A session has no locked mode — LLM chat and manual run-code are two write paths into
  the same session, sharing the same Executor, function contract, and whitelist (D-018).
- Session state is durable: SQLite (`volumes/sessions.db`, absolute path from config) is the
  source of truth; `SessionManager` is a capped lazy-LRU cache over it (Section 5 of the spec).
  Every turn — including loop-internal execution-feedback turns (`internal=1`) — and every
  output is written through to SQLite in the request that produced it. Image bytes stay in
  volumes/; image metadata is persisted in the `images` table and rehydrated at startup.
- Every session is owned by a `user_id`; every session route is `@login_required` and
  resolves access via `manager.get_session_for_user(id, user_id)` — cross-user access
  returns 404, not 403, to avoid confirming another user's session exists.
- Metadata formatting is shared between system prompt and UI — one function.

## Pipeline Execution (v3 — D-026)

```
Browser/LLM → POST /api/sessions/{id}/run-pipeline {pipeline_id | graph}
  → runner.validate(graph)          ← types, cycles, params; 400 with issues[] if invalid
  → per attached image, executor.run_pipeline_subprocess():
      resource-limited child (BLAS caps, rlimits, timeout — D-013)
      NO import whitelist: registry ops are trusted code
      runner.run() walks the topological order, coercing values between ports
      writes per-node preview JPEGs + final PNGs
  → finals persisted as GeneratedImage (graph JSON stored as `code` provenance)
  → node_start/node_done/node_previews/output_saved stream over the session's SSE queue
```

Trust boundary: **registry ops** are our own code and run without an import
whitelist; **custom nodes** (LLM- or user-authored) are AST-checked with the
run-code sandbox checker when SAVED, then exec'd inside the same limited child.

## GPU Model Lifecycle (v3 P4 — D-030)

```
pipeline child (sandboxed, per run)          model sidecar (one per host, long-lived)
  runner.run()                                  ModelManager
    node with requires_model ──── unix socket ────▶ acquire(model)
      arrays via .npy in MODEL_IPC_DIR              ├─ load on first use only
    ◀──────────── output .npy paths ────────────    ├─ refcount while in use
                                                    ├─ reaper unloads after idle
                                                    └─ LRU evict over VRAM budget
```

Model ops never execute in the pipeline child — it has no model, and it dies
after every run. Classical ops never touch the sidecar.

**The constraint is enforced, not documented:** `models/guard.py` AST-scans
`pipeline/ops/**` inside `registry._ensure_loaded()` and raises
`ModelPolicyError` at startup if any op loads weights directly (`torch.hub.load`,
`.from_pretrained`, `.cuda()`, …). The only sanctioned path is
`@register_model` in `core/models/definitions.py` + `@register_model_op`, which
injects an already-loaded handle. See CODING_STYLE "Adding a model".

Trust boundary note: the sidecar runs unrestricted (no `RLIMIT_AS` — CUDA needs
the address space, D-029) because it only ever executes our own model code.
User/LLM code stays in the whitelisted, resource-capped sandbox as before.
