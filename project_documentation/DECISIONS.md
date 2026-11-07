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

### D-013: Sandbox BLAS threading capped (configurable)
**Decision:** Sandboxed child processes get `OMP_NUM_THREADS` / `OPENBLAS_NUM_THREADS` / `MKL_NUM_THREADS` / `VECLIB_MAXIMUM_THREADS` / `NUMEXPR_NUM_THREADS` set to `SANDBOX_BLAS_THREADS` (config/env, default 1). The sandbox `RLIMIT_CPU` is scaled by the same factor (CPU time is counted summed across threads). System prompt tells the LLM the sandbox is already thread-capped so it stops adding `os.environ` workarounds or avoiding numpy.
**Root cause:** On the 256-core host, unrestricted OpenBLAS spawned a thread per core inside sandbox children: `RLIMIT_CPU = 2×timeout` was consumed ~256× faster than wall clock (SIGXCPU) and per-thread stacks/buffers spiked memory. `SANDBOX_MEMORY_LIMIT_GB` was correctly wired but couldn't fix the CPU-limit failure mode. The LLM had empirically learned to set `*_NUM_THREADS=1` in generated code / avoid OpenBLAS — symptoms of the sandbox limits, now fixed at the executor level.
**Verified:** sandboxed run confirms child sees the env caps; numpy BLAS matmul + image output succeed.

### D-014: OpenRouter provider with runtime model dropdown
**Decision:** Third LLM provider `openrouter` (OpenAI-compatible, `https://openrouter.ai/api/v1`) alongside gemini/openai. Model is user-selectable at runtime from a top-bar dropdown; catalog comes from OpenRouter's `GET /models` (key-scoped, ~345 models), cached in memory 1h. New endpoints `GET /api/llm/info` + `POST /api/llm/model` (llm_routes.py); selection is validated against the catalog and held in memory only (resets to `OPENROUTER_MODEL` env default on restart — persisting it would need a settings table, deferred). For gemini/openai the top bar shows a static `provider · model` label instead.
**Config:** `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` (startup default) in .env; `LLM_PROVIDER=openrouter`.
**Verified:** headless — info returns 345 models, switch to a listed model 200 + client uses it, unknown model 400.

### D-015: Per-run/per-slot output directories — batch results no longer overwrite
**Decision:** Every sandbox execution now writes into a unique directory: `outputs/{window_id}/{run_id}/slot{N}/` (LLM loop: 8-hex uuid per user turn; manual: `run{n}`). Previously all slots and all runs shared `outputs/{window_id}/`.
**Root cause:** generated/user code typically saves a fixed filename (`out.png`, `otsu_threshold.png`); with a shared dir, each batch slot overwrote the previous slot's file — all GeneratedImage rows pointed at the last-written file, so every batch result displayed the same image. Same collision silently corrupted filmstrip history across successive turns/runs.
**Verified:** headless batch manual run over two distinct images with fixed-filename code: two distinct paths (`run1/slot0/out.png`, `run1/slot1/out.png`), distinct content hashes; second run leaves first run's files intact (4 distinct paths total). Previews/sidecars/zip unaffected (all derive from stored absolute paths; URL building uses `relative_to(OUTPUTS_DIR)` which handles nesting).

## 2026-07-12 (v2 build start)

