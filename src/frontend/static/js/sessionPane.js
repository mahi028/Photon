/**
 * sessionPane.js — Alpine.js component for a session's workspace.
 *
 * Replaces chat.js + codeEditor.js (Phase 6, D-021): a session has no locked
 * mode, so this single component owns BOTH the LLM chat state and the manual
 * editor state for one session, toggled client-side via `viewMode`. Both
 * sub-views stay mounted (x-show, not x-if) so CodeMirror initializes once
 * and chat scroll position survives toggling.
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

function sessionPane(sess) {
  return {
    ...panelResizeState(),
    sess,
    viewMode: 'chat',   // 'chat' | 'code' — UI-only, not persisted server-side
    showMeta: false,

    // --- chat state ---
    messages: [],
    inputText: '',
    running: false,
    statusMsg: '',
    attemptLabel: `1/6`,

    // --- code state ---
    execResult: null,
    _cmView: null,

    // --- shared (outputs feed both views) ---
    outputsBySlot: {},
    get outputs() { return this.outputsBySlot[0] || []; },
    originalUrl: sess.previewUrl || null,
    addingImage: false,
    customNodes: [],    // for resolving custom_node_id -> name on pipeline cards

    async init() {
      try {
        const res = await fetch(`/api/sessions/${this.sess.session_id}/history`);
        if (res.ok) {
          const data = await res.json();
          // internal=true turns are loop plumbing (execution feedback fed back
          // to the LLM) — part of the LLM context but not shown in the chat UI
          this.messages = (data.llm_conversation || []).filter(m => !m.internal).map(m => ({
            role: m.role,
            response_type: m.response_type,
            content: m.message,
            code: m.code,
            was_executed: m.was_executed,
            execution_result: m.execution_result,
            pipeline: m.pipeline,
            turn_index: m.turn_index,
            outputs: [],   // filled below from source_turn_index linkage
            nodeStatus: {},
          }));
          if (data.preview_url) this.originalUrl = data.preview_url;
          if (data.metadata) this.sess.metadata = data.metadata;
          if (data.images) this.sess.images = data.images;
          this.sess.current_code = data.current_code;
          this._scrollChat();
        }

        const outRes = await fetch(`/api/sessions/${this.sess.session_id}/outputs`);
        if (outRes.ok) {
          const allOutputs = await outRes.json();
          this._buildOutputsBySlot(allOutputs);
          // Link outputs back to the chat turn that produced them (Preview Output)
          allOutputs.forEach(out => {
            if (out.source_turn_index == null) return;
            const msg = this.messages.find(m => m.turn_index === out.source_turn_index);
            if (msg) msg.outputs.push(out);
          });
        }
      } catch (e) {
        console.error('Failed to load session history:', e);
      }

      try {
        const cnRes = await fetch('/api/custom-nodes');
        if (cnRes.ok) this.customNodes = await cnRes.json();
      } catch (e) {
        // non-fatal: pipeline cards fall back to showing the raw node id
      }

      await this._initCodeMirror();
    },

    // ---------------------------------------------------------------------
    // Chat
    // ---------------------------------------------------------------------

    async sendMessage() {
      const prompt = this.inputText.trim();
      if (!prompt || this.running) return;

      this.running = true;
      this.inputText = '';
      this.messages.push({role: 'user', content: prompt});
      this.statusMsg = 'Starting...';

      try {
        const res = await fetch(`/api/sessions/${this.sess.session_id}/message`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({prompt}),
        });
        if (!res.ok) {
          const data = await res.json();
          this.messages.push({role: 'assistant', response_type: 'error', content: `Error: ${data.error || 'Unknown error'}`});
          this.running = false;
          return;
        }
        this._listenToStream();
      } catch (e) {
        this.messages.push({role: 'assistant', response_type: 'error', content: `Error: ${e.message}`});
        this.running = false;
      }
    },

    async stopLoop() {
      try {
        await fetch(`/api/sessions/${this.sess.session_id}/stop`, {method: 'POST'});
        this.statusMsg = 'Stopping...';
      } catch (e) {
        console.error('Stop request failed:', e);
      }
    },

    async runSnippet(code) {
      if (!code || this.running) return;
      this.running = true;
      this.statusMsg = 'Running snippet...';

      try {
        const res = await fetch(`/api/sessions/${this.sess.session_id}/run-code`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({code}),
        });
        const data = await res.json();

        if (!res.ok) {
          alert('Error running snippet: ' + (data.error || 'Unknown'));
        } else if (data.batch_run) {
          const outRes = await fetch(`/api/sessions/${this.sess.session_id}/outputs`);
          if (outRes.ok) this._buildOutputsBySlot(await outRes.json());
        } else {
          alert('Snippet finished. Stderr:\n' + (data.stderr || 'None'));
        }
      } catch (e) {
        alert('Execution failed: ' + e.message);
      } finally {
        this.running = false;
        this.statusMsg = '';
      }
    },

    _listenToStream() {
      const evtSource = new EventSource(`/api/sessions/${this.sess.session_id}/stream`);

      evtSource.onmessage = (e) => {
        const event = JSON.parse(e.data);

        if (event.event === 'stream_end') {
          evtSource.close();
          this.running = false;
          this.statusMsg = '';
          return;
        }
        if (event.event === 'attempt') {
          this.statusMsg = event.message;
          this.attemptLabel = `${event.attempt}/${event.max}`;
        }
        if (event.event === 'llm_message') {
          this.messages.push({
            role: 'assistant',
            response_type: event.response_type,
            content: event.message,
            code: event.code,
            pipeline: event.pipeline,
            was_executed: false,
            outputs: [],
            nodeStatus: {},
          });
        }

        // --- pipeline events (v3 P2) ---
        if (event.event === 'pipeline_invalid') {
          const msg = this._lastPipelineMessage();
          if (msg) msg.pipelineIssues = event.issues || [];
        }
        if (event.event === 'pipeline_start') {
          this.statusMsg = `Running pipeline (${event.node_count} nodes)...`;
        }
        if (event.event === 'node_start' || event.event === 'node_done' || event.event === 'node_error') {
          const msg = this._lastPipelineMessage();
          if (msg) {
            msg.nodeStatus = {...msg.nodeStatus,
              [event.node_id]: event.event.replace('node_', '')};
          }
          if (event.event === 'node_error') this.statusMsg = `Node ${event.node_id} failed`;
        }
        if (event.event === 'node_previews') {
          const msg = this._lastPipelineMessage();
          if (msg) msg.nodePreviews = {...(msg.nodePreviews || {}), ...event.previews};
        }
        if (event.event === 'pipeline_error') {
          const msg = this._lastPipelineMessage();
          if (msg) msg.pipelineError = {node: event.failed_node, message: event.message};
        }
        if (event.event === 'chat_turn') {
          this.running = false;
          this.statusMsg = '';
        }
        if (event.event === 'executing') {
          this.statusMsg = event.message;
        }
        if (event.event === 'exec_result') {
          for (let i = this.messages.length - 1; i >= 0; i--) {
            if (this.messages[i].role === 'assistant' && this.messages[i].response_type === 'code') {
              this.messages[i].was_executed = true;
              this.messages[i].execution_result = {
                file_exists: event.file_exists,
                timed_out: event.timed_out,
                time_taken_seconds: event.time_taken,
                stderr: event.stderr,
              };
              break;
            }
          }
        }
        if (event.event === 'output_saved') {
          const output = {
            image_id: event.image_id,
            description: event.description,
            preview_url: event.preview_url,
            produced_at: new Date().toISOString(),
          };
          const slot = event.image_slot || 0;
          if (!this.outputsBySlot[slot]) this.outputsBySlot[slot] = [];
          this.outputsBySlot[slot].push(output);
          this.outputsBySlot = {...this.outputsBySlot};

          // Attach to the producing chat turn (last assistant code/pipeline
          // message) so its card can offer "Preview Output" immediately.
          for (let i = this.messages.length - 1; i >= 0; i--) {
            const rt = this.messages[i].response_type;
            if (this.messages[i].role === 'assistant' && (rt === 'code' || rt === 'pipeline')) {
              if (!this.messages[i].outputs) this.messages[i].outputs = [];
              this.messages[i].outputs.push(output);
              break;
            }
          }
        }
        if (event.event === 'stopped') {
          this.messages.push({role: 'assistant', response_type: 'chat', content: event.message || '⏹ Stopped by user.'});
          this.running = false;
          this.statusMsg = '';
        }
        if (event.event === 'error') {
          this.messages.push({role: 'assistant', response_type: 'error', content: `✗ ${event.message}`});
          this.running = false;
          this.statusMsg = '';
        }

        this._scrollChat();
      };

      evtSource.onerror = () => {
        evtSource.close();
        this.running = false;
        this.statusMsg = '';
      };
    },

    _lastPipelineMessage() {
      for (let i = this.messages.length - 1; i >= 0; i--) {
        if (this.messages[i].role === 'assistant' && this.messages[i].response_type === 'pipeline') {
          return this.messages[i];
        }
      }
      return null;
    },

    pipelineNodeLabel(node) {
      if (node.op) return node.op;
      if (node.custom && node.custom.name) return `custom:${node.custom.name}`;
      if (node.custom_node_id) {
        const cn = (this.customNodes || []).find(c => c.custom_node_id === node.custom_node_id);
        return `custom:${cn ? cn.name : node.custom_node_id.slice(0, 6)}`;
      }
      return node.id;
    },

    async savePipelineToLibrary(msg) {
      if (!msg.pipeline || msg.savingPipeline) return;
      msg.savingPipeline = true;
      try {
        const title = prompt('Save pipeline as:', 'LLM pipeline');
        if (!title) return;
        const res = await fetch('/api/pipelines', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({title, graph: msg.pipeline}),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Save failed');
        msg.savedPipelineId = data.pipeline_id;
        return data.pipeline_id;
      } catch (e) {
        alert('Could not save pipeline: ' + e.message);
      } finally {
        msg.savingPipeline = false;
      }
    },

    async openPipelineInEditor(msg) {
      // Editor works on saved pipelines, so save first if needed, then jump.
      const id = msg.savedPipelineId || await this.savePipelineToLibrary(msg);
      if (id) window.location.href = `/pipelines/${id}`;
    },

    previewOutputs(msg) {
      const outs = msg.outputs || [];
      if (outs.length === 0) return;
      const slides = outs.map((o, i) => ({
        url: o.preview_url,
        label: outs.length > 1 ? `Output ${i + 1}` : (o.description || 'Output'),
        code: msg.code || null,
      }));
      this.$dispatch('view-carousel', {slides, startIndex: 0});
    },

    _scrollChat() {
      this.$nextTick(() => {
        const el = this.$refs.chatHistory;
        if (el) el.scrollTop = el.scrollHeight;
      });
    },

    // ---------------------------------------------------------------------
    // Code editor
    // ---------------------------------------------------------------------

    async _initCodeMirror() {
      // CodeMirror 6 loaded via ESM CDN.
      // NOTE: "codemirror" must be pinned to 6.0.x — npm has a rogue 6.65.7
      // release containing CodeMirror 5 code, so "@6" resolves to a package
      // with no EditorView export. Also no ?bundle: bundling duplicates
      // @codemirror/state per module, which breaks cross-module extensions.
      try {
        const { EditorView, basicSetup } = await import('https://esm.sh/codemirror@6.0.2');
        const { python } = await import('https://esm.sh/@codemirror/lang-python@6');
        let theme;
        try {
          ({ dracula: theme } = await import('https://esm.sh/@uiw/codemirror-theme-dracula@4'));
        } catch (e) {
          console.warn('Dracula theme load failed, falling back to oneDark:', e);
          ({ oneDark: theme } = await import('https://esm.sh/@codemirror/theme-one-dark@6'));
        }

        const container = this.$refs.editorContainer;
        if (!container) return;

        this._cmView = new EditorView({
          doc: this.sess.current_code || CODE_STUB,
          extensions: [basicSetup, python(), theme],
          parent: container,
        });
      } catch (e) {
        console.warn('CodeMirror CDN load failed, using textarea fallback:', e);
        const container = this.$refs.editorContainer;
        if (container) {
          container.innerHTML = `<textarea
            id="fallback-editor-${this.sess.session_id}"
            class="w-full h-full bg-gray-900 text-green-300 font-mono text-xs p-3 border-0 outline-none resize-none"
            spellcheck="false"
          >${this.sess.current_code || CODE_STUB}</textarea>`;
        }
      }
    },

    _getCode() {
      if (this._cmView) return this._cmView.state.doc.toString();
      const ta = document.getElementById(`fallback-editor-${this.sess.session_id}`);
      return ta ? ta.value : CODE_STUB;
    },

    async runCode() {
      const code = this._getCode().trim();
      if (!code || this.running) return;

      this.running = true;
      this.execResult = null;

      try {
        const res = await fetch(`/api/sessions/${this.sess.session_id}/run-code`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({code}),
        });
        const data = await res.json();
        this.execResult = data;

        if (data.batch_run) {
          const outRes = await fetch(`/api/sessions/${this.sess.session_id}/outputs`);
          if (outRes.ok) this._buildOutputsBySlot(await outRes.json());
        }
      } catch (e) {
        this.execResult = {stderr: e.message, file_exists: false, timed_out: false, time_taken_seconds: 0};
      } finally {
        this.running = false;
      }
    },

    // ---------------------------------------------------------------------
    // Shared
    // ---------------------------------------------------------------------

    _buildOutputsBySlot(allOutputs) {
      const newOutputsBySlot = {};
      (this.sess.images || []).forEach((img, idx) => { newOutputsBySlot[idx] = []; });
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

    async addImage(file) {
      if (!file || this.addingImage) return;
      this.addingImage = true;
      try {
        const formData = new FormData();
        formData.append('file', file);
        const upRes = await fetch('/api/upload', {method: 'POST', body: formData});
        if (!upRes.ok) throw new Error((await upRes.json()).error || 'Upload failed');
        const upData = await upRes.json();

        const attachRes = await fetch(`/api/sessions/${this.sess.session_id}/images`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({image_id: upData.image_id}),
        });
        if (!attachRes.ok) throw new Error((await attachRes.json()).error || 'Attach failed');

        this.sess.images.push({image_id: upData.image_id, metadata: upData.metadata, preview_url: upData.preview_url});
        this.sess.image_ids.push(upData.image_id);
        this.outputsBySlot[this.sess.images.length - 1] = [];
      } catch (e) {
        alert('Add image failed: ' + e.message);
      } finally {
        this.addingImage = false;
      }
    },
  };
}
