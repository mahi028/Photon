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

## Image Viewer

- Side-by-side: original left, latest output right
- Filmstrip: horizontally scrollable `<div>` of thumbnail cards below main compare
- Clicking a filmstrip card promotes it to the right panel
- For n>4 channel outputs: label overlay "Preview composite — download for full data"
