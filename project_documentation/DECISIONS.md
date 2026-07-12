# Decisions Log

> Dated log of notable decisions — especially deviations from spec or confirmed assumptions.

---

## 2026-07-09

### D-001: Alpine.js + CodeMirror 6 added
**Decision:** Adding Alpine.js (CDN) and CodeMirror 6 (CDN) as recommended in spec Section 10.1.
**Rationale:** Alpine.js eliminates manual DOM state management with no build step. CodeMirror 6 provides syntax highlighting in the manual code editor pane.
**Spec ref:** Section 14, assumption 4 — confirmed.

### D-002: Manual-run output naming
**Decision:** Default output name for manual window runs is `manual_run_{n}` where n is 1-indexed run count for the window.
**Rationale:** Spec Section 14, assumption 8 — chose simpler default over prompting user per run. User can distinguish runs by timestamp in filename.
**Spec ref:** Section 9.1, Section 14 item 8.

### D-003: In-memory session store
**Decision:** Using a plain Python dict (thread-safe via Flask dev server single-thread; lock added for production) as window/session registry.
**Rationale:** MVP scope per spec Section 14 assumption 3. Interface (manager.py) is designed so swapping to SQLite/Redis is isolated to that module.

### D-004: Turn image source = original
**Decision:** Each LLM turn and each manual run operates on the original uploaded image, not the previous turn's output.
**Rationale:** Spec Section 14, assumption 5 — default confirmed. User can chain explicitly in code if desired.

### D-005: SSE for LLM loop progress
**Decision:** Using Flask SSE (generator function + `text/event-stream`) for streaming loop progress to the browser.
**Rationale:** Spec Section 11 recommends SSE; POST /message triggers the loop and SSE stream delivers events. POST /message returns 202 immediately with a task handled in a background thread, SSE picks up via shared state.

### D-006: Windows deleted from memory but files retained on disk
**Decision:** `DELETE /api/windows/<id>` drops the in-memory Window object. Files in `volumes/outputs/{window_id}/` are retained on disk.
**Rationale:** Spec Section 11 notes this as an open question. Chose file retention to prevent accidental data loss — user can clean up manually. Logged as TODO for future cleanup endpoint.

---

## 2026-07-12

### D-007: Client-side markdown/math/mermaid rendering for LLM chat
**Decision:** LLM `message` fields are rendered client-side as markdown with math and diagrams, via a new shared module `static/js/markdownRenderer.js`. Stack (all CDN, no build step): marked (GFM) + DOMPurify (sanitization) + highlight.js + KaTeX + mermaid@10. Exposed as Alpine directives `x-markdown` and `x-highlight`.
**Rationale:** Inspired by the user's papers-app markdown engine (python-markdown + MathJax + highlight.js + mermaid, server-side). Chose client-side instead because chat messages arrive incrementally over SSE — server-side rendering would need an extra render endpoint per message. DOMPurify added because LLM output is untrusted HTML-wise, especially in interactive shared windows. KaTeX over MathJax for smaller payload and synchronous render into extracted math slots (avoids marked mangling `_` in TeX).
**Spec ref:** Not covered by spec — new capability. System prompt (Section 7.2 area, prompts.py) extended with a "Message Rendering" section so the LLM knows markdown/math/mermaid are supported.

### D-008: Dracula theme for all code display
**Decision:** Dracula theme everywhere code is shown: highlight.js `base16/dracula` CSS for the code dialog and chat snippets; `@uiw/codemirror-theme-dracula` (esm.sh, oneDark fallback) for the manual CodeMirror editor. Previously the dialog/snippets were unstyled plain text and the editor used oneDark.
**Rationale:** User request; one consistent theme across viewer and editor.
**Addendum (same day):** CodeMirror had never actually loaded — the editor was always the green textarea fallback. Two causes: (1) npm's `codemirror` package has a rogue `6.65.7` release containing CodeMirror 5 code, so the `@6` semver range resolved to a package with no `EditorView` export; (2) `?bundle` on esm.sh duplicates `@codemirror/state` per module, breaking cross-module extensions. Fixed by pinning `codemirror@6.0.2` and dropping `?bundle` (esm.sh dedupes shared deps via range URLs).

