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
