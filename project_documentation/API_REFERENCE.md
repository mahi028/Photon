# API Reference

> Keep this file accurate as endpoints are added or changed.

## Base URL

`http://localhost:5000`

---

## Auth

All `/api/*` routes below (except where noted) require an authenticated session
cookie (`@login_required`) — `POST /auth/signup` or `POST /auth/login` first.
Unauthenticated API calls return `401`. See DECISIONS D-017.

- `GET /auth/login` — login/signup page (Google button shown only if configured)
- `GET /auth/config` — `{ "google_oauth_enabled": bool }`
- `POST /auth/signup` — `{username, email, password}` → `201 {user_id, username}`
- `POST /auth/login` — `{username_or_email, password}` → `200 {user_id, username}`
- `GET /auth/login/google`, `GET /auth/callback/google` — 404 unless configured
- `POST /auth/logout`
- `GET /auth/me` — `200 {user_id, display_name, avatar_url}` or `401`

---

## POST `/api/upload`

Upload an image (multipart or URL).

**Request** (multipart/form-data):
```
file: <binary>
```
OR (application/json):
```json
{ "url": "https://example.com/image.tiff" }
```

**Response 200:**
```json
{
  "image_id": "uuid4-hex",
  "metadata": {
    "image_id": "...",
    "original_filename": "sample.tif",
    "shape": [512, 512, 8],
    "dtype": "float32",
    "channel_count": 8,
    "size_bytes": 2097152,
    "size_mb": 2.0,
    "guessed_kind": "multichannel",
    "value_range": [0.0, 1.0],
    "path": "volumes/uploads/...",
    "preview_path": "volumes/uploads/..._preview.jpg"
  },
  "preview_url": "/static/previews/uuid4-hex_preview.jpg"
}
```

**Errors:** 400 (bad ext/size), 422 (unreadable)

---

## GET `/api/llm/info`

Active LLM provider and model. For `openrouter`, includes the selectable model
catalog (fetched from `GET https://openrouter.ai/api/v1/models` with the
configured key, cached in memory for 1h).

**Response 200:**
```json
{
  "provider": "openrouter",
  "model": "openai/gpt-4o-mini",
  "model_selectable": true,
  "models": ["ai21/jamba-large-1.7", "..."]
}
```
(`models` is `[]` and `model_selectable` is `false` for gemini/openai;
`models_error` is set if the catalog fetch failed.)

---

## POST `/api/llm/model`

Switch the active OpenRouter model at runtime (validated against the catalog;
in-memory only — resets to `OPENROUTER_MODEL` on restart). 400 for other providers.

**Request:** `{ "model": "ai21/jamba-large-1.7" }`
**Response 200:** `{ "status": "ok", "model": "ai21/jamba-large-1.7" }`

---

## POST `/api/sessions`

Create a new session (owned by the current user). A session has no locked
mode — LLM chat and manual run-code both work in the same session (D-018).

**Request:** `{ "image_ids": ["uuid4-hex", ...], "title": "optional" }`
(also accepts a single `"image_id"`; `image_ids` may be `[]` — attach later via
`POST /api/sessions/<id>/images`)

**Response 201:** `{ "session_id": "uuid4-hex" }`

---

## GET `/api/sessions`

List the current user's sessions (summaries, sorted by `updated_at` desc). Never
hydrates full history — cheap even with long-running sessions.

**Response 200:**
```json
[
  { "session_id": "...", "title": "...", "status": "idle", "image_id": "...",
    "image_count": 1, "created_at": "...", "updated_at": "..." }
]
```

---

## GET `/api/sessions/<session_id>`

Owner only (404 otherwise — not 403, to avoid confirming existence to non-owners).

**Response 200:** full `Session.to_dict()` — `session_id, user_id, title,
created_at, updated_at, image_id, image_ids, status, output_count`.

---

## PATCH `/api/sessions/<session_id>`

