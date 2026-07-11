# Project Spec: LLM-Driven Image Feature/Transformation Visualizer

## 0. Prompt Preamble (read this first, agent)

You are building a Flask + Jinja2 + Tailwind web application that lets a user
upload an arbitrary-channel image (RGB, grayscale, RGBA, or n-channel
scientific/multispectral image), describe a feature or transformation in
natural language, and have an LLM (Anthropic Claude) **write and iteratively
debug Python code** that performs that transformation. The code runs in a
sandboxed executor, not in the main process. The LLM sees stdout/stderr/
traceback from each run and keeps fixing its own code until it succeeds or
gives up, in a closed loop. The user can run many such conversations in
parallel, each in its own "window" (like browser tabs), each with its own
image (or a copy of the same image) and its own LLM session. Users can also
open a **self-programming window** where they write and run their own code
against the same sandbox/function contract, with no LLM involved.

Within an LLM window, not every user turn should trigger code generation —
the user may just want to ask questions, discuss strategy, or ask about code
already produced. Section 7 defines exactly how the LLM signals "this is a
chat reply" vs. "this is code to execute," and Section 11 defines how each
renders differently in the UI. Every turn or output that involves real,
executed code must offer a "Preview Code" button showing that exact code,
well-formatted, in a dialog.

Windows must survive a page reload or server restart — Section 5 defines a
lightweight SQLite-backed persistence layer with lazy, capped in-memory
hydration, so you are never keeping every open window's full history in RAM
at once, only the handful actually being viewed.

Build this as a modular, scalable codebase — not a single-file prototype.
Favor clear interfaces (especially around the sandbox, the LLM client, and
the session store) so individual pieces can be swapped later (e.g.
subprocess sandbox → Docker sandbox, or SQLite → a hosted DB) without
touching calling code. Where you must make a judgment call not covered by
this spec, prefer the simplest correct option and leave a `# TODO(spec):`
comment explaining the tradeoff, rather than silently guessing.

**Hard requirement — living documentation.** This spec is your starting
point, not your ongoing reference. From the first commit, create and
maintain a `project_documentation/` folder (detailed in Section 13) and
treat it as the source of truth for architecture, design, conventions, and
plan/status as the build progresses. Consult it before making any decision
this spec doesn't explicitly pin down, and update it in the same work
session as any code change that affects architecture, API surface, data
models, conventions, or plan status. Do not let it drift out of sync with
the code. Any deviation from this spec (including the protocol change in
Section 7.3, which supersedes an earlier version of this document) must be
logged with a date in `DECISIONS.md`.

---

## 1. Goals / Non-Goals

**Goals**

- Support any channel count/dtype image, not just 8-bit RGB.
- Turn natural-language requests into runnable, self-correcting Python.
- Also let a user write and run their own Python against the same contract
  and sandbox, no LLM required (self-programming window).
- Let a user chat with the LLM about existing code, ask questions, or
  discuss strategy in an LLM window **without** forcing a code execution on
  every turn — code execution should happen only when the LLM (or the user's
  intent) actually calls for it.
- Let the user preview the exact, well-formatted code behind any executed
  turn or generated output, on demand, via a dialog.
- Run generated/user code in isolation from the Flask process and host
  filesystem.
- Let a user have multiple independent windows open at once — LLM-driven or
  self-programmed — each with its own image (or shared image) and history.
- Persist every window's state so it survives a page reload or server
  restart, without keeping every window fully loaded in memory at all times.
- Let a user share a specific window as a standalone link that still works
  after they've deleted it from their own view.
- Show input/output images side by side; allow downloading full-res results
  as a zip with a selection dialog.
- Always show the user the exact image metadata the backend/LLM is working
  from, and always give them a copyable reference for the code contract.

**Non-Goals (MVP)**

- Multi-user auth/accounts (assume single local/trusted user for now — see
  Open Questions). Note this also means the persistence/eviction cap in
  Section 5 is process-global, not truly per-device, since there's no
  device/user identity to key it on yet.
- Real-time collaborative editing of the same window by multiple browser tabs.

---

## 2. High-Level Architecture

```
Browser (Jinja2 + Tailwind + Alpine.js)
   |  REST + SSE
   v
Flask App (src/backend)
   |-- Upload/Metadata service
   |-- Session/Window manager (lazy LRU cache over a SQLite store — Section 5)
   |-- LLM client (Anthropic API, prompt templates)          [mode="llm" only]
   |-- Sandbox executor (subprocess, resource-limited; Docker-ready interface)
   |-- Image store (volumes/uploads, volumes/outputs)
```

Both window modes ("llm" and "manual") funnel code through the _same_
sandbox executor and the _same_ function contract — the only difference is
who writes the code (the LLM, in a loop, vs. the user, one run at a time)
and whether execution failures get auto-fed-back for a retry or surfaced
directly to the user to edit themselves. Within an "llm" window, not every
turn produces code at all — see Section 7.3.

The Flask app never `exec`s any generated or user-submitted code in-process.
It always shells out to the sandbox executor, which is the only component
allowed to touch untrusted code.

The Flask app also never treats "in memory" as the only copy of a window's
state — every window lives durably in SQLite (Section 5), and what's in
memory at any moment is just a bounded cache of whichever windows are
actively being viewed.

---

## 3. Folder Structure

