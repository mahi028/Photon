# API Reference

> Keep this file accurate as endpoints are added or changed.

## Base URL

`http://localhost:5000`

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

## POST `/api/windows`

Create a new window.

**Request:**
```json
{ "image_id": "uuid4-hex", "mode": "llm" }
```
or `"mode": "manual"`

**Response 201:**
```json
{ "window_id": "uuid4-hex" }
```

---

## GET `/api/windows`

List all active windows.

**Response 200:**
```json
[
  { "window_id": "...", "mode": "llm", "image_id": "...", "status": "idle", "created_at": "..." }
]
```

---

## DELETE `/api/windows/<window_id>`

Close and clean up a window (in-memory state dropped; files on disk retained).

**Response 204**

---

## POST `/api/windows/<window_id>/message`

Send a user prompt to an LLM window. Kicks off generate→execute→fix loop.

**Request:**
```json
{ "prompt": "convert to grayscale and invert" }
```

**Response 202:**
```json
{ "task_id": "uuid4-hex" }
```
Poll progress via `/stream`.

---

## POST `/api/windows/<window_id>/stop`

Request the in-flight LLM loop to stop (`mode == "llm"` only). Honored at the
next safe boundary — before the next LLM call or sandbox execution; an
execution already in flight runs to completion/timeout. Idempotent; a no-op
on an idle window. The loop emits a `stopped` SSE event and persists a
"⏹ Stopped by user." chat turn.

**Response 202:**
```json
{ "status": "stop_requested", "window_id": "uuid4-hex" }
```

---

## GET `/api/windows/<window_id>/stream`

SSE stream of LLM loop progress. `mode == "llm"` only.

**Events:**
```
data: {"event": "attempt", "attempt": 1, "max": 6, "message": "Generating code..."}
data: {"event": "exec_result", "attempt": 1, "file_exists": false, "stderr": "..."}
data: {"event": "done", "message": "...", "image_id": "...", "preview_url": "..."}
data: {"event": "error", "message": "gave up after 6 attempts", "last_error": "..."}
data: {"event": "stopped", "message": "⏹ Stopped by user."}
```

---

## POST `/api/windows/<window_id>/run-code`

Single sandboxed run (manual mode only).

**Request:**
```json
{ "code": "def main(input_path, output_path_dir):\n    ..." }
```

**Response 200:**
```json
{
  "stdout": "...",
  "stderr": "...",
  "traceback": null,
  "time_taken_seconds": 1.23,
  "file_exists": true,
  "output_path": "volumes/outputs/window_id/...",
  "timed_out": false,
  "image_id": "uuid4-hex",
  "preview_url": "/static/previews/..."
}
```

---

## GET `/api/windows/<window_id>/outputs`

List all GeneratedImages for a window.

**Response 200:**
```json
[
  {
    "image_id": "...",
    "window_id": "...",
    "description": "manual_run_1",
    "path": "...",
    "preview_path": "...",
    "preview_url": "...",
    "produced_at": "...",
    "source_iteration": 1
  }
]
```

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

## POST `/api/download`

Download a zip of selected full-res output images.

**Request:**
```json
{ "image_ids": ["uuid4-hex", "uuid4-hex"] }
```

**Response 200:** `application/zip` streamed binary
