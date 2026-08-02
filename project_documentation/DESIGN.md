# Design

## UI Layout (v2, D-021)

```
┌──────────────────────────────────────────────────────────────────────────┐
│  [Logo]                    [Model selector]  [GitHub]  [User ▾ Logout]   │
├────────────┬───────────────────────────────────────────────────────────┤
│ [+ New     │  Session Title (editable)      [Share] [Download] [Delete] │
│  Session]  │  ┌─────────────────────────────────────────────────────┐  │
│            │  │ [💬 Chat | </> Code toggle]                         │  │
│ Session 1  │  │ ─────────────────────────────────────────────────── │  │
│ Session 2  │  │ Left: image compare + filmstrip + Add Image         │  │
│ Session 3  │  │ Right (resizable): chat history+input, OR           │  │
│  ...       │  │   CodeMirror editor + Run + exec log — same session,│  │
│            │  │   both mounted (x-show), state survives toggling    │  │
│  [«]       │  └─────────────────────────────────────────────────────┘  │
└────────────┴───────────────────────────────────────────────────────────┘
```

A session has no locked mode (D-018) — the toggle is purely client-side UI
state (`sessionPane.js`'s `viewMode`), not persisted server-side. Both
sub-views read/write the same session's `llm_conversation`/`current_code`/
`outputs`.

## Responsive & Sizing Conventions (added 2026-08-02, D-022)

- **Breakpoints**: `lg` (1024px) is the split point for the session pane —
  below it, images and the chat/code panel stack vertically (panel gets a
  fixed 55% height block, drag-resize handle hidden); at `lg+` they sit
  side-by-side with the draggable `--panel-w` split. `sm` (640px) is the
  split for the original/output compare pair (stacks below it) and for
  hiding secondary labels (toolbar button text, header tagline, model
  selector).
- **Dialogs**: one shared shell — `w-full max-w-{md|lg|3xl} max-h-[85vh]
  flex flex-col overflow-hidden rounded-2xl border-gray-700 bg-gray-900`,
  header/footer `px-6 py-4`, scrollable body. Sizes: share=md,
  download/new-session=lg, code=3xl.
- **Sidebar**: JS-managed px width, clamped by `max-w-[85vw]`; starts
  collapsed when `window.innerWidth < 1024`.
- **Panel width**: set as a CSS variable (`--panel-w`) so the width only
  applies via `lg:w-[var(--panel-w)]` — no inline width fighting the
  stacked layout on small screens.
- Root uses `h-screen` with a `100dvh` upgrade (`supports-[height:100dvh]`)
  for mobile browser chrome.
- After editing any template/JS class names: `npm run build:css` (output.css
  is generated, gitignored).

## Tailwind Conventions

- Dark theme base: `bg-gray-950` body, `bg-gray-900` panels, `bg-gray-800` cards
- Accent: `indigo-500` for primary actions, `emerald-500` for success, `red-500` for errors
- Code blocks: `font-mono text-sm bg-gray-900 text-green-300 rounded p-3`
- Metadata JSON block: read-only `<pre>` with copy button overlay (top-right corner)

## Pipeline Editor (v3 P3, D-028)

```
┌──────────────────────────────────────────────────────────────────────┐
│ ← Pipelines │ [title]  preset?  unsaved?    [session ▾][Run][Save]  │
├──────────┬────────────────────────────────────────────┬─────────────┤
│ Palette  │  Drawflow canvas (dark, dotted grid)       │ Inspector   │
│ [filter] │   ┌──────────┐      ┌──────────┐           │ op name+doc │
│ +Source  │   │source img│─────▶│to_gray   │──▶ ...    │ params from │
│ color    │   └──────────┘      └──────────┘           │ schema      │
│ threshold│                                            │ ───────────  │
│ morph…   │  [−][+][Validate][Delete node]             │ last-run    │
│ custom   │  ⚠ issues bar / ✗ run error                │ node previews│
└──────────┴────────────────────────────────────────────┴─────────────┘
```

Ports are typed even though Drawflow isn't: the client mirrors the server's
coercion table (from `GET /api/ops`) and drops incompatible connections on
`connectionCreated` with an explanatory toast; the server re-validates on
Save and Run. Node HTML is plain DOM, styled dark in markdown.css
(`#drawflow .drawflow-node`, `.df-node-title`, `.df-port-label`).

## Component Partials → JS Module Mapping (v2, D-021)

| Partial                     | JS Module           | Alpine component              |
|------------------------------|----------------------|--------------------------------|
| session_sidebar.html        | sessionManager.js   | x-data="sessionManager()"     |
| new_session_dialog.html     | newSessionDialog.js | x-data="newSessionDialog()"   |
| session_pane.html           | sessionPane.js      | x-data="sessionPane(sess)"    |
| metadata_block.html         | (inline copy button) | —                              |
| code_dialog.html            | codeDialog.js       | x-data="codeDialogViewer()"   |
| image_compare.html          | imageViewer.js       | x-data="imageViewerSlot(i)"   |
| download_dialog.html        | downloadDialog.js   | x-data="downloadDialog()"     |
| share_dialog.html           | shareDialog.js       | x-data="shareDialog()"        |
| pipelines.html (page)       | inline                | x-data="pipelineLibrary()"    |
| pipeline_editor.html (page) | pipelineEditor.js    | x-data="pipelineEditor(id)"   |

`session_pane.html` replaces the old `chat_pane.html`+`code_editor_pane.html`
pair — one partial, one Alpine component, both chat and code UI, toggled by
`viewMode`. `new_session_dialog.html` replaces `upload_panel.html` (upload is
now a modal that creates a session, not a persistent sidebar panel).

## Code & Markdown Rendering (added 2026-07-12)

All code display uses the **Dracula** theme, consistently across three surfaces:

| Surface                              | Mechanism                                         |
|--------------------------------------|---------------------------------------------------|
| Code dialog (Preview/View/Example)   | highlight.js + `base16/dracula.min.css`, via `x-highlight` directive |
| Chat illustrative snippets           | Same `x-highlight` directive                      |
| Manual editor (CodeMirror 6)         | `@uiw/codemirror-theme-dracula` (esm.sh), oneDark fallback |

LLM chat `message` fields are rendered as sanitized markdown via a shared
client-side pipeline in `static/js/markdownRenderer.js` (inspired by the
user's papers-app markdown engine, but client-side since chat arrives over SSE):

```
message string
  → extract ```mermaid fences → placeholder divs
  → extract $...$ / $$...$$ math (non-code segments only) → placeholder spans
  → marked (GFM, breaks) → HTML
  → DOMPurify.sanitize          ← LLM output is untrusted, esp. shared windows
  → insert into DOM, then: highlight.js on code fences,
    KaTeX render into math slots, mermaid.run on diagram slots
```

Exposed as two Alpine directives registered on `alpine:init`:
- `x-markdown="expr"` — full markdown pipeline (chat bubbles, code-card notes)
- `x-highlight="expr"` — single highlighted Python block (dialog, snippets)

All libraries load from CDN in `base.html` (marked, DOMPurify, highlight.js,
KaTeX, mermaid@10 UMD); every feature degrades gracefully to plain text if a
CDN fails. Prose styles live in `static/css/markdown.css` (`.markdown-body`),
kept out of the Tailwind build.

The system prompt (prompts.py, "Message Rendering" section) tells the LLM the
chat supports markdown/math/mermaid so it can use them in explanations.
User messages and error bubbles stay plain text (`x-text`).

## Resizable Right Panel (added 2026-07-12, ratio-based same day)

The right-side panel (chat sub-view or code sub-view — same panel, toggled by
`viewMode`) is width-adjustable by dragging its left edge — same interaction
as the left session sidebar. Shared Alpine mixin `panelResizeState()` lives in
`sessionManager.js`, spread into `sessionPane()` (D-021; previously spread
into two separate components, `chatPane()`/`codeEditor()`, before the D-018
unification). Width is a **percentage of the pane container** (default 45% —
i.e. a 45:55 panel:image split), clamped 25–65%, persisted in `localStorage`
(`photon:panelWidthPct`). Percentage rather than px so the default ratio holds
at any viewport.

## LLM Activity Halo + Stop Button (added 2026-07-12)

While the generate→execute→fix loop is in flight (`running`), the chat panel
gets an animated "halo": a 2px rotating conic-gradient border ring
(`.llm-halo` in markdown.css — masked ring, `@property --halo-angle`
animation; browsers without `@property` just show a static gradient ring).
The Send button is replaced by a red **Stop** button while running, showing
the current attempt count. Stop calls `POST /api/sessions/<id>/stop`; the loop
halts at its next safe boundary (see API_REFERENCE), emits a `stopped` SSE
event, and persists a "⏹ Stopped by user." chat turn so the abort is visible
in restored history.

## Image Viewer

- Side-by-side: original left, latest output right
- Filmstrip: horizontally scrollable `<div>` of thumbnail cards below main compare
- Clicking a filmstrip card promotes it to the right panel
- For n>4 channel outputs: label overlay "Preview composite — download for full data"