```
root/
├── project_documentation/            # see Section 13 — mandatory, living docs
│   ├── PROJECT_SPEC.md               # this spec, kept current as it evolves
│   ├── ARCHITECTURE.md
│   ├── DESIGN.md
│   ├── CODING_STYLE.md
│   ├── API_REFERENCE.md
│   ├── PLAN.md
│   └── DECISIONS.md
├── src/
│   ├── backend/
│   │   ├── app.py                     # Flask app factory, blueprint registration
│   │   ├── config.py                  # env-driven config (model name, limits, paths)
│   │   ├── wsgi.py                    # entrypoint for gunicorn
│   │   ├── api/
│   │   │   ├── __init__.py
│   │   │   ├── upload_routes.py       # POST /api/upload
│   │   │   ├── window_routes.py       # window CRUD (mode: llm | manual), share/unshare
│   │   │   ├── shared_routes.py       # GET /shared/<token> — focused single-window view
│   │   │   ├── chat_routes.py         # LLM message send, history, SSE stream
│   │   │   ├── manual_routes.py       # self-programming / ad-hoc snippet run endpoint
│   │   │   └── download_routes.py     # zip export
│   │   ├── core/
│   │   │   ├── llm/
│   │   │   │   ├── client.py          # thin wrapper around Anthropic SDK
│   │   │   │   ├── prompts.py         # system prompt + templates (section 7)
│   │   │   │   └── schema.py          # pydantic models for LLM I/O contract
│   │   │   ├── sandbox/
│   │   │   │   ├── base.py            # Executor ABC: execute(code, input_path, output_dir, timeout)
│   │   │   │   ├── subprocess_executor.py   # MVP implementation
│   │   │   │   └── docker_executor.py       # stub for future hardening
│   │   │   ├── image/
│   │   │   │   ├── metadata.py        # shape/dtype/size/channel introspection
│   │   │   │   ├── thumbnail.py       # downsample for web preview
│   │   │   │   └── composite.py       # n>4 channel -> viewable composite
│   │   │   └── session/
│   │   │       ├── store.py           # SQLite-backed durable store (Section 5)
│   │   │       ├── window.py          # Window dataclass + lifecycle (mode-aware)
│   │   │       └── manager.py         # lazy LRU cache over store.py — NOT the source of truth
│   │   ├── examples/
│   │   │   └── example_code.py        # canonical example shown in "View Example Code"
│   │   ├── models/
│   │   │   └── dto.py                 # request/response dataclasses
│   │   └── utils/
│   │       ├── slugify.py
│   │       └── validation.py          # file type/size checks
│   └── frontend/
│       ├── templates/
│       │   ├── base.html
│       │   ├── index.html
│       │   ├── shared_window.html      # focused view for a shared-link visitor
│       │   └── partials/
│       │       ├── upload_panel.html
│       │       ├── metadata_block.html     # copyable metadata code block
│       │       ├── window_tab.html
│       │       ├── chat_pane.html          # mode="llm" — renders bubbles AND code-result cards
│       │       ├── code_editor_pane.html   # mode="manual"
│       │       ├── code_dialog.html        # SHARED: example code, preview-executed-code, illustrative snippets
│       │       ├── image_compare.html
│       │       ├── download_dialog.html
│       │       └── share_dialog.html       # shows the shareable link + copy button
│       └── static/
│           ├── css/
│           │   ├── input.css          # tailwind directives
│           │   └── output.css         # built (gitignored)
│           └── js/
│               ├── windowManager.js    # tab create/switch/close, mode selection
│               ├── uploader.js         # progress bar upload flow
│               ├── chat.js             # sends messages, renders bubbles/cards, wires "Preview Code"
│               ├── codeEditor.js       # self-programming pane (run/edit, save-on-run)
│               ├── codeDialogViewer.js # shared dialog: renders/highlights/copies any code string
│               ├── imageViewer.js      # side-by-side, zoom, per-output "View Code"
│               ├── downloadDialog.js   # selection + zip request
│               └── shareDialog.js      # requests a share link, shows/copies it
├── volumes/
│   ├── uploads/            # original full-res images, keyed by image_id
│   ├── outputs/            # generated result images, keyed by window_id/ (with sibling .py files — see 10.1)
│   ├── tmp_exec/           # per-execution scratch dirs (auto-cleaned)
│   └── sessions.db         # SQLite store — the durable source of truth for windows (Section 5)
├── requirements.txt
├── package.json            # tailwind build tooling only
├── tailwind.config.js
├── .env.example
└── README.md
```

---

## 4. Data Models

```python
# ImageMetadata
image_id: str
original_filename: str
shape: tuple[int, ...]        # (H, W, C) or (H, W) for grayscale
dtype: str                    # "uint8", "uint16", "float32", ...
channel_count: int
size_bytes: int
size_mb: float
guessed_kind: str             # "rgb" | "rgba" | "grayscale" | "multichannel"
value_range: tuple[float, float]  # observed min/max, per-channel if useful
path: str                     # full-res path in volumes/uploads
preview_path: str             # downsampled preview/composite path

# Window (a single tab — either an LLM chat session or a self-programming pad)
window_id: str
mode: str                     # "llm" | "manual"
created_at: datetime
image_id: str                 # base image this window operates on
llm_conversation: list[Message] | None   # populated only when mode == "llm"
current_code: str | None                 # populated only when mode == "manual";
                                          # persisted (Section 5) only when "Run" is clicked
outputs: list[GeneratedImage]     # every image produced in this window
status: str                   # "idle" | "running" | "error"
share_token: str | None        # set once the window has been shared (Section 5.4)
is_shared: bool                # true once shared; changes DELETE semantics (Section 5.4)

# Message  (one turn in an "llm" window's conversation)
role: str                     # "user" | "assistant"
response_type: str | None     # "chat" | "code"; None for user-authored turns
message: str                  # human-readable text — always present
code: str | None              # present when response_type == "code" (executed),
                               # OR optionally when response_type == "chat" (an
                               # illustrative, NON-executed snippet — see 7.3)
was_executed: bool            # true only if this message's code actually ran
execution_result: ExecutionResult | None  # populated only if was_executed
turn_index: int

# GeneratedImage
image_id: str
window_id: str
description: str              # LLM-provided name, or user-provided/derived name for manual runs
path: str                     # full-res output path
preview_path: str
code: str                     # exact code that produced this image (also on disk, see 10.1)
source_turn_index: int | None # links back to the Message that produced it, if from an "llm" window
produced_at: datetime
source_iteration: int         # which loop attempt (llm) or run number (manual) produced it

# ExecutionResult (internal, not persisted beyond the owning Message/GeneratedImage row)
stdout: str
stderr: str
traceback: str | None
time_taken_seconds: float
file_exists: bool
output_path: str | None
timed_out: bool
```

---

## 5. Persistence & Session Lifecycle

Windows must survive a page reload and a server restart. The design goal is:
**SQLite is always the source of truth; the in-memory `WindowManager` is a
small, capped cache of whichever windows are actively being viewed** — not a
mirror of everything that exists.

### 5.1 Store (`core/session/store.py`)

