/**
 * codeEditor.js — Alpine.js component for the manual code editor pane.
 *
 * Uses CodeMirror 6 for syntax highlighting.
 * Calls POST /api/windows/<id>/run-code and displays ExecutionResult.
 */

const CODE_STUB = `def main(input_path: str, output_path_dir: str) -> str:
    # Available: numpy, PIL, cv2, scipy, skimage, tifffile
    # input_path       — path to the uploaded image (read-only)
    # output_path_dir  — directory to save your output file
    # Return           — full path to the saved file

    import numpy as np
    from PIL import Image
    from pathlib import Path

    # Load image
    arr = np.array(Image.open(input_path))

    # --- your transformation here ---

    # Save result
    out_path = Path(output_path_dir) / "output.png"
    Image.fromarray(arr).save(str(out_path))
    return str(out_path)
`;

function codeEditor(window) {
  return {
    window,
    running: false,
    execResult: null,
    showMeta: false,
    outputsBySlot: {},  // slotIdx -> GeneratedImage[]
    // Backward compat, use slot 0
    get outputs() { return this.outputsBySlot[0] || []; },
    originalUrl: window.previewUrl || null,
    _cmView: null,

    get currentCode() {
      if (this._cmView) {
        return this._cmView.state.doc.toString();
      }
      return CODE_STUB;
    },

    async init() {
      // Restore existing outputs from server (so they survive tab switches)
      try {
        // Fetch history to restore originalUrl and metadata
        const histRes = await fetch(`/api/windows/${this.window.window_id}/history`);
        if (histRes.ok) {
          const data = await histRes.json();
          if (data.preview_url) this.originalUrl = data.preview_url;
          if (data.metadata) this.window.metadata = data.metadata;
        }

        const outRes = await fetch(`/api/windows/${this.window.window_id}/outputs`);
        if (outRes.ok) {
          const allOutputs = await outRes.json();
          this._buildOutputsBySlot(allOutputs);
        }
      } catch (e) {
        console.error('Failed to load outputs:', e);
      }
      // Attempt to init CodeMirror 6 if available via CDN
      await this._initCodeMirror();
    },

    async _initCodeMirror() {
      // CodeMirror 6 loaded via ESM CDN — use dynamic import fallback approach
      try {
        const { EditorView, basicSetup } = await import(
          'https://esm.sh/codemirror@6?bundle'
        );
        const { python } = await import(
          'https://esm.sh/@codemirror/lang-python@6?bundle'
        );
        const { oneDark } = await import(
          'https://esm.sh/@codemirror/theme-one-dark@6?bundle'
        );

        const container = this.$refs.editorContainer;
        if (!container) return;

        this._cmView = new EditorView({
          doc: this.window.current_code || CODE_STUB,
          extensions: [basicSetup, python(), oneDark],
          parent: container,
        });
      } catch (e) {
        // Fallback to textarea if CodeMirror fails to load
        console.warn('CodeMirror CDN load failed, using textarea fallback:', e);
        const container = this.$refs.editorContainer;
        if (container) {
          container.innerHTML = `<textarea
            id="fallback-editor"
            class="w-full h-full bg-gray-900 text-green-300 font-mono text-xs p-3 border-0 outline-none resize-none"
            spellcheck="false"
          >${this.window.current_code || CODE_STUB}</textarea>`;
        }
      }
    },

    _getCode() {
      if (this._cmView) {
        return this._cmView.state.doc.toString();
      }
      // Fallback textarea
      const ta = document.getElementById('fallback-editor');
      return ta ? ta.value : CODE_STUB;
    },

    async runCode() {
      const code = this._getCode().trim();
      if (!code || this.running) return;

      this.running = true;
      this.execResult = null;

      try {
        const res = await fetch(`/api/windows/${this.window.window_id}/run-code`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({code}),
        });

        const data = await res.json();
        this.execResult = data;

        if (data.batch_run || (data.file_exists && data.image_id)) {
          // Re-fetch outputs from server
          const outRes = await fetch(`/api/windows/${this.window.window_id}/outputs`);
          if (outRes.ok) {
             const allOutputs = await outRes.json();
             this._buildOutputsBySlot(allOutputs);
             
             // Emit fe:new-output for the last output in each slot so download dialog updates
             if (this.window.images) {
                 this.window.images.forEach((img, idx) => {
                     const slotOuts = this.outputsBySlot[idx];
                     if (slotOuts && slotOuts.length > 0) {
                         globalThis.dispatchEvent(new CustomEvent('fe:new-output', {detail: slotOuts[slotOuts.length - 1]}));
                     }
                 });
             }
          }
        }
      } catch (e) {
        this.execResult = {
          stderr: e.message,
          file_exists: false,
          timed_out: false,
          time_taken_seconds: 0,
        };
      } finally {
        this.running = false;
      }
    },
    
    _buildOutputsBySlot(allOutputs) {
      const newOutputsBySlot = {};
      if (this.window.images) {
         this.window.images.forEach((img, idx) => { newOutputsBySlot[idx] = []; });
      }
      allOutputs.forEach(out => {
         let slot = 0;
         if (out.description && out.description.includes('slot')) {
             const m = out.description.match(/slot(\d+)_/);
             if (m) slot = parseInt(m[1], 10);
         }
         if (!newOutputsBySlot[slot]) newOutputsBySlot[slot] = [];
         newOutputsBySlot[slot].push(out);
      });
      this.outputsBySlot = newOutputsBySlot;
    },
  };
}