Rename. **Request:** `{ "title": "New title" }` → **Response 200**

---

## DELETE `/api/sessions/<session_id>`

Smart delete (owner only, D-020) — deletes **everything** this session owns:
the whole outputs directory AND every source image it references, except an
image still referenced by another session (kept). Always a full delete
regardless of sharing (D-019): clones made from this session are independent
and unaffected. Cascades messages, outputs, and any active share links.

**Response 204**

---

## POST `/api/sessions/<session_id>/images` · DELETE `/api/sessions/<session_id>/images/<image_id>`

Attach / detach an already-uploaded image. **Request (POST):** `{ "image_id": "..." }`
**Response 200:** updated `Session.to_dict()`

---

## POST `/api/sessions/<session_id>/message`

Send a user prompt. Kicks off the generate→execute→fix loop in a background thread.

**Request:** `{ "prompt": "convert to grayscale and invert" }`
**Response 202:** `{ "status": "started", "session_id": "..." }` — progress via `/stream`.

---

## POST `/api/sessions/<session_id>/stop`

Request the in-flight loop to stop. Honored at the next safe boundary — before
the next LLM call or sandbox execution; an execution already in flight runs to
completion/timeout. Idempotent. Emits a `stopped` SSE event + persists a
"⏹ Stopped by user." chat turn.

**Response 202:** `{ "status": "stop_requested", "session_id": "..." }`

---

## GET `/api/sessions/<session_id>/stream`

SSE stream of loop progress.

**Events:**
```
data: {"event": "attempt", "attempt": 1, "max": 6, "message": "Generating code..."}
data: {"event": "exec_result", "attempt": 1, "file_exists": false, "stderr": "..."}
data: {"event": "output_saved", "image_id": "...", "preview_url": "..."}
data: {"event": "error", "message": "gave up after 6 attempts", "last_error": "..."}
data: {"event": "stopped", "message": "⏹ Stopped by user."}
```
Pipeline turns (v3 P2) additionally emit `pipeline_invalid` (with `issues[]`),
`pipeline_start`, `node_start`/`node_done`/`node_error`, `node_previews`,
`pipeline_error`, and `custom_nodes_saved`.

---

## POST `/api/sessions/<session_id>/run-code`

Single sandboxed run — no LLM, no retry loop. Works in any session (not mode-gated).

**Request:** `{ "code": "def main(input_path, output_path_dir):\n    ..." }`
**Response 200:**
```json
{
  "stdout": "...", "stderr": "...", "traceback": null,
  "time_taken_seconds": 1.23, "file_exists": true,
  "output_path": "volumes/outputs/session_id/run1/slot0/...",
  "timed_out": false, "batch_run": true, "all_success": true
}
```

---

## GET `/api/sessions/<session_id>/outputs`

**Response 200:**
```json
[
  { "image_id": "...", "session_id": "...", "description": "manual_run_slot0_1",
    "path": "...", "preview_path": "...", "preview_url": "...",
    "produced_at": "...", "source_iteration": 1 }
]
```

---

## GET `/api/sessions/<session_id>/history`

Full conversation + attached-image metadata for restoring a session in the UI.

**Response 200:** `title, image_id, image_ids, images[], metadata, preview_url,
llm_conversation[], current_code`

Each turn in `llm_conversation` carries `response_type` (`chat`|`code`|`pipeline`|`error`),
`code`, `pipeline` (graph JSON for pipeline turns, v3 P2), `was_executed`,
`execution_result`, `turn_index`, `internal`.

---

## POST `/api/sessions/<session_id>/share`

Create a share link. Opening it does **not** grant access to this session — it
deep-clones the session (full file duplication) into the visitor's own account.
See D-019.

**Request:** `{ "reusable": false }` (default `false` — one-time)
**Response 200:** `{ "token": "...", "url": "/shared/<token>", "reusable": false }`

---

## DELETE `/api/sessions/<session_id>/share`