A thin SQLite wrapper (stdlib `sqlite3`, no new dependency) backing
`volumes/sessions.db`. Roughly three tables:

- `windows` — `window_id, mode, image_id, created_at, status, current_code,
share_token, is_shared`
- `messages` — `window_id, turn_index, role, response_type, message, code,
was_executed, execution_result_json`
- `outputs` — `window_id, image_id, description, path, preview_path, code,
source_turn_index, produced_at, source_iteration`

Only text and file paths live in SQLite — the actual image bytes stay
exactly where they already are, in `volumes/uploads/` and
`volumes/outputs/`, so the DB stays small regardless of history length.

Expose a small interface mirroring the `Executor` pattern so the backend
could swap SQLite for something else later without touching callers:

```python
def load_window(window_id: str) -> Window | None: ...
def save_window(window: Window) -> None: ...
def list_window_summaries() -> list[WindowSummary]: ...
def append_message(window_id: str, message: Message) -> None: ...
def append_output(window_id: str, output: GeneratedImage) -> None: ...
def delete_window(window_id: str) -> None: ...
def resolve_share_token(token: str) -> str | None:  # -> window_id
```

`WindowSummary` is a lightweight projection (`window_id, mode, image_id,
created_at, status` — no messages/outputs) used purely for the tab bar.

### 5.2 Lazy LRU cache (`core/session/manager.py`)

- Holds an in-memory `{window_id: Window}` cache capped at
  `MAX_HYDRATED_WINDOWS` (config, default **5**).
- `list_windows()` **never touches the cache** — it calls
  `store.list_window_summaries()` directly, so restoring the tab bar on page
  load is cheap no matter how much history exists or how many windows are
  currently hydrated.
- `get_window(window_id)`:
  - cache hit → return it, mark as most-recently-used.
  - cache miss → `store.load_window(window_id)` (full hydration: all
    messages, outputs, current code), insert into cache; if the cache is now
    over `MAX_HYDRATED_WINDOWS`, evict the least-recently-used entry (evict
    from memory only — it stays untouched in SQLite and will simply be
    reloaded next time it's opened).
- This is a **count-based LRU only** — no idle-timer/background sweep. At
  this app's scale (a handful of tabs, single local user) a time-based
  eviction thread adds complexity without a real benefit; the count cap
  alone keeps memory bounded. If the cap turns out to matter as a UX limit
  later, revisit — for now `MAX_HYDRATED_WINDOWS=5` is generous relative to
  how many tabs someone would realistically keep open.
- Since there's no auth/device identity in this MVP (Section 15, assumption
  6), this cap is enforced globally in the one Flask process, not truly
  per-browser/per-device — flagged explicitly per the Non-Goals note in
  Section 1.

### 5.3 Write-through, not write-behind

Every mutation writes to SQLite in the same request that produced it, and
updates the in-memory cached copy at the same time:

- A new `Message` appended (chat turn, whether `"chat"` or `"code"`) →
  `store.append_message(...)` immediately.
- A `GeneratedImage` created → `store.append_output(...)` immediately.
- A manual window's code → written via `store.save_window(...)` **only when
  "Run" is clicked**, not on every keystroke (confirmed default — see
  Section 15, assumption 11). If the browser is closed with unrun edits in
  the editor, those edits are lost; only the last successfully-run code
  persists. This keeps the write path simple (no debounce endpoint, no
  autosave timer) at the cost of losing unrun drafts, which is an accepted
  tradeoff for MVP.
- A window created/deleted/shared/unshared → written immediately.

This means a mid-conversation crash loses at most the one in-flight
execution that hadn't finished yet (the user just resends that message) —
everything already completed is durable.

### 5.4 Sharing and deletion semantics

- `POST /api/windows/<window_id>/share` generates a `share_token` (e.g. a
  random URL-safe string), sets `is_shared = true`, persists both, and
  returns a shareable URL like `/shared/<share_token>`.
- `GET /shared/<token>` resolves the token to a `window_id` via
  `store.resolve_share_token`, hydrates that window through the same lazy
  path as any other window, and renders `shared_window.html` — a **focused,
  single-window view** showing only that window's chat/editor and outputs,
  not the visitor's own tab bar (they have none) and not any of the owner's
  other windows. This satisfies "open the link, land directly in that
  session."
- **Important, flagged for confirmation**: since there's no auth model,
  opening a share link grants full interactive access to that window — a
  visitor can continue chatting (in an "llm" window) or running code (in a
  "manual" window) against your sandbox and your image, not just view it
  read-only. If you'd rather shared links be view-only, that's a real design
  change (need a separate read-only rendering path and a route guard on the
  message/run-code endpoints for shared-but-not-owned windows) — flag if you
  want that instead; the current spec assumes interactive sharing since
  that's the simpler default and matches how the request was phrased.
- `DELETE /api/windows/<window_id>`:
  - if `is_shared == false`: a real, full delete — removes the `windows`
    row, all `messages` and `outputs` rows, and the window's output image
    files under `volumes/outputs/{window_id}/`.
  - if `is_shared == true`: removes the window from the _owner's own_ tab
    list/session view only (e.g. a `hidden_from_owner` flag, or simply: the
    owning browser's local tab-bar state stops referencing it — since there's
    no per-user ownership tracking yet, the simplest MVP implementation is
    "shared windows are never deleted by DELETE, only by a separate explicit
    'unshare and delete' action"). Note this simplification in
    `DECISIONS.md` and revisit if multi-user ownership is added later.
  - "Unshare" (a distinct action, e.g. `DELETE /api/windows/<window_id>/share`)
    clears `share_token`/`is_shared`; a subsequent normal `DELETE` then
    performs the real full delete.

---

## 6. Upload & Metadata Flow

1. `POST /api/upload` accepts either a multipart file or a `url` field.
   - If URL: fetch server-side, validate content-type/size before saving.
   - Validate extension against an allow-list (`.png .jpg .jpeg .tif .tiff
.npy .bmp .webp`), and cap file size (config: `MAX_UPLOAD_MB`, default 200).
2. Save the original, untouched, to `volumes/uploads/{image_id}.{ext}`.
3. Load with a format-aware reader:
   - Standard formats → Pillow.
   - `.tif/.tiff` and anything Pillow can't introspect fully (arbitrary
     channel counts, float32 data) → `tifffile` or `numpy.load` for `.npy`.