### D-016: Phase 1 — auto dependency sync from pyproject.toml
**Decision:** New `core/sandbox/dep_sync.py::sync_from_pyproject()` reads `[project].dependencies` from `pyproject.toml` at config import time and derives both the sandbox AST allowlist (`config.SANDBOX_ALLOWED_IMPORTS`) and the LLM system prompt's library list (`config.SANDBOX_LIBRARY_WHITELIST`) — single source of truth, replacing two independently-hardcoded lists in `subprocess_executor.py` and `prompts.py`. Package→module name mapping for cases where they differ (`opencv-python-headless`→`cv2`, `pillow`→`PIL`, `scikit-image`→`skimage`, `pydensecrf2`→`pydensecrf`); infra/SDK packages (`flask`, `pydantic`, `requests`, `google-genai`, `openai`, `authlib`, ...) are excluded from the sandbox even though they're real dependencies. `uv add xyz` + restart is now sufficient to make a library sandbox-importable.
**Bug caught during rollout:** `scipy` was hardcoded into both old allowlists but was never a declared `pyproject.toml` dependency (only present transitively via `scikit-image`). A naive auto-sync would have silently dropped `scipy` from the sandbox on first restart, breaking any existing/generated code using it. Fixed by declaring `scipy>=1.18.0` directly in `pyproject.toml` (already locked at that version in `uv.lock`, so no environment change) — the right fix, since dep_sync's whole premise is "declared in pyproject.toml == available."
**Verified:** `pydensecrf` (already in pyproject.toml per spec's own verification target) and `scipy` both resolve into the allowlist and the LLM prompt string; a real sandboxed execution importing both `pydensecrf` and `scipy.ndimage` together succeeds; a disallowed import (`subprocess`) is still blocked; live server survived the change via auto-reload.
**Spec ref:** `photon_v2_plan.md` Phase 1.

### D-017: Phase 2 — auth: username/password always-on, Google OAuth optional
**Decision:** New `users` table (`username`, `email` both `NOT NULL UNIQUE`; `password_hash` nullable for Google-only accounts; `provider`/`provider_id` nullable for password-only accounts; `email_verified` column reserved unused for a future OTP flow — no email service exists yet). Password hashing via `werkzeug.security` (already a transitive Flask dependency — no new package). `core/auth/oauth.py::init_oauth()` is a no-op unless both `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` are set (`config.GOOGLE_OAUTH_ENABLED`); the two Google routes in `auth_routes.py` `abort(404)` when disabled instead of erroring. `login.html` always shows username/email+password login and signup forms; the Google button is a server-side Jinja conditional on `google_oauth_enabled`, avoiding an extra `/auth/config` round trip (that endpoint still exists for future SPA use). `/` is now gated with `@login_required` — **this is a live behavior change**: existing browser sessions with no auth cookie get redirected to `/auth/login` on next visit; one-time signup (`POST /auth/signup`) restores access immediately, no data lost (window/session data model isn't user-scoped yet — that lands in Phase 3).
**Not yet done (by design, deferred to Phase 3):** `/api/windows/*`, `/api/llm/*`, and other API blueprints are still unauthenticated/unscoped — Phase 2's own plan text says auth infra "must come before session scoping," not that every route gets gated immediately. Gating them now with no user_id column on `windows` would just 401 every existing fetch call with nothing to attribute the request to.
**Verified:** headless — unauthenticated `/` → 302 to `/auth/login`; login page hides Google button and `/auth/login/google` 404s when unconfigured, shows button and 302-redirects to Google when `GOOGLE_CLIENT_ID`/`SECRET` are set; signup → 201 + session set → `/auth/me` 200 → `/` 200; duplicate username/email → 409; weak password → 400; wrong password / unknown identifier → 401; Google-only-account password login correctly blocked (path logic reviewed, not exercised — needs a real Google account to hit the callback). Also verified live against the running server: real signup/login round trip via curl with a cookie jar, session persisted, `/` reachable post-login; test users cleaned from `sessions.db` afterward.
**Spec ref:** `photon_v2_plan.md` Phase 2 (as amended for optional-OAuth, this session).

### D-018: Phase 3 — unified Session model replaces mode-locked Window (backend only)
**Decision:** `Window` (`window_id`, `mode: "llm"|"manual"`, single `image_id`) replaced by `Session` (`session_id`, `user_id`, `title`, `created_at`/`updated_at`, `image_ids: list`) in `models/dto.py`. **No `mode` field at all** — a session now holds both `llm_conversation` and `current_code` simultaneously; LLM chat and manual run-code are just two write paths into the same record, freely interleaved (verified: a manual `run-code` call followed by an LLM `message` call on the *same* `session_id` both succeed and both land in that session's history/outputs). `GeneratedImage.window_id` → `session_id`. SQLite `windows` table → `sessions` table (`user_id` FK, `ON DELETE CASCADE`); `messages`/`outputs` FK renamed `window_id`→`session_id`. `window_routes.py` deleted, replaced by `session_routes.py` — every route requires `@login_required` and resolves ownership via `manager.get_session_for_user(id, user_id)`, so one user can never read, run code in, or delete another user's session (404, not 403, to avoid confirming existence). `chat_routes.py`/`manual_routes.py` renamed throughout and lost their `window.mode != "llm"` / `"manual"` gate checks — any session accepts both endpoint types now. `shared_routes.py` and `download_routes.py` got the minimum rename needed to not crash against the new manager API; `download_routes.py` additionally gained `@login_required` + scoping to the current user's own sessions (a global unscoped download became a real cross-user leak the moment multi-user auth existed — not full Phase 5 session-scoping, just the auth-consistency floor). `shared_routes.py`'s actual sharing semantics are untouched (still pre-Phase-4 "visit unauthenticated" behavior) since Phase 4 redesigns that into clone-on-open. New `GET/POST /session/<id>` route added in `app.py` (inert until Phase 6 wires the sidebar to read `active_session_id`).
**Breaking, by design (plan-approved):** wiped `sessions.db` and all files under `volumes/uploads/`, `volumes/outputs/`, `volumes/tmp_exec/` before starting (per explicit user instruction — old conversations/images weren't needed once the session model itself was being replaced). Re-wiped a second time mid-phase when the physical DB file still had columns from the old `windows` schema (SQLite's `CREATE TABLE IF NOT EXISTS` doesn't retroactively alter existing tables) — caught by a live `sqlite3.OperationalError: table outputs has no column named session_id` during verification, not a design ambiguity.
**Explicitly deferred, not attempted this phase:** the frontend (`chat.js`, `codeEditor.js`, `windowManager.js`, `index.html` and its partials) still speaks the old `/api/windows/*` + `mode` contract and is now broken in the browser — Phase 6 is a separate, later rewrite (sidebar, mode toggle removed in favor of a unified pane, `/session/<id>` routing). Per user instruction ("do it one by one"), Phase 3 verification was done entirely via the Flask test client and live `curl`, not the browser.
**Verified:** headless — two users, user B gets 404 on every route touching user A's session (get/history/run-code); a single session accepts a manual `run-code` call *and* an LLM `message` call, both appear in that session's outputs/history; title rename, share/unshare, delete-blocked-while-shared then delete-succeeds-after-unshare, add/remove image all pass; unauthenticated `/api/sessions` → 401. Also verified live against the running dev server (which had been stopped independently since the prior session — restarted in the existing `Photon` screen) with a real signup + session-create round trip against the fresh schema.
**Spec ref:** `photon_v2_plan.md` Phase 3.

### D-019: Phase 4 — share-as-clone, replacing interactive-share
**Decision:** Sharing no longer grants access to the original session. `POST /api/sessions/<id>/share {reusable}` creates a row in a new `share_links` table (`token` PK = `secrets.token_urlsafe(32)`, `session_id`, `created_by`, `reusable`, `used_count`, `created_at`) — a session can have several independent links over time, unlike the old single `share_token`/`is_shared` pair on `sessions` (both columns removed from the `Session` dataclass and the `sessions` table entirely). `GET /shared/<token>` (`@login_required` — the clone has to belong to *someone*) looks the token up, 404s if it was never issued, 410s if one-time and already used, then calls `SessionManager.clone_session(source_id, target_user_id)`: duplicates every attached upload (new `image_id`, copied file + preview, new `images` row) and every output (new `image_id`, copied file + preview + `.py` sidecar into `outputs/{new_id}/cloned/`), copies all messages, creates a brand-new owned `Session`, and redirects to `/session/<new_id>`. `used_count` increments only after a successful clone. `DELETE /api/sessions/<id>/share` now revokes *all* active links for that session (a session-level action, since there can be multiple links, matching the original single-endpoint shape from Phase 3's route list). Deleting a session no longer requires unsharing first — `SessionManager.delete_session` dropped the `is_shared` guard entirely, since a clone is a fully independent session by construction; deleting the original never touches an already-created clone (its `share_links` rows just cascade-delete). `shared_window.html` deleted (dead — nothing renders it anymore, `/shared/<token>` is a redirect-only route).
**Layering:** `clone_session` lives in `core/session/manager.py` and only touches `store.py` + the filesystem (never imports from `api/`, per CODING_STYLE's `api/` → `core/` one-way rule); the route handler in `api/shared_routes.py` is what re-populates the in-memory image registry (`upload_routes.get_image_registry()`) for the newly duplicated image ids after the clone, since that registry is `api`-layer runtime state, not something `core/` should reach into.
**Verified:** headless, three separate users — anonymous visit to a share link redirects to login (no clone happens); logged-in recipient opens a one-time link → clone created with a genuinely new `image_id`, a duplicated upload file (different path, identical byte content, confirmed via hash), a duplicated output file, and the same conversation history (2 turns) as the source; the clone is a fully working session — recipient runs code in it independently; second open of the same one-time link → 410; a random guessed token → 404; a reusable link opened by two different users produces two independent clones; owner revokes → link 404s for everyone; owner deletes the *original* session → succeeds immediately (no shared-block), and the earlier clone remains fully accessible to its owner afterward. One bug caught in my own test harness along the way, not the implementation: the first verification pass registered a test image only in the in-memory registry and skipped `store.save_image()` (which real `/api/upload` always calls) — `clone_session` correctly found nothing to duplicate and the clone had no image, exactly as it should for an image that was never actually persisted; fixed the test, reran, confirmed clean.
**Spec ref:** `photon_v2_plan.md` Phase 4.

### D-020: Phase 5 — session-scoped download + smart deletion (full wipe, not partial)
**Decision:** `POST /api/download` (global, aggregated across all the user's sessions) replaced by `POST /api/sessions/<id>/download` — owner-only, scoped to exactly one session's outputs; empty/omitted `image_ids` downloads all of that session's outputs instead of erroring.
`DELETE /api/sessions/<id>` deletes **everything the session owns**, not just generated outputs: the whole `outputs/{session_id}/` directory AND every source image it references (file + preview + DB row + in-memory registry entry) — confirmed explicitly by the user, overriding the original plan pseudocode's "never delete uploads." The one guard kept: an image is skipped (retained) if any *other* session — this user's or anyone else's — still lists it via `image_ids` (images aren't exclusive to a session, see `POST /sessions/<id>/images`); deleting session A must never break session B's ability to run code against an image they both reference. New `store.image_referenced_elsewhere(image_id, exclude_session_id)` does the check (full scan of `sessions.image_ids_json` — fine at this scale, no dedicated join table needed yet). `SessionManager.delete_session` return type changed `bool` → `Optional[list[str]]` (the image_ids actually deleted, or `None` if not found/not owned) so the route can prune exactly those from the in-memory registry; caught a real bug in my own first draft of the route — checking `if not removed` on a list return treats an empty list (session deleted, zero images removed) as "not found," which would incorrectly 404 a successful delete. Fixed to `is None`.
**Explicitly out of scope, flagged not built:** the original plan goal text ("retains ... source code for regeneration") described something closer to *idle-session output cleanup* — periodically purging just the regenerable outputs of sessions that aren't actively being used, while keeping the source image and code so results can be regenerated later. That's a distinct feature (needs an idle/staleness definition, a trigger — background job vs. explicit user action — and UI) that was never part of the approved phase plan; user confirmed explicit `DELETE` should be a full wipe instead. Worth scoping separately if wanted later.
**Verified:** headless — two sessions sharing one image (`imgB`) plus one exclusive image (`imgA`): deleting the first session removes its output directory, deletes `imgA` (file + DB row + registry entry) since nothing else references it, but leaves `imgB` fully intact (file, DB row, registry) because the second session still needs it — confirmed the second session keeps working (runs code against the shared image successfully) after the first is gone; deleting the second session afterward then correctly removes `imgB` too, since no session references it anymore. Session-scoped download: filtered and unfiltered zip downloads work, a session with no outputs 404s, a non-owner gets 404. Live server restarted clean on the running schema (unchanged this phase — no new columns).
**Spec ref:** `photon_v2_plan.md` Phase 5 (deletion semantics amended per user clarification, this session).

### D-021: Phase 6 — frontend overhaul (sidebar, unified session pane, session-scoped dialogs)
**Decision:** Full frontend rewrite matching the backend's session model (Phases 3–5):
- **Sidebar** (`session_sidebar.html` + `sessionManager.js`, replaces `window_tab.html`/`windowManager.js`): collapsible, ChatGPT-style vertical list (title, image count, running indicator) instead of a horizontal tab bar. Sorted by `updated_at` desc (server already returns it that way). Click activates + `history.pushState`s to `/session/<id>`; browser back/forward handled via `popstate`. `GET /session/<id>` (added in Phase 3, unused until now) injects `window.ACTIVE_SESSION_ID` so a direct link or reload lands on the right session.
- **Unified session pane** (`session_pane.html` + `sessionPane.js`, replaces `chat_pane.html`+`chat.js` and `code_editor_pane.html`+`codeEditor.js`): one Alpine component owns both the LLM chat state and the manual editor state for a session, since a session has no locked mode (D-018). A `viewMode` toggle (`💬 Chat` / `</> Code`, client-side only, not persisted) switches which half renders — both halves stay mounted via `x-show` (not `x-if`) so CodeMirror initializes exactly once per session-activation and chat scroll position survives toggling. One `history` fetch on init populates both. Session header (editable title, Share/Download/Delete) lives here since it needs `sess.session_id`.
- **New-session flow** (`new_session_dialog.html` + `newSessionDialog.js`, replaces `upload_panel.html`+`uploader.js`): upload moved from a persistent left-sidebar panel into a modal triggered by "New Session" — uploads image(s), then dispatches `fe:create-session` (same decoupled-event pattern the old `fe:open-window` used) for `sessionManager.js` to create the session and navigate in. "Add Image" to an *existing* session is a small inline affordance in `sessionPane.js` (`addImage()`, hidden file input) rather than a second dialog — didn't justify a new file for one button.
- **Session-scoped dialogs**: `downloadDialog.js`/`download_dialog.html` now fetch outputs for exactly the session that opened them (`fe:download-opened` event carries `session_id`, hits `GET/POST /api/sessions/<id>/...`) instead of reading a global `allOutputs` array aggregated at app boot — that global aggregation is gone entirely, no longer needed. `shareDialog.js`/`share_dialog.html` gained the one-time/reusable picker (D-019) and updated field names (`token` not `share_token`); copy reworded to make clear a share link creates an independent copy, not access to the original.
- **Top bar**: global Download button removed (now per-session, in the session header). Added a user menu (avatar initial + display name, dropdown with Logout — `GET /auth/me` on load, `POST /auth/logout` then redirect to `/auth/login`). Model selector and GitHub link unchanged.
- `imageViewer.js` and `image_compare.html`: renamed `window.images` → `sess.images` (data source, not the DOM/browser `window` object — no behavior change).
Deleted (superseded, all old-model references swept and confirmed zero remaining): `window_tab.html`, `upload_panel.html`, `chat_pane.html`, `code_editor_pane.html`, `windowManager.js`, `chat.js`, `codeEditor.js`, `uploader.js`.
**Verified:** JS syntax-checked (`node --check`) on every touched file; `GET /` and `GET /session/<id>` render through a real Flask test client with no Jinja errors, confirmed present in the output (not just absence of a 500): sidebar markup, mode toggle, resizable-panel attributes, user menu, all four dialog stores; a full backend flow exercising every endpoint the new frontend calls (create session → manual run-code → LLM chat message on the *same* session → history → render `/session/<id>` → share → download → delete) passes end-to-end.
**Not verified — explicitly flagged:** no browser automation tool was available this session (checked via the `claude-in-chrome` skill, not installed/connected). Nothing here has been visually confirmed to render or actually be clickable in a real browser — CDN script loads (CodeMirror, marked, KaTeX, mermaid, highlight.js), Alpine reactivity, and interaction flows are all unverified beyond static/backend checks. **A manual browser pass is needed before trusting this phase is actually done**, especially: CodeMirror initializing in the code sub-view, the mode toggle, sidebar session switching, and both dialogs opening from the session header.
**Spec ref:** `photon_v2_plan.md` Phase 6.

## 2026-08-02

### D-022: Frontend sizing/responsiveness cleanup
**Decision:** Standardized widths/heights and added responsive layouts across the v2 frontend (user request — components had no breakpoints at all and inconsistent fixed sizes):
1. **Session pane** — `flex-col lg:flex-row`: below `lg` the images area and chat/code panel stack (panel = full-width, 55%-height block; resize handle `hidden lg:block`); at `lg+` the draggable split applies. Panel width moved from an inline `width: X%` style to a CSS variable (`--panel-w` + `lg:w-[var(--panel-w)]`) so it can't fight the stacked layout.
2. **Image compare** — original/output pair stacks below `sm` (each `h-44`); row height `sm:h-56 lg:h-64` instead of fixed `h-64`; `min-w-0` guards added.
3. **Dialogs** — one shared shell: `max-h-[85vh]` everywhere (was 70vh/none/85vh mixed), scrollable bodies, consistent `px-6 py-4` header/footer. Fixed a broken `h-full`-inside-auto-height-scroll-parent chain in `code_dialog.html`.
4. **Top bar** — `px-4 sm:px-6`; model selector hidden below `sm`, width-clamped `w-40 md:max-w-[260px]`; session-header toolbar button labels `hidden sm:inline` (icons remain).
5. **Sidebar** — width clamped `max-w-[85vw]`; starts collapsed when viewport < 1024px.
6. Root container gets a `100dvh` upgrade via `supports-[height:100dvh]` for mobile browser chrome.
**Build note:** `output.css` is generated (`npm run build:css`, tailwindcss in devDependencies) — rebuilt as part of this change and verified every new arbitrary-value class landed in the built CSS; forgetting the rebuild silently no-ops any new class.
**Verified:** Tailwind rebuild + fixed-string checks of all new classes in `output.css`; Jinja render of `/` through the test client confirms every new class/attribute in the served HTML; live server restarted clean. Same caveat as D-021: no browser tool available — actual visual behavior at each breakpoint not observed, needs a manual pass (resize the window across 640/1024px, open each dialog, check the stacked mobile layout).
**Addendum (same day):** carousel backdrop was fully transparent — `bg-black/92` is not a valid Tailwind opacity step (scale has 90/95, no 92), so the class was silently never generated. Same failure mode as the build note above: an invalid/unbuilt class no-ops with no error. Fixed to `bg-black/90` + `backdrop-blur-sm`, rebuilt output.css, verified both classes in the built CSS.

### D-023: Type-to-confirm session deletion; sidebar delete removed
**Decision:** The sidebar's hover × delete button is gone — deletion lives only in the session window's toolbar. The native `confirm()` prompt replaced by a proper dialog (`delete_session_dialog.html`, `$store.deleteDialog`): shows the session id as a copy-pastable code block with a Copy button, and requires typing/pasting the exact id into an input before the Delete button enables. `sessionManager.deleteSession()` now just opens the dialog; the actual API call + list cleanup moved to `performDelete()`, which the dialog reaches via Alpine scope inheritance (the partial is included inside the `sessionManager()` x-data div, same pattern as `new_session_dialog.html`).
**Verified:** CSS rebuilt, JS syntax-checked, rendered page contains the dialog/store/confirm flow, `deleteSession` referenced only from the session pane toolbar, live server serving.

### D-024: Clipboard fallback for non-HTTPS hosting
**Decision:** All copy buttons (code dialog, share link, metadata block, delete-dialog session id) now go through a shared `copyTextToClipboard()` helper in `markdownRenderer.js` (the globally-loaded frontend util module). It uses `navigator.clipboard.writeText` when available, falling back to the legacy hidden-textarea + `document.execCommand('copy')` path otherwise, and returns a success boolean so the "Copied!" indicator only shows on actual success.
**Root cause:** `navigator.clipboard` exists only in secure contexts (HTTPS or localhost). The app is hosted on a LAN machine and reached via plain `http://<ip>:8889`, where the API is `undefined` — every copy button silently failed. Not a Linux issue per se; the same failure occurs from any non-localhost plain-HTTP origin.
**Verified:** zero direct `navigator.clipboard` references remain outside the helper; JS syntax-checked; rendered page confirms the helper script loads (head) before any call site. Actual click behavior needs the usual manual browser pass (no browser tool).

### D-025: "Preview Output" button on chat code cards
**Decision:** Code-result cards in the chat view now show a green **Preview Output** button beside **Preview Code** whenever the turn produced output image(s); it opens the existing fullscreen carousel with that turn's outputs (all batch slots as slides, each carrying the turn's code for the carousel's own View Code button).
**Backend fix required:** the LLM loop had always persisted `GeneratedImage.source_turn_index=None`, so restored messages couldn't be linked to their outputs. The loop's winning-code extraction now also captures the winning assistant message's `turn_index` and stamps it on every output of that run (including batch slots). Frontend keeps `turn_index` on mapped chat messages, links outputs on history restore via `source_turn_index`, and attaches live `output_saved` SSE events to the last assistant code message.
**Verified:** headless with a stubbed LLM — after a successful code turn, `/outputs` rows carry `source_turn_index` matching the assistant code turn and a `preview_url`; py/js compile checks; CSS rebuilt (emerald button classes present); live server serving.
**Addendum to D-021 (2026-08-02):** switching sessions from the sidebar didn't swap the workspace until a manual reload. Cause: the pane is rendered under `x-if="activeSessionData"`, which only reacts to truthy↔falsy transitions, and Alpine has no `:key` mechanism outside `x-for` (the `:key` I'd put on the pane div was a no-op) — so replacing one session object with another never destroyed/recreated `sessionPane()`. Fixed by nulling `activeSessionData` at the start of `hydrateActiveSession()`, forcing a full unmount before the new session's data remounts a fresh pane; the misleading `:key` removed.

## 2026-08-02 (v3 pipelines)

### D-026: v3 P1 — pipeline op registry, typed DAG runner, presets, API
**Decision:** New `core/pipeline/` package implementing the v3 pipeline system (see `photon_v3_plan.md`): a curated registry of 38 classical image-processing ops across 7 categories (color, threshold, morphology, filters, features, masking, geometry), all **thin wrappers over skimage/scipy** rather than reimplementations — the one real implementation is MIND (Heinrich 2012 self-similarity descriptor), which no library ships. `registry.catalog()` feeds the editor palette and `registry.catalog_for_prompt()` feeds the LLM system prompt from the same source so they cannot drift. Typed ports (`IMAGE_GRAY/RGB/ANY`, `MASK`, `FLOAT_MAP`, `FEATURE_MAP`, `NUMBER`, `ANY`) with an explicit coercion table; server (`runner.validate`) is source of truth and ships the table to the client for instant editor feedback. Six preset pipelines seeded idempotently into `pipelines` (user_id NULL = global read-only, clone-to-edit). New tables `pipelines` + `custom_nodes` (additive — no DB wipe needed). Execution goes through a resource-limited subprocess (`executor.py`) reusing the sandbox's BLAS caps/rlimits/timeout (D-013) but deliberately **without** the import whitelist, since registry ops legitimately import skimage/scipy; custom-node safety instead comes from the AST check applied at *save* time via the existing `_check_ast_safety`. Per-node preview JPEGs are written for ComfyUI-style inspection; finals become normal `GeneratedImage` rows so filmstrip/download/Preview Output work unchanged.
**Two design bugs caught during verification, both by running the presets rather than eyeballing them:**
1. The implicit `input` node was typed `IMAGE_ANY`, which under the initial one-directional coercion table couldn't connect to any concrete `IMAGE_RGB`/`IMAGE_GRAY` port — i.e. *every* valid pipeline failed validation. Fixed by treating the session image as a wildcard source whose concrete type is re-checked at run time.
2. Pass-through ops (blur, gamma, crop…) declare `IMAGE_ANY` outputs, so blur→sobel was rejected despite being obviously valid. Fixed by making `IMAGE_ANY` a wildcard on both sides, with `infer_type()` coercion at run time. `IMAGE_RGB → IMAGE_GRAY` remains deliberately incompatible: dropping channels must be an explicit `to_grayscale` node.
Also replaced two skimage calls deprecated in 0.26 (`binary_*` morphology, `remove_small_objects(min_size=)` whose replacement has inverted off-by-one semantics — reimplemented via labelling to keep meaning stable across versions).
**Verified:** all 6 presets validate and execute against a synthetic H&E image with warnings-as-errors; full API pass — catalog (38 ops), preset listing, presets reject PATCH/DELETE with 404, clone-then-edit works, `/pipelines/validate` accepts valid and rejects invalid, custom-node save AST-checks (a `import socket` node is rejected 400), `run-pipeline` produces a correct stain mask (fg fraction 0.151) plus 5 per-node previews on disk and a `GeneratedImage` row with preview, genuinely-invalid graphs rejected synchronously with 400 (type mismatch and unknown op), cross-user isolation (another user 404s on my pipeline and sees only presets). Live server restarted clean with 6 presets seeded.
**Note on an earlier misleading test:** my first rejection test used `input → otsu_threshold`, which is *valid* by design (wildcard source), so its 202 was correct behavior, not a bug — retested with a genuinely invalid graph.

### D-027: v3 P2 — LLM composes pipelines instead of regenerating code
**Decision:** New `response_type: "pipeline"` alongside chat/code (`LLMTurnResult.pipeline`, additive `messages.pipeline_json` column with an ALTER-based migration — no DB wipe). The system prompt now carries the full op catalog generated by `registry.catalog_for_prompt()` **plus the requesting user's saved custom nodes**, and instructs the model to prefer pipelines whenever a request decomposes into catalog ops, reserving `code` for genuinely novel transforms. `build_system_prompt` gained a `custom_nodes` argument; `chat_routes` passes the caller's library so the LLM references existing nodes by `custom_node_id` rather than rewriting them — that's the whole "retrieve if it exists, generate only when missing" mechanism, and it needs no extra machinery because the catalog *is* the retrieval index.
**Loop behavior:** a pipeline turn is statically validated (`runner.validate`) BEFORE running. Invalid → the exact issues are fed back as an internal turn via `build_pipeline_feedback_message` and the model retries within the same `MAX_LOOP_ITERATIONS` budget (mirrors the code path's execution-feedback retry). Valid → runs through `run_pipeline_for_session` (shared with `POST /run-pipeline`), streams `node_start`/`node_done`/`node_previews` over the session SSE queue, and persists outputs with `source_turn_index` so the D-025 Preview Output linkage works for pipeline cards too. Runtime failures also feed back and retry.
**Inline custom nodes:** a pipeline may carry `{"custom": {name, code, ports, params}}` for the one step the catalog lacks. `_persist_inline_custom_nodes()` AST-checks each with the run-code sandbox checker, saves it (upsert by `(user_id, name)`), and rewrites the node to a `custom_node_id` reference before validation — so the stored graph is portable and the node is reusable. A rejected node becomes a feedback turn, never a crash.
**Frontend:** new pipeline chat card (`session_pane.html` + `sessionPane.js`) — node chain as chips that light amber/green/red from live SSE node events, per-node preview thumbnails (clickable into the carousel), validation-issue and runtime-error strips, View Graph (JSON via the shared code dialog), Preview Output, and "Save to my pipelines".
**Verified** (stubbed LLM, five cases): (1) valid pipeline → 1 LLM call, graph persisted, correct mask output linked by `source_turn_index`; (2) invalid pipeline → exactly 2 LLM calls with an internal "Validation Failed" feedback turn between them, corrected pipeline then produces output; (3) inline custom node → saved to the library, rewritten to `custom_node_id` in the stored graph, pipeline runs; (4) prompt contains the op catalog, the saved custom node, and the prefer-pipeline instruction; (5) a custom node containing `import socket` is rejected by the AST check, never saved, fed back as an internal turn, and the loop continues normally to a chat reply. One bug found during verification was in the test harness itself (stub lambdas used positional args while `send_turn` is called with keywords), not in product code.

### D-028: v3 P3 — Drawflow visual pipeline editor
**Decision:** Two new pages: `/pipelines` (library — your pipelines + read-only presets with Clone) and `/pipelines/<id>` (editor). Editor is **Drawflow 0.0.60** via jsDelivr, pinned exactly and verified UMD (`window.Drawflow`) with both JS and CSS returning 200 before any code was written — the CodeMirror lesson from D-008. Chosen over LiteGraph.js (archived upstream, absorbed into ComfyUI's monorepo) and Rete.js v2 (bundler/plugin-oriented, no usable CDN build); Drawflow's DOM-based nodes also mean node markup is plain HTML we can style with the existing Tailwind palette.
**Typed ports on an untyped library:** Drawflow has no port types, so type checking lives in `pipelineEditor.js` — it mirrors the server's coercion table (shipped inside `GET /api/ops`) and rejects an incompatible connection on the `connectionCreated` event via `removeSingleConnection`, with a toast explaining why (including the "insert a to_grayscale node" hint for RGB→GRAY). The server remains source of truth: Save and Run both re-validate through `runner.validate`.
**Graph mapping:** Drawflow's numeric node ids and positional `input_N`/`output_N` classes are mapped to our stable string ids and *named* ports via `node.data.our_id` and port index. The implicit `input` source node is materialised as a real, draggable Drawflow node (op `__input__`) and stripped on export/re-added on import, so users can wire from the source image without it existing server-side. Node positions round-trip in `node.pos` (the server ignores the field), so layouts survive save/reload.
**UI:** filterable palette grouped by category (including the user's custom nodes), inspector panel rendering param widgets from each op's declared schema (number/checkbox/select with min/max/choices), session picker + Run with live per-node preview thumbnails over the existing SSE stream, Validate, zoom, delete-node, and Clone-to-edit for presets. Pipeline chat cards gained "Open in editor" (saves first if needed, then navigates). "Pipelines" link added to the main top bar. Drawflow's light default theme overridden for the dark palette in markdown.css.
**Verified:** CDN assets confirmed 200 + UMD before building; both pages render with all expected markup and are auth-gated (302 for anonymous); **graph round-trip is byte-identical** on the branching `nuclei-segmentation` preset (6 nodes, 7 edges including two fan-outs from the source image) through a faithful stub of Drawflow's internal data model; client-side `_canConnect` agrees with the server on all four representative pairs (MASK→GRAY coerces, RGB→GRAY rejected, exact match ok, FLOAT_MAP→MASK rejected); and a graph exported from the editor *after an inspector param edit* validates and executes server-side, producing 6 node previews and a final output, with the edited param correctly typed as int rather than a form string.
**Not verified — same caveat as D-021/D-022:** no browser tool available, so actual dragging, connection rendering, and Drawflow's real event payloads have not been observed live. The export/import logic is tested against a stub that models Drawflow's documented data shape; if the real library differs in a detail, that's where it would show. Needs a manual pass: open a preset, clone it, drag a node, wire a connection, try an illegal one (RGB→GRAY), edit a param, Save, Run.

### D-029: PyTorch pinned to CUDA 12.1 wheels (P4 prerequisite)
**Decision:** `torch==2.5.1` + `torchvision==0.20.1` added via uv, sourced from `https://download.pytorch.org/whl/cu121` — the user's container only works with the cu121-compiled build, not the default PyPI wheels. Configured as an `explicit = true` uv index with `[tool.uv.sources]` mapping only torch/torchvision to it, so every other dependency still resolves from PyPI. Installed as `2.5.1+cu121` / `0.20.1+cu121`; verified `torch.cuda.is_available()` on the A100-80GB (driver 535, CUDA 12.1). **torchaudio deliberately omitted** — it was in the user's pasted command (the standard install triple) but is irrelevant to an image-processing app and adds weight; say so if it's actually wanted.
**Side effect, by design:** dep_sync (D-016) reads `pyproject.toml`, so `torch` and `torchvision` are now automatically in `SANDBOX_ALLOWED_IMPORTS` and in the LLM's library whitelist — LLM-generated code and custom nodes can use the GPU. That's desirable for P4, but it's an automatic consequence worth knowing about rather than an explicit choice.
**Empirically checked, not assumed:** I expected `RLIMIT_AS` to break CUDA (it historically does — CUDA reserves large virtual address space). It does not at the configured limit: GPU work inside the real sandbox executor succeeds at `SANDBOX_MEMORY_LIMIT_GB` of 20 and 8, and fails at 4. CUDA 12's lazy module loading keeps the reservation small enough. **Do not drop `SANDBOX_MEMORY_LIMIT_GB` below ~8 or GPU code in the sandbox will fail** with a bare non-zero exit. Also re-verified that the install disturbed nothing: numpy 2.5.1 / skimage 0.26 / scipy 1.18 / cv2 5.0 unchanged, and all 6 pipeline presets still validate and run.

### D-030: v3 P4 — GPU model lifecycle enforced as a software constraint
**Requirement (user):** anything consuming VRAM loads only when actually requested and unloads 5 minutes after last use — and this must bind *future* model additions structurally, not by convention.
**Why it needed architecture, not a helper function:** pipeline ops execute in a throwaway subprocess per run (`pipeline/executor.py`), so a model loaded there dies in ~2s and a 5-minute idle window is literally unexpressible; and production runs `gunicorn --workers 2`, so in-process models would mean two copies of everything. Hence a **single long-lived model sidecar** (`core/models/sidecar_server.py`), spawned lazily on the first model request (O_EXCL lock so concurrent workers can't race two of them), talking over a Unix socket with arrays handed across as `.npy` files rather than serialized through the socket. The sidecar is deliberately NOT under the sandbox's tight `RLIMIT_AS` (D-029: CUDA fails below ~8GB address space) — it only ever runs our own trusted model code, never user/LLM code, which keeps the sandbox's trust boundary intact.
**The lifecycle owner:** `ModelManager` — lazy load on first `acquire()`, refcount so a model in use is never unloaded mid-inference, a reaper thread unloading anything idle past `MODEL_IDLE_TIMEOUT_SECONDS` (default 300), and LRU eviction of unreferenced models to stay under `MODEL_VRAM_BUDGET_MB` (default: half the device). Op authors never call load/unload; `@register_model_op` injects an already-loaded handle.
**The actual constraint — `core/models/guard.py`:** an AST scan of every module under `core/pipeline/ops/` that runs inside `pipeline.registry._ensure_loaded()` (so it can't be skipped by importing ops another way). It rejects `torch.hub.load`, `torch.load`, `.from_pretrained`, `.load_state_dict`, `rembg.new_session`, `onnxruntime.InferenceSession`, `timm.create_model`, `.cuda()` and `.to("cuda")` outside the sanctioned `core/models/` package, and raises `ModelPolicyError` naming file+line and pointing at the correct API. A contributor who adds a model the wrong way gets a hard startup failure instead of VRAM silently pinned forever. Documented as the one sanctioned path in CODING_STYLE "Adding a model".
**Model choice deviation from the plan:** the plan said rembg for background removal; it is **not installable here** — `rembg`→`pymatting`→`numba` caps numpy at <2.5 while this project is on numpy 2.5.1 (shared by skimage/scipy/torch, all verified working). Downgrading numpy across the whole stack for one op was the wrong trade, so background removal instead uses **torchvision's DeepLabV3/ResNet-50** — a real GPU model, comparable quality for the segmentation case, and *zero* new dependencies. Ops shipped: `segment_foreground` (→MASK, composes with the existing apply_mask/fill_holes/remove_small_objects) and `dinov2_features` (→FEATURE_MAP, composes with the existing feature_mean/feature_channel from P1). Two new presets: *Background Removal (model)* and *DINOv2 Feature Map*, alongside their classical counterparts.
**Concurrency bug found and fixed (pre-existing since P1):** `registry._ensure_loaded()` set its `_loaded` flag *before* importing op modules, so a second thread would see "already loaded" and find an empty registry. Single-threaded callers never hit it; the multi-threaded sidecar exposed it immediately — 2 of 3 concurrent cold requests failed with "Unknown op". Fixed with double-checked locking (flag set only after imports complete) in both `pipeline/registry.py` and `models/registry.py`.
**Verified:** guard catches all 10 bypass patterns with zero false positives on legitimate ops, and a violation planted in a real op file fails startup with file+line. Manager unit tests (fake models, 2s timeout): lazy load, warm reuse (`load_count` stays 1), an in-use model is NOT reaped, idle reap fires, LRU eviction under budget keeps the newer model, an oversized model is rejected with a clear message rather than OOM, background reaper runs unattended. Sidecar end-to-end on the A100: cold call 6.0s (spawn+CUDA+load) vs warm call **0.05s** (120× faster, one load); 5 concurrent cold requests → 5 successes, exactly 1 load. Lifecycle measured with torch's own allocator: **162 MB held while warm → 0 MB after idle unload**, allocator cache included, then reloads on demand (`load_count` 2). All 8 presets validate and run — classical ones stay instant and never touch the sidecar. Note: `nvidia-smi` still shows a few hundred MB for the sidecar after unload — that's the per-process CUDA context, not weights; `MODEL_SIDECAR_IDLE_EXIT_SECONDS` (default 1800) retires the process itself.

### D-031: Pipeline editor was dead in the browser — `|tojson` inside an HTML attribute
**Symptom (user-reported):** the editor page showed only the "Run against" dropdown and a blank button — no palette, no canvas, no nodes.
**Root cause:** `x-data="pipelineEditor({{ pipeline_id|tojson }})"` rendered as `x-data="pipelineEditor("preset_stain-mask")"`. Jinja's `tojson` emits the value *with* its double quotes (it escapes `<`, `>`, `&`, `'` for HTML but deliberately not `"`), so those quotes terminated the `x-data="` attribute. The browser saw the attribute value as the fragment `pipelineEditor(` plus junk attributes, Alpine failed to parse the expression, and the component never initialized — leaving every `x-for`/`x-if`/`x-text` inert. That is exactly the reported UI: empty palette and canvas, a `<select>` showing only its static placeholder `<option>`, and buttons whose labels come from `x-text`.
**Fix:** inject the id as a JS global inside a `<script>` tag (`window.PIPELINE_ID`) and use `x-data="pipelineEditor(window.PIPELINE_ID)"` — the same pattern `index.html` already used successfully for `ACTIVE_SESSION_ID`, which is why sessions never hit this. Swept the templates: no other `tojson` sits inside an HTML attribute.
**Why P3's tests missed it (the honest lesson):** every P3 test called `pipelineEditor('id')` directly from JavaScript, bypassing the HTML attribute entirely. The graph round-trip, type-checking and export logic were all correct — the *wiring between template and component* was the untested part, and it was the only part that was broken. Verifying "the logic works" is not the same as verifying "the page works".
**New regression guard:** `jsdom`-based checks that parse the **actually served** page — assert the `x-data` expression is valid JS, assert no junk attributes appear from quote breakage, and compile all 30 Alpine expression attributes on the page. Plus a full end-to-end run that loads the served HTML with real Drawflow and the real editor module and asserts the UI populates (40 palette ops, 6 canvas nodes, sessions listed, 5-node/5-edge round-trip export).

### D-032: MCP server exposes the pipeline layer, not the chat loop
**Decision:** `src/mcp_server/` is a stdio MCP server over `core/` (+ `run_pipeline_for_session`). Tools: image upload, sessions, op catalog, validate/run/save pipelines, outputs with JPEG previews.
**Why not wrap chat:** an MCP client is already an LLM. Wrapping `/message` would stack Photon's LLM loop under the client's — double cost/latency and the validation feedback gets buried. The client plans; Photon validates and executes.
**Excluded (v1):** `run_code` and saving new custom nodes. Both run arbitrary Python; the AST check + rlimited subprocess is not a boundary to expose to possibly prompt-injected callers. Revisit once `DockerExecutor` exists.
**Identity:** `PHOTON_MCP_USER` (username/email) — stdio is local single-user. Remote (streamable HTTP) needs per-user tokens first.
**Dependency:** `mcp` is an optional extra (`uv sync --extra mcp`) so `dep_sync` (which reads only `[project].dependencies`) does not whitelist it for sandboxed code.
**Side fix:** `SessionManager.get_session` now checks the cached session's `updated_at` against SQLite and reloads if another process wrote it. Before, a second writer (MCP, or any other gunicorn worker) could leave a worker serving stale outputs, and the worker's next `save_session` could overwrite `image_ids` with its stale copy. Status-only changes don't bump `updated_at`, so they can still be stale cross-process.

### D-033: MCP over HTTP authenticates with per-user API keys minted in the UI
**Decision:** `api_keys` table (`key_id`, `user_id`, `name`, `key_hash`, `prefix`, `created_at`, `last_used_at`, `expires_at`, `revoked_at`). Users create, list and revoke keys at `/settings/api-keys` (routes: `GET/POST /api/keys`, `DELETE /api/keys/<id>`, session-cookie auth, owner-scoped, 404 on someone else's key). The MCP HTTP transport requires `Authorization: Bearer pk_...` via the SDK's `TokenVerifier` hook; each call acts as the key's owner (`AccessToken.subject = user_id`, `client_id = key_id`).
**Key handling:** `pk_` + 32 random bytes; only the SHA-256 hex digest is stored, and the plaintext is returned once at creation. A plain hash (not bcrypt) is enough because keys are high-entropy random, and verification runs on every request. `last_used_at` is written at most once per minute per key. `POST /api/keys` requires a real JSON body (no `force=True`), so a cross-site form post can't mint a key.
**stdio unchanged:** local and single-user, still `PHOTON_MCP_USER`. The HTTP transport ignores it. Tools are now collected in a list and registered by `build_server()`, because auth is fixed when `MCPServer` is constructed.
**Not done:** OAuth sign-in for MCP clients (no copy-paste), key scopes (read-only vs run), and an admin CLI to mint keys.

## D-034 — Remote-safe image I/O for MCP (and URL fetch hardening)

**Context.** Over streamable HTTP the MCP caller is remote and may be prompt-injected. `upload_image(path=)` read any file the server's OS user could read (including other users' uploads under `volumes/`), and `url=` fetches (MCP and `POST /api/upload`) could reach loopback, the LAN, or cloud metadata (SSRF).

**Decision.**
- `upload_image` gains `data_base64` + `filename` as the portable way in. `path` stays unrestricted over stdio. Over HTTP it is refused unless `PHOTON_MCP_PATH_ROOTS` allow-lists directories, and `volumes/` is never readable.
- `core/image/fetch.py::fetch_url` is shared by both entry points. It allows http(s) only, every hop (redirects followed manually, max 3) must resolve to global addresses, and the streamed body is capped at `MAX_UPLOAD_BYTES`. `PHOTON_ALLOW_PRIVATE_URLS=1` opts out.
- `download_output` returns full-res outputs as an MCP embedded resource, capped by `PHOTON_MCP_MAX_DOWNLOAD_MB` (default 25).
- New `detach_image`, plus `delete_session`, which requires `confirm_title`. Tools carry `readOnlyHint` or `destructiveHint` annotations.

**Known gaps.** There is a DNS-rebinding window between our resolve and requests' connect. `POST /api/upload` and `GET /api/images/<id>` were unauthenticated and are now `@login_required`. Images still have no owner, so any logged-in user who knows an image_id (a uuid4) can read its metadata.

## D-035 — Published pipelines (instance-wide, versioned snapshots)

**Context.** Custom nodes are resolved from the *running* user's library, so a pipeline that uses one only works for its author. Sharing meant copying code by hand.

**Decision.**
- Publishing turns one of your library pipelines into an immutable version, stored in `published_pipelines` and `published_pipeline_versions`. Each version freezes the graph and a full copy of every custom node it uses (code, ports, params, doc), keyed by the original `custom_node_id`.
- Runs of a published version resolve custom nodes from that snapshot (`run_pipeline_for_session(custom_nodes=...)`), never from anyone's live library. The author's later edits or deletes never break existing versions.
- Re-publishing appends version N+1 to the same publication (one per source pipeline). An identical snapshot is a no-op. The AST check is re-run at publish time.
- Visibility is the whole instance; all users are trusted colleagues. Custom node code runs in the same rlimited subprocess as the author's own runs. The real boundary, the Docker executor, is Phase 2. Users can read a version's custom node code: they should see what they run.
- Unpublishing hides a publication from others. The owner still sees it, versions are never deleted, and publishing again restores it. Deleting the source pipeline keeps the publication (`source_pipeline_id` is SET NULL).
- Fork copies a version into the forker's library. Custom nodes are imported: an identical same-name node is reused, a clash is renamed `name (from owner)`. The graph is rewritten to the new ids, and a credit line goes in the description.
- Output provenance (`GeneratedImage.code`) records `{published_id, version, title, owner, graph}`.
- Core logic is in `core/pipeline/publishing.py`, with no HTTP. The UI is a Public section on the Pipelines page plus Publish/Unpublish in the editor, and `/published/<id>?version=N` is a read-only editor view with a version picker, Run and Fork. MCP gets `list_pipelines(scope, query)`, `get_pipeline(published_id, version)`, `run_pipeline(published_id, version)`, and `publish_pipeline`, `unpublish_pipeline`, `fork_pipeline`.

**Not yet.** Example input/output thumbnails on cards. Photon's own chat LLM doesn't see public pipelines. Composite nodes (pipeline as a node). Per-pipeline MCP tools. Docker isolation.

## D-036 — Sandbox hardening before the container/VM executor

**Context.** Shared pipelines (D-035) mean colleagues run each other's custom node code. The save-time AST check was trivially bypassable: `pathlib.Path(".env").read_text()`, `getattr(os, "sys"+"tem")`, and `np.load(..., allow_pickle=True)` all passed it. Also, the pipeline child imported `config.py`, whose `load_dotenv()` put every app secret into the child's environment.

**Decision (defence in depth; not the final boundary).**
- **Runtime guard** `core/sandbox/guard.py`, a PEP 578 audit hook installed in both children (the pipeline child after trusted imports, and inlined at the top of the run-code harness), before any user code:
  - It denies process creation (subprocess, os.system/exec/spawn/posix_spawn/fork), signals, ctypes, unpickling (`pickle.find_class`), and network sockets.
  - AF_UNIX connects are allowed only to the model sidecar socket.
  - File opens may write only under the run's own dirs, and may read only Python's install, the repo's `src/`, the input and those dirs. Paths are realpath'd, so symlinks resolve to their targets. Filesystem mutations need every path inside write roots.
  - `os.environ` is wiped to an allow-list.
- **`PHOTON_NO_DOTENV=1`** in children skips `load_dotenv`.
- **The model sidecar** is started by the parent; the child may connect to it but never spawn it.
- **AST check** now also rejects:
  - `getattr`/`setattr`/`delattr`/`globals`/`locals`/`vars`/`__builtins__`;
  - dunder access outside a small allow-list, and frame/code attributes;
  - `allow_pickle`, and `torch.load` without `weights_only=True`;
  - fork/spawn/kill-style calls and relative imports;
  - more explicit module blocks (pickle, marshal, gc, inspect, sys, shutil…).
  - `strict_io=True` (custom node save, chat-inline nodes, publish) also rejects all file-I/O calls, since nodes get arrays, not files.

**Verified.** 18 attack variants run directly in the pipeline child (bypassing the AST layer) are all denied, and decoy `.env`/db/upload files stay untouched. The stored custom nodes and LLM snippets show no new false positives. Publishing e2e: 42/42.

**Known limits.**
- C extensions do I/O without audit events (e.g. `cv2.imread`, which strict mode blocks by name; other C paths may exist).
- Audit hooks are not a security boundary per PEP 578.
- The sidecar socket accepts any command from the child.
- `MODEL_IPC_DIR` is shared across runs.
- Fix: the container/VM executor (Phase 2).

## D-037 — Who gets in, and who sees what

**Context.** D-035's trust model ("every account is a trusted colleague") rested on three things that weren't true:
- Sign-up was open to anyone who could reach the server, with no email verification.
- `/previews/<path>` served any file under `volumes/` without login. That included `sessions.db`, with every password hash and API-key hash. `/outputs/<path>` had the same problem, and `/api/llm/model` let anonymous users change the global model.
- Any user who knew an `image_id` could attach another user's image to their own session.

**Decision.**
- **Sign-up policy** (`core/auth/signup_policy.py`), set by env vars:
  - `PHOTON_SIGNUP=open|invite|closed`.
  - Optional `PHOTON_SIGNUP_ALLOWED_DOMAINS` / `PHOTON_SIGNUP_ALLOWED_EMAILS`. Google sign-up must also have `email_verified`. Password sign-up can't prove the email, which is why `invite` exists: it requires `PHOTON_SIGNUP_INVITE_CODE`.
  - Existing accounts can always log in.
  - Startup logs a warning for open mode with no allow-list.
  - `python -m src.backend.cli user add|list|passwd` creates accounts for `closed` mode.
- **File routes.** Both now require login. `/previews/uploads/<file>` serves only a registered image's file or preview, to someone who may access that image. `/outputs/<session_id>/<file>` serves only under a session you own. Everything else returns 404. `llm_routes` and `example_routes` now require login.
- **Image ownership.** New column `images.owner_id`, set on upload (web, MCP) and on clone. `store.user_can_access_image` means: you uploaded it, or it's listed in one of your sessions (this covers pre-migration rows and share clones). It's enforced on metadata, preview, session create and attach, in both web and MCP.
- **Session run status across processes.**
  - `store.claim_session_run` is an atomic `UPDATE ... WHERE status != 'running'`, and `SessionManager.try_start_run` replaces check-then-set. Two processes (Flask, MCP) can no longer both start a run on one session.
  - `save_session` no longer writes `status`, so a stale cached copy can't clobber another process's run.
  - The cache compares `(updated_at, status)`.
  - A `running` claim older than an hour (`status_at`) is treated as abandoned.
  - The LLM loop and manual run release the session in `finally`. The nested pipeline run inside the LLM loop no longer flips the session to idle mid-loop (`manage_status=False`).
- **Thread caps** (`core/sandbox/limits.py`), shared by all children. `OPENCV_FOR_THREADS_NUM` and the TF pool vars join the BLAS ones. Without them, cv2 failed with "Can't spawn new thread" on the 256-core box under RLIMIT_AS.

**Verified.** `tests/`: an anonymous request for `sessions.db` gets 404. A second user gets 404 on another user's image metadata, preview, original, outputs, attach and session create. Cross-process claims work with real child processes. Fresh-DB migrations are ordered (a test caught an `ALTER TABLE images` that ran before the table existed).

## D-038 — Remote sandbox worker (gVisor, optional GPU), ready to deploy

**Context.** The D-036 guard is a speed bump. Custom nodes may need torch/tensorflow on a GPU, which rules out WASM. The dev box is itself a container (no KVM, no Docker), so Firecracker and runsc can't run here.

**Decision.**
- One switch, `core/sandbox/backend.py`: `PHOTON_SANDBOX=local|remote`. `get_executor()` handles run-code and `run_pipeline()` handles graphs. Routes, the LLM loop and MCP call only these.
- **Remote** (`core/sandbox/remote.py`) sends the job and input file over HTTP with a bearer token to the worker (`src/sandbox_worker/server.py`). The worker runs the same local executors (AST, rlimits, guard) in a scratch dir. It returns a zip with `result.json` and outputs, with paths relative. The client validates every entry (no absolute paths, no `..`, no links, count and size caps) before extracting into the caller's dirs.
- **Worker image** `docker/sandbox-worker/`:
  - Slim Python with the cu121 wheels; it holds only code, with no `.env`, `volumes/` or frontend.
  - Compose sets `runtime: runsc`, read-only root, tmpfs scratch, all caps dropped, `no-new-privileges`, pids/mem/cpu limits and an optional GPU via nvproxy.
  - `SANDBOX_MEMORY_LIMIT_GB=0` disables RLIMIT_AS, which CUDA can't live under; the container limit caps memory instead.
  - Egress is blocked with a `DOCKER-USER` firewall rule (see the README).
- Removed the `DockerExecutor` stub and `config.EXECUTOR_CLASS`.

**Verified.** Without gVisor, against a localhost worker: protocol, auth, zip-traversal rejection, the app's run route end to end, and the full guard attack suite through the worker (`PHOTON_SANDBOX=remote pytest tests/test_sandbox.py`: 34 passed).

**Not verified.** Running under `runsc`, GPU via nvproxy, model ops on the worker's own sidecar. Checklist: `docker/sandbox-worker/README.md`.

## D-039 — Shared pipelines in the chat loop; async MCP runs

- The chat system prompt lists up to 30 published pipelines (id, title, version, owner, op chain, short description). Each entry is fenced as user-written data. The LLM can answer with `{"response_type": "pipeline", "pipeline": {"published_id": ..., "version"?: N}}`. The loop resolves the frozen snapshot (`publishing.resolve_for_run`) and runs it with provenance. An unknown id becomes a feedback turn.
- MCP `run_pipeline(wait=False)` returns a `job_id` immediately; `get_run(job_id)` returns progress, then the usual summary and previews. Jobs live in the MCP process's memory for an hour after they finish, and only the owner can see them. Outputs are saved to the session either way.

## D-040 — Card thumbnails for published pipelines; chat cards for shared runs

**Thumbnails (#8).**
- The image is the author's own latest output of the published graph. That is real data, and no extra run is needed. Other users' runs are never used, since their data isn't the author's to share.
- "Same graph" means node ids, ops, params and edges match; editor `pos` and key order are ignored. Pipeline outputs already store the graph as provenance (`outputs.code`), so this needs no schema change.
- On publish, the image is copied into `volumes/published_thumbs/<published_id>.jpg`, downscaled to 480 px on the long edge. It is copied rather than referenced so it survives the session being deleted and never points into session files.
- It is served by `GET /api/published/<id>/thumbnail`, under the same visibility rule as the publication.
- The publish dialog has a checkbox, on by default, that says the image is visible to everyone; MCP `publish_pipeline(thumbnail=)` does the same. Unchecking it removes the image.
- Re-publishing an unchanged pipeline refreshes the image. A new version with no matching output drops the old image, because that image no longer shows what the pipeline does.
- Cards without an image show the op chain.

**Chat cards for shared runs.** Once the chat loop has resolved a `published_id`, the stored message is the resolved graph plus `published: {published_id, version, title, owner}`, and the UI gets a `pipeline_resolved` event. The card's "Save" becomes a Fork, which imports the snapshot's custom nodes into the user's library. "Open" goes to the read-only `/published/<id>?version=N`. A plain save would reference custom nodes the user doesn't own.

## D-041 — Library functions as pipeline nodes (phase 1)

**Context.** The curated catalogue (about 40 hand-written ops) kept the platform narrow: something like multi-Otsu meant writing a custom node. The libraries already document their functions well (304 of 309 public skimage functions in the exposed modules have numpydoc Parameters and Returns sections).

**Decision.**
- **Naming.** A node can name a function directly: `{"op": "lib:<module>.<function>"}` (`core/pipeline/library.py`). The graph schema, runner, sandbox child and remote worker are unchanged; `registry.get_op` resolves the `lib:` prefix.
- **Modules exposed:** `skimage.{color, exposure, feature, filters, filters.rank, measure, morphology, restoration, segmentation, transform, util, registration, metrics, draw}`, `scipy.ndimage` and `cv2`, about 600 functions.
- **How specs are generated:**
  - **Python functions** use `inspect.signature` and numpydoc (`scipy._lib._docscrape`). Array-typed arguments become input ports, optional when they have a default. An "int or ndarray" argument becomes both an optional port and a param; a connected port wins. Everything else is a param whose kind (int, float, bool, str, choice or json) comes from the docstring type. Each documented return value becomes an output port. `out`/`output` buffers are hidden.
  - **OpenCV builtins** are parsed from their `f(a, b[, c]) -> x, y` docstring line. Image-like names become ports. An optional argument that is also returned is an output buffer and is hidden. Constants can be given by name (`"THRESH_BINARY|THRESH_OTSU"`); flat lists become tuples and nested lists become float32 arrays.
- **Only set params are passed** (`OpSpec.pass_defaults=False`), so the function's own defaults apply. Optional ports left unconnected are not passed either.
- **Types:**
  - New `ARRAY` type: it connects to and from every array type, plus NUMBER into ARRAY.
  - `ANY` is now also a wildcard source.
  - The runtime coercion check runs only on 2-D and larger arrays.
  - IMAGE_RGB into IMAGE_GRAY still needs an explicit `to_grayscale`.
- **Non-image outputs** (thresholds, numbers, tuples, regionprops tables) are reported back as size-capped JSON `values` per node:
  - in the run result and the `node_values` SSE event;
  - in the editor inspector and the chat card;
  - in the MCP summary and the LLM's run feedback.

  Only image-like arrays become previews or saved outputs.
- **Glue ops** (`ops/glue.py`): `get_item`, `digitize`, `math`, `compare`, `astype`, `take`, `stack`, `to_number`. This is a curated slice of numpy, not numpy itself, which has too many footguns (memory-mapping, pickle loading, object arrays).
- **Discovery:**
  - Editor: searches `/api/ops/library?q=` (debounced), and fetches the specs a graph uses via `?names=`. `/api/ops` stays core-only unless `?library=1`, because the full list is about 600 KB.
  - MCP: `list_ops(query=...)` returns `library_ops`, and `describe_op(name)` returns one spec.
  - Chat: the system prompt lists the function names by module (about 9 KB). Validation errors now list a node's real inputs, outputs and params, so a wrong guess is corrected in one round.
- **Reproducibility.** Published versions record `{distribution: version}` for the libraries their graph uses, plus numpy (`published_pipeline_versions.env_json`, `environment` in the API). An unchanged graph doesn't get a new version just because a library was upgraded.
- The catalogue (about 3 s to build) warms in a background thread at app start.

**Security.**
- A name is accepted only if its module is allow-listed and the attribute is a public function of it, so there's no dotted-path resolution of user input.
- Excluded:
  - functions with file-like arguments (`fname`, `path`, `url`...);
  - functions whose name contains read, write, load, save, show, fetch, download, plot, view or pickle;
  - functions with a required callable argument;
  - an explicit deny list (`cv2.imwrite*`, `cv2.imencode*`, `cv2.selectROI*`, `cv2.addText`, `skimage.util.apply_parallel`, `skimage.measure.ransac`...);
  - OpenCV functions without an image argument (global state such as `setNumThreads`).
- Params are JSON, so no callables or objects can be passed in.
- The allow-list guards against footguns; the boundary is still the sandbox child (rlimits and the D-036 guard, and the D-038 worker when it's enabled).

**Not in phase 1.** scikit-learn make/fit/predict with a trained-model value (phase 2). Training jobs (phase 3). Pipeline-as-a-node (#9) and per-pipeline MCP tools (#10), which now get simpler because the types are generic.
