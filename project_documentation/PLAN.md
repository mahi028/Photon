# Implementation Plan

1. [x] Scaffold folders, config, `.env.example`, requirements/tailwind.
2. [x] Image metadata + upload endpoint + preview generation + metadata UI.
3. [x] Sandbox executor + AST safety check.
4. [ ] Build the SQLite store (`core/session/store.py`) and the lazy-LRU `manager.py`.
5. [ ] Self-programming window end-to-end with save-on-run persistence.
6. [ ] "View Example Code" dialog + shared `code_dialog.html`/`codeDialogViewer.js`.
7. [ ] LLM client + prompt templates + schema validation for the `response_type`-driven contract.
8. [ ] Wire the full turn-handling logic end-to-end on the backend for LLM windows (chat bubbles, code-result cards, auto-retry loop).
9. [ ] Frontend: upload panel → single LLM window (chat pane + image compare with "View Code") → window manager (restore from /api/windows) → share dialog + /shared/<token> route → download dialog.