4. Compute `ImageMetadata` as above. For `channel_count > 4`, do **not**
   attempt to save a directly viewable PNG of the raw array — generate a
   composite for `preview_path` instead (see Section 10.3).
5. Build a downsampled preview (`thumbnail.py`, max long-edge ~1024px,
   format-appropriate: PNG/JPEG for display) for **every** upload regardless
   of channel count, so the frontend always has something safe to `<img>`.
6. Return `{image_id, metadata, preview_url}` to the frontend. Full-res stays
   server-side only until explicitly downloaded.
7. **Always render the returned metadata as a read-only, copyable code
   block** in the UI (`metadata_block.html`, formatted as pretty-printed
   JSON) — not just prose — so the user can see exactly what fields exist
   and their exact values/keys. This block is shown in the upload panel for
   the image itself, and is also surfaced inside every window attached to
   that image (both "llm" and "manual" modes) so a user writing code by hand
   never has to leave the window to check `shape`/`dtype`/`channel_count`.

---

## 7. LLM Integration (applies only to `mode == "llm"` windows)

### 7.1 Client

`core/llm/client.py` wraps the Anthropic Python SDK. Model name, max_tokens,
and temperature come from `config.py` (env-configurable; default model:
`claude-sonnet-4-6` — confirm this string is current before hardcoding, model
identifiers change). The client function signature should be:

```python
def send_turn(conversation: list[Message], system_prompt: str) -> LLMTurnResult
```

Where `LLMTurnResult` is the parsed, validated JSON described in 7.3 (use
`schema.py` pydantic models to validate and raise a clear error if the model
returns malformed JSON — do not regex-scrape it).

### 7.2 System Prompt (`prompts.py`)

The system prompt must be assembled per-window at conversation start and
should include, at minimum:

- **Role framing**: "You are an image-processing assistant. Most of the
  time you write a single Python function that gets executed in an isolated
  sandbox with no network access — but not every message needs code. If the
  user is asking a question, discussing strategy, or asking about code you
  already wrote, just answer in plain language. Only produce executable code
  when the user's request calls for an actual transformation to be run."
- **Image metadata block**: shape, dtype, channel count, size, guessed kind,
  value range — formatted as a clear key-value block, not prose. This is the
  exact same metadata the user sees in their copyable code block (Section 6,
  point 7) — keep the two in sync via one shared formatting function.
- **Available libraries**: explicit whitelist (see 7.4) — state that imports
  outside this list will fail in the sandbox.
- **Function contract** (verbatim requirement, see 7.3 — identical contract
  used in the self-programming window, Section 8 — applies whenever code
  will actually be executed).
- **Output JSON contract** (see 7.3) — tell it to respond with _only_ JSON,
  no markdown fences, no commentary outside the JSON fields.
- **Chat vs. code decision guidance**: explicitly instruct it to set
  `response_type: "chat"` for questions, explanations, strategy discussion,
  or "what does this code do" style requests — optionally including a short,
  non-executed illustrative snippet in the `code` field if that helps answer
  the question — and to set `response_type: "code"` only when it intends
  for something to actually run right now.
- **Loop protocol**: explain that whenever it replies with `response_type:
"code"`, the code is executed immediately and it will be called again
  automatically with the execution results (Section 7.3.1) so it can fix
  problems — this repeats until it replies with `response_type: "chat"`
  (meaning it's satisfied, wants to explain the result, or wants to ask the
  user something) or the iteration budget runs out.
- **Iteration budget**: tell it the auto-retry loop will be force-stopped
  after `MAX_LOOP_ITERATIONS` (config, default 6) consecutive `"code"`
  replies without a `"chat"` reply in between, and that it should prioritize
  a working result over a perfect one if attempts are running low. Include
  the current attempt number/remaining budget in each execution-feedback turn.