Revoke ALL active share links for this session (owner only). Already-created
clones are unaffected.

**Response 204**

---

## GET `/shared/<token>`

`@login_required`. Resolves the token, clones the session into the current
user's account, and redirects to `/session/<new_id>`. 404 if the token was
never issued (or the source session was deleted); 410 if one-time and already
used. Random guesses 404 — tokens are `secrets.token_urlsafe(32)`.

---

## GET `/api/examples/code`

Returns example code + contract explanation.

**Response 200:**
```json
{
  "code": "def main(input_path, output_path_dir):\n    ...",
  "explanation": "..."
}
```

---

## POST `/api/sessions/<session_id>/download`

Download a zip of selected full-res output images from **one** session (D-020;
owner only). Omit/empty `image_ids` → all of this session's outputs.

**Request:** `{ "image_ids": ["uuid4-hex", ...] }` (optional)
**Response 200:** `application/zip` streamed binary
**Errors:** 404 (session not found / not owned / no matching outputs)

---

# Pipelines (v3 — see photon_v3_plan.md, DECISIONS D-026)

All `@login_required`, owner-scoped (404, not 403). Presets (`user_id NULL`)
are readable by everyone and never writable — clone them to edit.

## GET `/api/ops`
Registry catalog for the editor palette + client-side type checking.
**200:** `{ops:[{name,category,inputs[],outputs[],params[],doc,requires_model}], port_types[], coercions[], custom_nodes[]}`

`requires_model` is non-null for GPU model ops — those execute in the model
sidecar, which loads the model on demand and unloads it after idle (D-030).

## GET · POST `/api/pipelines`
List (user's + presets) · create. **POST body:** `{title?, description?, graph?}`
or `{clone_from: "<pipeline_id>", title?}` to clone a preset/own pipeline. **201:** `{pipeline_id}`

## GET · PATCH · DELETE `/api/pipelines/<pipeline_id>`
Read (own or preset) · update (own only) · delete (own only). PATCH body:
`{title?, description?, graph?}`. Presets return 404 on PATCH/DELETE.

## POST `/api/pipelines/validate`
**Body:** `{graph}` → **200:** `{valid: bool, issues: [{node_id, message, severity}]}`
Checks unknown ops, port-type compatibility, missing required inputs, param
ranges, duplicate/multiple edges into one port, and cycles.

## GET · POST `/api/custom-nodes` · DELETE `/api/custom-nodes/<id>`
The user's LLM/hand-authored node library. **POST body:**
`{name, code, ports:{inputs[],outputs[]}, params?[], doc?}` — code must define
`node(inputs, params)` and is AST-checked at save time with the run-code
sandbox checker (rejected → 400). Upserts by `(user_id, name)`.

## POST `/api/sessions/<session_id>/run-pipeline`
**Body:** `{pipeline_id}` or `{graph, title?}`. Validates synchronously
(**400** with `issues[]` if invalid), then runs against every image attached
to the session in a background thread. **202:** `{status:"started", session_id, title}`
Progress streams over the session's existing SSE endpoint:
```
{"event":"pipeline_start","image_slot":0,"title":"...","node_count":5}
{"event":"node_start"|"node_done"|"node_error","node_id":"...","image_slot":0}
{"event":"node_previews","image_slot":0,"previews":{"<node_id>":"/outputs/..."}}
{"event":"pipeline_error","failed_node":"...","message":"..."}
{"event":"output_saved","image_id":"...","preview_url":"...","description":"..."}
```
Final outputs become normal `GeneratedImage` rows (filmstrip/download/Preview
Output all work); the pipeline graph JSON is stored as their `code` provenance.

---

## Page routes (v3 P3)

- `GET /pipelines` — pipeline library (yours + presets). `@login_required`.
- `GET /pipelines/<pipeline_id>` — Drawflow editor. `@login_required`;
  presets open read-only with a Clone-to-edit action.
