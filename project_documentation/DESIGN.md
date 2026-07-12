# Design

## UI Layout

```
┌──────────────────────────────────────────────────────────────────────────┐
│  [Upload Panel]                      [Tab Bar: + New | LLM | Manual | ×] │
│  ┌────────────┐                      ┌─────────────────────────────────┐  │
│  │ drag-drop  │                      │  Active Window Pane             │  │
│  │ or URL     │                      │  ─────────────────────────────  │  │
│  └────────────┘                      │  [LLM mode]: chat history       │  │
│  [Image Preview]                     │              input + send btn   │  │
│  [Metadata JSON block + Copy]        │  [Manual mode]: CodeMirror      │  │
│                                      │               Run btn           │  │
│                                      │               Exec log panel    │  │
│                                      │  Both: metadata block (collap.) │  │
│                                      │         View Example Code btn   │  │
│                                      │  ─────────────────────────────  │  │
│                                      │  [Image Compare: input | output]│  │
│                                      │  [Filmstrip: older outputs]     │  │
│                                      └─────────────────────────────────┘  │
│  [Download Button (global)]                                                │
└──────────────────────────────────────────────────────────────────────────┘
```

## Two Window Modes

Both modes are fully symmetric except for who writes the code:

| Aspect              | LLM mode                          | Manual mode                        |
|---------------------|-----------------------------------|------------------------------------|
| Code author         | LLM (Claude)                      | User (CodeMirror editor)           |
| Retry on failure    | Auto (loop, up to MAX_ITER)       | User-initiated (hit Run again)     |
| Error display       | SSE stream messages in chat       | Execution log panel                |
| LLM system prompt   | Yes                               | No                                 |
| Sandbox             | Same SubprocessExecutor           | Same SubprocessExecutor            |
| Function contract   | Same `main(input_path, output_dir)` | Same                              |
| Output              | GeneratedImage → compare view     | GeneratedImage → compare view      |

## Tailwind Conventions

- Dark theme base: `bg-gray-950` body, `bg-gray-900` panels, `bg-gray-800` cards
- Accent: `indigo-500` for primary actions, `emerald-500` for success, `red-500` for errors
- Code blocks: `font-mono text-sm bg-gray-900 text-green-300 rounded p-3`
- Metadata JSON block: read-only `<pre>` with copy button overlay (top-right corner)

## Component Partials → JS Module Mapping

| Partial                    | JS Module            | Alpine component     |
|---------------------------|----------------------|----------------------|
| upload_panel.html         | uploader.js          | x-data="uploader()" |
| metadata_block.html       | (inline copy button) | —                    |
| window_tab.html           | windowManager.js     | x-data="winManager()"|
| chat_pane.html            | chat.js              | x-data="chatPane()" |
| code_editor_pane.html     | codeEditor.js        | x-data="codeEditor()"|
| example_code_dialog.html  | exampleDialog.js     | —                    |
| image_compare.html        | imageViewer.js       | x-data="imgViewer()"|
| download_dialog.html      | downloadDialog.js    | x-data="dlDialog()" |

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

The right-side panel (chat pane in LLM mode, code editor pane in manual mode)
is width-adjustable by dragging its left edge — same interaction as the left
upload sidebar. Shared Alpine mixin `panelResizeState()` in `windowManager.js`,
spread into both `chatPane()` and `codeEditor()`. Width is a **percentage of
the pane container** (default 45% — i.e. a 45:55 panel:image split), clamped
25–65%, persisted in `localStorage` (`photon:panelWidthPct`), shared by both
modes. Percentage rather than px so the default ratio holds at any viewport.

## LLM Activity Halo + Stop Button (added 2026-07-12)

While the generate→execute→fix loop is in flight (`running`), the chat panel
gets an animated "halo": a 2px rotating conic-gradient border ring
(`.llm-halo` in markdown.css — masked ring, `@property --halo-angle`
animation; browsers without `@property` just show a static gradient ring).
The Send button is replaced by a red **Stop** button while running, showing
the current attempt count. Stop calls `POST /api/windows/<id>/stop`; the loop
halts at its next safe boundary (see API_REFERENCE), emits a `stopped` SSE
event, and persists a "⏹ Stopped by user." chat turn so the abort is visible
in restored history.

## Image Viewer

- Side-by-side: original left, latest output right
- Filmstrip: horizontally scrollable `<div>` of thumbnail cards below main compare
- Clicking a filmstrip card promotes it to the right panel
- For n>4 channel outputs: label overlay "Preview composite — download for full data"