- **Multi-channel guidance**: if channel_count > 3, remind it that it must
  decide how to handle channels explicitly (don't silently assume RGB) and
  that PNG output is only valid for ≤4 channels — for higher channel counts,
  it should save as `.npy` or `.tiff` and the harness will auto-generate a
  preview composite.

### 7.3 Function & Output Contract

> **This section supersedes any earlier `status: "in_progress" | "done"`
> protocol.** Continuation is now driven entirely by `response_type`, not by
> a separate status flag. Log this change in `DECISIONS.md` if you find an
> older version of this contract referenced elsewhere.

Whenever code is actually meant to run (in an "llm" window's `"code"` replies,
and in the self-programming window, Section 8), it must define:

```python
def main(input_path: str, output_path_dir: str) -> str:
    """Reads input_path, transforms it, saves result into output_path_dir,
    and returns the full path to the saved file."""
    ...
```

Every LLM reply in an "llm" window's conversation must be **strict JSON**,
matching:

```json
{
  "response_type": "chat",
  "message": "human-readable text shown as a chat bubble",
  "code": null
}
```

```json
{
  "response_type": "chat",
  "message": "human-readable text — e.g. explaining an idea or answering a question",
  "code": "an illustrative python snippet — NOT executed, shown as a formatted code block attached to this chat bubble"
}
```

```json
{
  "response_type": "code",
  "message": "short human-readable note about what this code attempt does",
  "code": "full python source defining main(input_path, output_path_dir) — REQUIRED and WILL be executed immediately"
}
```

Rules:

- `response_type == "chat"` → `code` is **optional**.
  - `null`/absent: a plain conversational reply. No sandbox execution occurs.
    This ends the automatic loop for the current user turn — the user must
    send another message to continue.
  - present: an illustrative snippet the model wants to show but not run
    (e.g. "here's roughly what changing the kernel size would look like").
    The backend must **never** auto-execute this — it's rendered purely as a
    formatted, copyable code block inline in the chat bubble (reusing the
    shared `code_dialog.html`/`codeDialogViewer.js` component from Section
    11.4 for the "Preview" / expand action). This also ends the automatic
    loop, same as the null case.
- `response_type == "code"` → `code` is **required** and must satisfy the
  function contract above. The backend executes it via the sandbox
  immediately, records the `ExecutionResult` against this `Message`, creates
  a `GeneratedImage` if `file_exists` is true, renders a **code-result card**
  (not a plain bubble — see Section 11.2) with a "Preview Code" button, and
  then automatically sends the execution-feedback turn (7.3.1) back to the
  LLM to continue the loop — repeating until a reply comes back with
  `response_type == "chat"` or `MAX_LOOP_ITERATIONS` consecutive `"code"`
  replies is reached.
- On JSON parse failure or contract violation (e.g. `response_type: "code"`
  with `code: null`), do not crash the loop — feed a synthetic
  execution-feedback turn back explaining the contract was violated, and
  count it toward the iteration budget.
- At the iteration cap, stop calling the LLM and inject a locally-authored
  `response_type: "chat"` message ("Stopped after N attempts — last error:
  …") rather than looping forever.

### 7.3.1 Execution Feedback Turn

After any `"code"` reply is executed, construct the next turn (sent as a
user-role message) containing, at minimum:

```
attempt: <n> of <MAX_LOOP_ITERATIONS>
stdout: <...>
stderr: <...>
traceback: <... or null>
time_taken_seconds: <float>
file_exists: <bool>
output_path: <str or null>
timed_out: <bool>
```

so the LLM can decide whether to reply with corrected `"code"`, or switch to
`"chat"` to explain success/failure or ask the user something.

### 7.4 Library Whitelist (enforced, not just documented)

`numpy`, `PIL`/`Pillow`, `cv2` (opencv-python-headless), `scipy`,
`skimage` (scikit-image), `tifffile`. No `matplotlib` unless purely for
colormap LUTs (no GUI backends, no file dialogs). No `os`, `sys`, `subprocess`,
`socket`, `requests`, `shutil` beyond what a static check allows (see 9.2 in
Section 9). This whitelist applies equally to self-programming windows.

---

## 8. Self-Programming Window (`mode == "manual"`)

A window a user creates to write and run their own Python instead of
chatting with an LLM. Same contract, same sandbox, no LLM call anywhere in
this path.

- **Creation**: window creation UI lets the user pick a mode — "Chat with
  LLM" or "Write my own code" — at the point they open a new tab (see
  Section 11.2). A manual window is otherwise identical to an LLM window in
  every other respect: it's attached to an uploaded image, shows the same
  metadata block, produces `GeneratedImage`s that flow into the same
  image-compare view and the same global download dialog, and persists the
  same way (Section 5).
- **Editor**: a code editor pane (`code_editor_pane.html` / `codeEditor.js`)
  pre-populated with the function stub:
  ```python
  def main(input_path: str, output_path_dir: str) -> str:
      # your code here
      ...
      return output_file_path
  ```
  The editor content is retained in the `Window.current_code` field, but
  only **written to SQLite when "Run" is clicked** (Section 5.3) — not on
  every keystroke. Recommend a lightweight in-browser syntax-highlighting
  editor (e.g. CodeMirror 6 via CDN, no bundler needed) rather than a bare
  `<textarea>` — flag this as a suggested dependency addition, same as
  Alpine.js, and confirm before adding.
- **No "Preview Code" button needed here** — the code is already fully
  visible and editable in the pane itself; the shared `code_dialog.html`
  component is for cases where code is buried inside a scrollable chat
  history or an output thumbnail, not for the manual editor.
- **Run button**: `POST /api/windows/<window_id>/run-code {code}` — server
  validates `window.mode == "manual"`, runs the code through the exact same
  `Executor.execute(...)` used by the LLM path (same AST safety check,
  resource limits, timeout), persists `current_code` and, if successful, the
  resulting `GeneratedImage` (Section 5.3), and returns the `ExecutionResult`
  directly — **no LLM call, no auto-retry loop.** If it fails, the
  traceback/stderr is shown to the user in an execution-log panel so _they_
  fix the code and hit Run again.
- **No LLM budget/config concerns**: `MAX_LOOP_ITERATIONS` and the LLM
  system prompt machinery don't apply here — this window type is exempt from
  Section 7 entirely except for sharing its function contract and library
  whitelist.
- _(Optional, stretch)_: the same `run-code` endpoint can be reused to let a
  user explicitly "Run" an illustrative snippet the LLM showed them in an
  "llm" window chat bubble (Section 7.3's second `"chat"` example), if you
  want that affordance — not required for MVP, note in `DECISIONS.md` if built.

---

## 9. Sandbox Executor

### 9.1 Interface (`sandbox/base.py`)

```python
class Executor(ABC):
    def execute(self, code: str, input_path: str, output_dir: str,
                timeout: int) -> ExecutionResult: ...
```

All callers — both the LLM loop (Section 7) and the self-programming "Run"
endpoint (Section 8) — depend on this interface, never on a concrete
implementation, so swapping `SubprocessExecutor` → `DockerExecutor` later is
a one-line change in `config.py`/DI wiring.

### 9.2 MVP implementation: `SubprocessExecutor`

- Write `code` to a temp file inside `volumes/tmp_exec/{run_id}/script.py`,
  appending a small harness that imports the user's `main`, calls it with
  the real input/output paths, times it, and prints a single JSON line to
  stdout with `{"time_taken": ..., "output_path": ...}` so the wrapper can
  parse timing without scraping arbitrary stdout.
- Before execution, run a static AST check that rejects disallowed imports
  and dangerous builtins (`eval`, `exec`, `__import__`, `open` outside the
  provided input/output paths, `os.system`, `subprocess`, `socket`). Reject
  and return a synthetic "blocked by sandbox policy" ExecutionResult without
  ever running the code if it fails this check. Applies identically whether
  the code came from the LLM or a human in a manual window.
- Launch via `subprocess.run` with:
  - `timeout=timeout` (config `EXECUTION_TIMEOUT_SECONDS`, default 30).
  - A restricted environment (`env={}` plus only what's needed).
  - Working directory set to the per-run scratch dir only.
  - Resource limits via `resource.setrlimit` in a `preexec_fn` (CPU time,
    address space/memory, no core dumps).
  - No network — if not containerized, at minimum strip proxy env vars and
    rely on the import blocklist to prevent `socket`/`requests` usage; note
    in code comments that a container/network-namespace approach is stronger
    and is the intended production path (`docker_executor.py`).
- Capture stdout/stderr, detect timeout, check `file_exists` at the returned
  (or expected) output path, and populate `ExecutionResult`.
- Always clean up the per-run scratch dir after copying any output file into
  `volumes/outputs/{window_id}/`.

### 9.3 Future hardening (`docker_executor.py`, stub only for MVP)

Leave a documented stub implementing the same interface, running the script
in a minimal, network-disabled, read-only-except-output-dir container. Don't
build this out in MVP — just make sure the interface doesn't need to change
when it's implemented.

---

## 10. Image Handling Details

### 10.1 Naming and code provenance

Output files: `volumes/outputs/{window_id}/{iso_timestamp}_{slugify(description)}.{ext}`.
For LLM windows, `description` comes from the LLM. For manual windows, derive
a reasonable default (e.g. `manual_run_{n}`) unless you want to let the user
name it in the UI — either is fine, note your choice in `DECISIONS.md`.
Extension chosen by the code based on channel count (PNG/JPEG for ≤4
channels, `.npy` or `.tiff` otherwise).

Alongside every saved output image, also write a sibling source file with
the exact code that produced it (e.g. `foo.png` + `foo.py`). This makes code
provenance for any given output recoverable straight from disk even in an
edge case where the SQLite row is somehow unavailable — the "Preview Code" /
"View Code" buttons (Section 11) can fall back to reading this sibling file
if the persisted `GeneratedImage.code` isn't available for some reason.
Under normal operation, Section 5's write-through persistence means this
fallback shouldn't be needed, but it costs nothing to have as a second line
of defense.

### 10.2 Full-res vs preview

Every image (input or output) gets a same-named `*_preview.{jpg}` alongside
it, generated server-side, max long-edge 1024px. The frontend only ever
requests preview URLs for display; full-res is only touched by the download
endpoint.

### 10.3 Composite for n>4 channels

`core/image/composite.py`: given an array with C>4, produce a viewable
3-channel image via a documented, simple default (e.g. mean-projection
grayscale, or first-3-channels-as-RGB) purely for the preview thumbnail — the
actual saved output file keeps all channels intact. Label this clearly in
the UI ("preview composite — download for full data") so the user isn't
confused about data loss.

---

## 11. Frontend

### 11.1 Stack

Jinja2 for server-rendered shell + Tailwind for styling, per your spec. For
the interactive bits (tabs, chat, live polling/SSE, upload progress, code
editor, dialogs) plain `fetch`-based vanilla JS modules are fine, but
recommend **Alpine.js** (no build step, drops in as a `<script>` tag, plays
well with Jinja2-rendered HTML) to avoid hand-rolling DOM state management,
and **CodeMirror 6** (via CDN) both for the self-programming editor pane and
for read-only syntax highlighting inside the shared code dialog. Flag both
as suggested additions, not hard requirements — confirm before adding.

### 11.2 Layout

- Left/top: upload panel with drag-drop + URL field, progress bar during
  upload, then the (downsampled) image preview once uploaded, plus the
  read-only metadata code block (Section 6, point 7) right below the
  preview, with its own "Copy" button.
- Tab bar ("window manager"): on page load, populated from
  `GET /api/windows` (a cheap summary query, Section 5.2 — no hydration
  happens just from listing tabs). `+` opens a small chooser — "Chat with
  LLM" or "Write my own code" — plus which uploaded image to attach (or
  reuse the current one). Each tab shows a short label and a mode icon (chat
  bubble vs `</>`). `×` closes the tab, calling `DELETE /api/windows/<id>`
  (full delete, or owner-side-hide-only if shared — Section 5.4).
- Each window also has a **"Share" button** in its toolbar opening
  `share_dialog.html`: calls `POST /api/windows/<id>/share`, then shows the
  resulting link with a copy button (`shareDialog.js`).
- Main pane per active tab (hydrated on first open via
  `GET /api/windows/<id>/history`, Section 5.2):
  - **If mode == "llm"**: a chat history that renders two distinct visual
    forms depending on each turn's `response_type`:
    - **Plain chat bubble** — for `response_type == "chat"` with no code, or
      with an illustrative snippet: message text, and if a snippet is
      present, a formatted, collapsed-by-default code block beneath it with
      an "Expand" / "Preview" affordance opening the shared code dialog.
      Never has an execution status, since nothing ran.
    - **Code-result card** — for `response_type == "code"`: the model's
      short note, an execution status summary (success / failed / timed
      out), a "Preview Code" button (opens the shared code dialog with the
      exact executed source), and — if it succeeded — a thumbnail linking
      into the image-compare view for the resulting `GeneratedImage`.
      Input box + send button, disabled while the auto-retry loop is in
      flight, with a visible "attempt N/6…" indicator fed by SSE.
  - **If mode == "manual"**: code editor pre-filled with the function stub
    (or the persisted `current_code` if reopening an existing window), a
    "Run" button, and an execution-log panel (stdout/stderr/traceback, time
    taken, file_exists) shown directly below the editor after each run.
    No "Preview Code" button needed (see Section 8).
  - Both modes: a small metadata code block reiterating the attached image's
    metadata (collapsed by default, expandable), and — in "llm" mode only —
    a "View Example Code" button in the pane toolbar (Section 11.4).
  - Image comparison strip: original (or the source image for this turn) on
    the left, latest generated output on the right, with older outputs in a
    horizontally scrollable filmstrip below so the user can compare across
    turns/runs, not just the latest. Each thumbnail in the filmstrip also
    carries a small "View Code" icon-button opening the shared code dialog
    for the exact code that produced _that_ image — this works from either
    window mode and doesn't require scrolling back through chat history to
    find it.
- Download button (global or per-window): opens a dialog listing every
  `GeneratedImage` across window(s) with a thumbnail, description, and a
  checkbox; "Download Zip" posts selected `image_ids` to `/api/download`.
- A visitor arriving via a share link instead sees `shared_window.html`: the
  same chat/editor + image-compare components for that one window, with no
  tab bar and no reference to any other window.

### 11.3 Component files

Keep each of upload, metadata block, tab bar, chat pane, code editor pane,
the shared code dialog, image compare, download dialog, and share dialog as
separate Jinja2 partials + a matching JS module, per the folder structure in
Section 3 — no monolithic `index.html` with inline scripts.

### 11.4 Shared Code Dialog (`code_dialog.html` / `codeDialogViewer.js`)

One reusable modal component, parameterized by a title and a code string,
used in three places:

1. **"View Example Code"** — a button in the "llm" pane toolbar opening the
   dialog with the contents of `src/backend/examples/example_code.py`, plus
   a short explanation of the `main(input_path, output_path_dir)` contract.
   Keep `example_code.py` as a real, tested file on disk (not a string
   embedded in a template) so it can't silently drift into something that
   no longer runs.
2. **"Preview Code" on a code-result chat card** — opens the dialog with the
   exact code that was executed for that turn (`Message.code`).
3. **"View Code" on an output thumbnail** — opens the dialog with
   `GeneratedImage.code` (falling back to the sibling `.py` file on disk per
   Section 10.1 if the persisted value is unavailable for some reason).

In every case the dialog renders the code read-only, syntax-highlighted, and
copyable via a "Copy to clipboard" button.

---

## 12. API Endpoints

| Method | Path                                | Purpose                                                                                                                                                                                                   |
| ------ | ----------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------ |
| POST   | `/api/upload`                       | multipart file or `{url}` → `{image_id, metadata, preview_url}`                                                                                                                                           |
| POST   | `/api/windows`                      | create window `{image_id, mode: "llm"                                                                                                                                                                     | "manual"}`→`{window_id}` |
| GET    | `/api/windows`                      | list active windows as lightweight summaries — no hydration (Section 5.2)                                                                                                                                 |
| DELETE | `/api/windows/<window_id>`          | full delete if not shared; owner-side hide only if shared (Section 5.4)                                                                                                                                   |
| POST   | `/api/windows/<window_id>/share`    | generate/return `share_token` + shareable URL, sets `is_shared = true`                                                                                                                                    |
| DELETE | `/api/windows/<window_id>/share`    | unshare (clears token/flag); a subsequent DELETE then fully deletes                                                                                                                                       |
| GET    | `/shared/<token>`                   | resolves token → window, hydrates it, renders the focused single-window view                                                                                                                              |
| POST   | `/api/windows/<window_id>/message`  | `{prompt}` → sends a user turn; may resolve as a single chat reply or kick off the generate→execute→fix loop, per Section 7.3; `mode == "llm"` only                                                       |
| GET    | `/api/windows/<window_id>/history`  | full ordered turn list for this window, including `response_type`, `code`, and `execution_result` per turn — hydrates the window into the cache if not already loaded (Section 5.2); `mode == "llm"` only |
| GET    | `/api/windows/<window_id>/stream`   | SSE stream of loop progress (attempt N, response_type, message) for the current in-flight request; `mode == "llm"` only                                                                                   |
| POST   | `/api/windows/<window_id>/run-code` | `{code}` → single sandboxed run, returns `ExecutionResult`, persists `current_code` + any output; `mode == "manual"` only (see Section 8 for the optional stretch use from "llm" windows)                 |
| GET    | `/api/windows/<window_id>/outputs`  | list `GeneratedImage`s for this window, including each one's `code`                                                                                                                                       |
| GET    | `/api/examples/code`                | returns the canonical example code + contract explanation (backs the "View Example Code" dialog)                                                                                                          |
| POST   | `/api/download`                     | `{image_ids: [...]}` → streamed zip of full-res files                                                                                                                                                     |

For MVP, `POST /message` can respond synchronously once the whole loop
finishes (chat replies resolve in one round-trip regardless) if SSE proves
complex to stand up first — but design the endpoint so a streaming version
is additive, not a rewrite (e.g., loop logic lives in a generator function
either way). `POST /run-code` is always synchronous — it's a single
execution, not a loop. Code is embedded directly in `/history` and
`/outputs` payloads rather than requiring a separate fetch — see Section 15,
assumption 10, for the tradeoff.

---

## 13. Mandatory Project Documentation (`project_documentation/`)

This is not optional scaffolding — treat it as a deliverable equal in
importance to the code itself, and reference it continuously while building.

**Files to create at project start, seeded from this spec, then kept current:**

- **`PROJECT_SPEC.md`** — a living copy of this spec. When you deviate from
  it (see `DECISIONS.md` below), update this file so it reflects what's
  actually true, not just what was originally asked for.
- **`ARCHITECTURE.md`** — component responsibilities, data flow diagram
  (text/ASCII is fine), how the sandbox/LLM/session layers interact, the
  SQLite persistence + lazy-LRU-cache design (Section 5), and what the
  Executor and LLM-client interfaces look like and why they're abstracted
  the way they are.
- **`DESIGN.md`** — UI/UX decisions: layout rationale, the two window modes
  and how they share components, the chat-bubble-vs-code-result-card
  distinction, the shared code dialog's three call sites, the share/delete
  flow, Tailwind conventions/design tokens used, and any decisions about the
  code editor, dialogs, and image-compare view.
- **`CODING_STYLE.md`** — naming conventions, module boundaries, docstring
  conventions, error-handling conventions (e.g. how sandbox errors propagate
  vs. how validation errors propagate), and any linting/formatting tools in use.
- **`API_REFERENCE.md`** — the endpoint table from Section 12, kept accurate
  as endpoints are added/changed, including request/response shapes.
- **`PLAN.md`** — a checklist mirroring the Suggested Build Order (Section
  16), with status markers, so progress is visible at a glance and the next
  session (human or agent) knows exactly where to resume.
- **`DECISIONS.md`** — a running, dated log of notable decisions, especially
  any point where you deviated from this spec or from an "Explicit
  Assumption" in Section 15, and why. Log the `status`→`response_type`
  protocol change and the in-memory→SQLite persistence change here
  explicitly if an older draft was already implemented.

**Enforcement rules:**

1. Create all seven files before or alongside the first code commit — don't
   defer documentation to "later."
2. Before implementing any feature or making an architectural choice this
   spec doesn't explicitly pin down, consult `project_documentation/` first
   — it supersedes this spec once it diverges, since it reflects what was
   actually built.
3. Update the relevant file(s) in the **same work session** as any code
   change affecting architecture, API surface, data models, conventions, or
   plan/status — not as a batched cleanup later.
4. Treat a mismatch between `project_documentation/` and the actual codebase
   as a bug to fix, not a documentation nicety.

---

## 14. Config & Environment

`.env` (see `.env.example`):

```
ANTHROPIC_API_KEY=
LLM_MODEL=claude-sonnet-4-6
MAX_LOOP_ITERATIONS=6
EXECUTION_TIMEOUT_SECONDS=30
MAX_UPLOAD_MB=200
MAX_HYDRATED_WINDOWS=5
FLASK_ENV=development
```

`requirements.txt` should include at least: `flask`, `anthropic`, `pillow`,
`numpy`, `opencv-python-headless`, `scikit-image`, `tifffile`, `python-dotenv`,
`pydantic`, `gunicorn`. (`sqlite3` is stdlib — no extra dependency needed for
Section 5's store.)

---

## 15. Explicit Assumptions Made (please confirm/override before building)

1. **LLM provider**: Anthropic Claude via the official SDK, model name
   configurable — you'll need to supply/verify the exact current model
   string.
2. **Sandbox for MVP**: subprocess + AST import-check + resource limits, with
   Docker as a documented future upgrade, not built now. If you want
   container isolation from day one, say so — it changes the deployment
   story (needs Docker-in-Docker or a sibling container).
3. **Persistence backend**: SQLite (`volumes/sessions.db`), stdlib-only, no
   external DB server. Say if you'd rather use something else (e.g. if this
   ever needs to run across multiple processes/machines, SQLite's
   single-writer nature becomes a real constraint).
4. **Frontend interactivity**: recommended Alpine.js + CodeMirror 6
   additions alongside vanilla JS modules — confirm you're fine adding
   these dependencies.
5. **Turn-to-turn image source**: by default each new chat turn in an LLM
   window (and each run in a manual window) operates on the _original_
   uploaded image, not the previous turn/run's output, unless the user's
   prompt/code implies otherwise. If you want turns/runs to chain by default
   instead, flip this.
6. **Auth**: none — single trusted local user. This also means the
   `MAX_HYDRATED_WINDOWS` cache cap (Section 5.2) and the "interactive share
   link" behavior (Section 5.4) are both effectively global/unscoped rather
   than per-user. Flag if this needs to become multi-user — both of those
   would need real rework at that point.
7. **Loop cap**: 6 consecutive `"code"` replies before forced stop for LLM
   windows, configurable via env var. A `"chat"` reply resets the counter
   (see Section 7.3). Manual windows have no cap since each Run is a single,
   user-initiated execution.
8. **Manual-run naming**: output files from a manual window default to
   `manual_run_{n}` unless you'd rather prompt the user for a name per run —
   record whichever you pick in `DECISIONS.md`.
9. **Chat-vs-code decision**: left entirely to the LLM's judgment via the
   `response_type` field and the system-prompt guidance in Section 7.2 —
   there is no manual UI toggle forcing "just answer" vs. "make a change."
   If you'd rather give the user an explicit switch to force one mode or the
   other per message (removing the ambiguity from the model), that's a
   reasonable alternative — flag it and note the choice in `DECISIONS.md`.
10. **Code delivery**: code is embedded directly in the `/history` and
    `/outputs` JSON payloads rather than requiring a separate lazy-load
    endpoint, since generated snippets are expected to be small (tens of
    lines). If code sizes grow large enough to bloat these payloads, add a
    dedicated `GET /api/windows/<id>/code/<ref_id>` endpoint later — the
    shared code dialog component should be written so swapping to lazy
    loading doesn't require a rewrite (accept either an inline string or a
    fetch-on-open URL).
11. **Manual window autosave**: `current_code` is persisted only when "Run"
    is clicked, not on a debounce/keystroke timer. Unrun edits are lost if
    the window is closed/evicted before running — accepted tradeoff for
    simplicity; revisit if this proves annoying in practice.
12. **Eviction policy**: count-based LRU only (cap 5, Section 5.2), no
    idle-time sweep. Revisit only if the cap itself proves too small in
    practice, not by adding time-based eviction preemptively.
13. **Share links are fully interactive, not read-only** (Section 5.4) —
    flagged as the one sharing-related decision most worth a second look
    given the lack of auth.

---

## 16. Suggested Build Order (for the agent)

1. Scaffold folders (including `project_documentation/` with its seven files
   populated from this spec), config, `.env.example`, requirements/tailwind
   setup.
2. Build the SQLite store (`core/session/store.py`) and the lazy-LRU
   `manager.py` on top of it (Section 5) — window CRUD, summaries vs. full
   hydration, and the share/unshare/delete semantics — before wiring any
   image or LLM logic, since every other piece depends on windows actually
   persisting correctly.
3. Image metadata + upload endpoint + preview generation + the metadata
   code-block UI (testable in isolation with sample images of varying
   channel counts).
4. Sandbox executor + AST safety check, tested standalone with hand-written
   scripts before wiring in the LLM or the manual-run endpoint.
5. Self-programming window end-to-end (editor pane → `run-code` endpoint →
   execution log + output display, with save-on-run persistence) — this
   validates the sandbox/contract and the persistence write path without any
   LLM complexity in the loop.
6. "View Example Code" dialog, backed by a real `example_code.py` file that
   you've verified actually runs successfully through the sandbox — build
   the shared `code_dialog.html`/`codeDialogViewer.js` component here since
   it's reused later for "Preview Code" and "View Code" (step 8).
7. LLM client + prompt templates + schema validation for the
   `response_type`-driven contract (Section 7.3), tested with a fixed fake
   "conversation" (covering all three JSON shapes: plain chat, chat with
   illustrative snippet, and code) before wiring the real loop.
8. Wire the full turn-handling logic end-to-end on the backend for LLM
   windows — chat bubbles, code-result cards with "Preview Code", the
   auto-retry loop driven by `response_type`, and write-through persistence
   of every turn — verified via API calls (curl/Postman) before touching
   frontend.
9. Frontend: upload panel → single LLM window (chat pane with both bubble
   and card rendering, image compare with per-thumbnail "View Code") →
   window manager (multi-tab, both modes, restored from `/api/windows` on
   load) → share dialog + `/shared/<token>` route → download dialog, roughly
   in that order.
10. Update `PLAN.md` checkboxes and `DECISIONS.md` as each step lands.
