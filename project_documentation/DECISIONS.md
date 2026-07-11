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