### D-009: Resizable right panel (chat / code editor)
**Decision:** The fixed 350px right panel in both window modes is now drag-resizable from its left edge (300–800px clamp), mirroring the left sidebar's drag handle. Shared `panelResizeState()` mixin in windowManager.js; width persisted in localStorage (`photon:panelWidth`), shared across modes/windows.
**Rationale:** User request; wider panel also benefits rendered markdown (tables, diagrams).

### D-010: Restart-survival bug fixed — chat turns now write-through; internal turn flag added
**Decision:** Fixed three persistence bugs that made LLM sessions vanish on server restart:
1. `chat_routes._run_llm_loop` appended every turn directly to the in-memory `window.llm_conversation`, bypassing `manager.add_message`/`store.append_message` — the `messages` table stayed empty. Now every turn (user prompt, assistant replies, loop-internal feedback) is written through per spec Section 5.3. Assistant `"code"` turns are persisted *after* execution so the stored row carries `was_executed`/`execution_result`; a crash mid-execution loses only that in-flight turn (accepted per Section 5.3).
2. `store.DB_PATH` was relative (`volumes/sessions.db`) — launching from a different cwd silently created a fresh empty DB. Now absolute via `config.VOLUMES_DIR`.
3. Execution results were attached to the message object only after it would have been persisted, so they could never reach the DB (moot under bug 1, fixed by the ordering above).
**Also:** new `Message.internal` flag (+ `messages.internal` column, additive migration). Loop-internal turns (execution feedback, JSON-contract-violation feedback) are persisted — they are part of the LLM context needed to faithfully resume a conversation — but `internal=true` turns are filtered out of the chat UI on history restore (chat.js). `/history` still returns the full turn list including them.
**Supersedes:** D-003 (in-memory session store) — the store has been SQLite-backed since the Section 5 persistence layer landed; D-003 is retained for history but no longer describes the code.

### D-011: Stoppable LLM loop, activity halo, 45:55 default panel ratio
**Decision:**
1. New `POST /api/windows/<id>/stop` endpoint + per-window `threading.Event`. The loop checks the flag before each LLM call and before each sandbox execution (a sandbox run already in flight completes/times out — no process kill in MVP). On stop: `stopped` SSE event + persisted "⏹ Stopped by user." chat turn. Batch execution across image slots also honors the flag.
2. Animated conic-gradient halo (`.llm-halo`) around the chat panel while the loop runs — activity indicator paired with the Stop button, not an always-on decoration.
3. Right-panel width switched from px (300–800) to percentage of the pane container (25–65%, default 45% → 45:55 panel:image ratio per user request). localStorage key changed `photon:panelWidth` → `photon:panelWidthPct` (old key simply ignored).
**Verified:** headless test — stubbed LLM burning attempts, stop requested mid-loop: halted at attempt 3/6 at the pre-execution boundary, `stopped` event emitted, stop turn persisted.

### D-012: Root cause of "session lost on reload" — INSERT OR REPLACE + FK cascade
**Decision:** `store.save_window` switched from `INSERT OR REPLACE` to a proper UPSERT (`ON CONFLICT(window_id) DO UPDATE`). Also all store connections now close after each call (`contextlib.closing`) — previously every call leaked an open connection.
**Root cause:** SQLite implements `REPLACE` as DELETE+INSERT. With `PRAGMA foreign_keys=ON` and `ON DELETE CASCADE` on `messages`/`outputs`, every `save_window` call (e.g. `set_status("idle")` at the end of every LLM loop) deleted the window row and cascade-wiped all just-persisted messages and outputs, then re-inserted only the window row. Hence tabs survived reload but conversations/outputs always came back empty — even though the write-through calls (D-010) were all executing correctly.
**Verified:** live server, two consecutive LLM loops (one chat, one code+execution): all turns incl. execution results + internal feedback + output row present in SQLite after loops completed and status writes fired.
