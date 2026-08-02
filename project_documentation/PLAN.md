# Implementation Plan

1. [x] Scaffold folders, config, `.env.example`, requirements/tailwind.
2. [x] Image metadata + upload endpoint + preview generation + metadata UI.
3. [x] Sandbox executor + AST safety check.
4. [x] Build the SQLite store (`core/session/store.py`) and the lazy-LRU `manager.py`.
   (2026-07-12: chat-turn write-through was missing — fixed, see DECISIONS D-010; restart survival verified end-to-end.)
5. [x] Self-programming window end-to-end with save-on-run persistence.
6. [x] "View Example Code" dialog + shared `code_dialog.html`/`codeDialog.js`.
7. [x] LLM client + prompt templates + schema validation for the `response_type`-driven contract.
   (Deviation: providers are Gemini/OpenAI-compatible, not Anthropic — predates 2026-07-12, not yet logged as a DECISIONS entry.)
8. [x] Wire the full turn-handling logic end-to-end on the backend for LLM windows (chat bubbles, code-result cards, auto-retry loop).
9. [x] Frontend: upload panel → single LLM window (chat pane + image compare with "View Code") → window manager (restore from /api/windows) → share dialog + /shared/<token> route → download dialog.
10. [x] (2026-07-12) Markdown/math/mermaid chat rendering, Dracula code theming, resizable right panel — see DECISIONS D-007..D-009.
11. [x] (2026-07-12) OpenRouter provider + runtime model dropdown, batch output-dir collision fix, sandbox BLAS thread capping — see DECISIONS D-013..D-015.
12. [ ] **v2** (see `photon_v2_plan.md`): auth, unified sessions, share-as-clone, session-scoped download, smart deletion, auto dep sync.
    - [x] Phase 1: Auto dependency sync (`dep_sync.py`) — see DECISIONS D-016.
    - [x] Phase 2: Auth — username/password (always) + Google OAuth (optional) — see DECISIONS D-017.
    - [x] Phase 3: Unified session model (replaces windows) — backend only, see DECISIONS D-018.
      **Frontend is broken until Phase 6** — old `chat.js`/`codeEditor.js`/`windowManager.js`/partials
      still speak the old `/api/windows/*` + `mode` contract, which no longer exists.
    - [x] Phase 4: Share-as-clone — see DECISIONS D-019.
    - [x] Phase 5: Session-scoped download + smart deletion — see DECISIONS D-020.
    - [x] Phase 6: Frontend overhaul — see DECISIONS D-021. **Not visually browser-tested**
      (no browser tool available this session) — verified via JS syntax checks, Jinja
      render checks, and a full backend E2E flow matching every frontend call. Do a
      manual pass before trusting it fully.
13. [x] **v3** (see `photon_v3_plan.md`): pipeline system — op registry, typed DAG, editor, LLM composition.
    - [x] P1: backend core — `core/pipeline/` (38 ops, typed runner, subprocess executor),
      pipelines/custom_nodes tables, 6 seeded presets, API routes — see DECISIONS D-026.
    - [x] P2: LLM integration — `response_type: "pipeline"`, prompt catalog, pipeline chat card — see DECISIONS D-027.
    - [x] P3: Drawflow drag-drop editor — see DECISIONS D-028, and D-031 for the
      template-wiring bug that made it blank in the browser (fixed + regression-tested).
    - [x] P4: GPU model lifecycle as an enforced constraint + first model ops
      (DINOv2 features, DeepLabV3 background removal) — see DECISIONS D-029/D-030.
      rembg dropped: incompatible with numpy 2.5 (see D-030).
