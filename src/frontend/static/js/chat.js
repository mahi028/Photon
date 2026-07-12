/**
 * chat.js — Alpine.js component for LLM chat pane.
 *
 * Sends messages to POST /api/windows/<id>/message,
 * then opens an SSE stream via GET /api/windows/<id>/stream
 * to receive real-time loop progress events.
 */

function chatPane(window) {
  return {
    ...panelResizeState(),
    window,
    messages: [],       // [{role, response_type, content, code, was_executed, execution_result}]
    inputText: '',
    running: false,
    statusMsg: '',
    attemptLabel: '1/6',
    showMeta: false,
    outputsBySlot: {},  // slotIdx -> GeneratedImage[]
    // Backward compat, use slot 0
    get outputs() { return this.outputsBySlot[0] || []; },
    originalUrl: window.previewUrl || null,

    async init() {
      // Fetch history if window was restored
      try {
        const res = await fetch(`/api/windows/${this.window.window_id}/history`);
        if (res.ok) {
          const data = await res.json();
          // internal=true turns are loop plumbing (execution feedback fed back
          // to the LLM) — part of the LLM context but not shown in the chat UI
          this.messages = data.llm_conversation.filter(m => !m.internal).map(m => ({
            role: m.role,
            response_type: m.response_type,
            content: m.message,
            code: m.code,
            was_executed: m.was_executed,
            execution_result: m.execution_result,
          }));
          // Restore original image preview and metadata from history
          if (data.preview_url) this.originalUrl = data.preview_url;
          if (data.metadata) this.window.metadata = data.metadata;
          this._scrollChat();
        }
        
        // Also fetch outputs (with preview_url already computed by the server)
        const outRes = await fetch(`/api/windows/${this.window.window_id}/outputs`);
        if (outRes.ok) {
          const allOutputs = await outRes.json();
          this.outputsBySlot = {};
          // Initialize slots based on window.images
          if (this.window.images) {
             this.window.images.forEach((img, idx) => {
                 this.outputsBySlot[idx] = [];
             });
          }
          allOutputs.forEach(out => {
             // The backend doesn't save image_slot in the output model right now.
             // We can infer slot by matching source_image_id? But output model doesn't store source_image_id either, 
             // it just stores description which might have slotIdx.
             // Let's parse slot from description: "llm_output_slot{slotIdx}_{N}"
             let slot = 0;
             if (out.description && out.description.includes('slot')) {
                 const m = out.description.match(/slot(\d+)_/);
                 if (m) slot = parseInt(m[1], 10);
             }
             if (!this.outputsBySlot[slot]) this.outputsBySlot[slot] = [];
             this.outputsBySlot[slot].push(out);
          });
        }
      } catch (e) {
        console.error("Failed to load history:", e);
      }
    },

    async sendMessage() {
      const prompt = this.inputText.trim();
      if (!prompt || this.running) return;

      this.running = true;
      this.inputText = '';
      this.messages.push({role: 'user', content: prompt});
      this.statusMsg = 'Starting...';

      try {
        const res = await fetch(`/api/windows/${this.window.window_id}/message`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({prompt}),
        });
        if (!res.ok) {
          const data = await res.json();
          this.messages.push({
            role: 'assistant',
            response_type: 'error',
            content: `Error: ${data.error || 'Unknown error'}`,
          });
          this.running = false;
          return;
        }

        this._listenToStream();
      } catch (e) {
        this.messages.push({
          role: 'assistant',
          response_type: 'error',
          content: `Error: ${e.message}`,
        });
        this.running = false;
      }
    },

    async stopLoop() {
      try {
        await fetch(`/api/windows/${this.window.window_id}/stop`, {method: 'POST'});
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
        const res = await fetch(`/api/windows/${this.window.window_id}/run-code`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({code}),
        });
        const data = await res.json();
        
        if (!res.ok) {
          alert("Error running snippet: " + (data.error || "Unknown"));
        } else {
          // If it succeeded and produced an image, fetch outputs again to update strip
          if (data.image_id) {
            const outRes = await fetch(`/api/windows/${this.window.window_id}/outputs`);
            if (outRes.ok) {
              const allOutputs = await outRes.json();
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
            }
          } else {
             // Just show error
             alert("Snippet finished. Stderr:\n" + (data.stderr || "None"));
          }
        }
      } catch (e) {
        alert("Execution failed: " + e.message);
      } finally {
        this.running = false;
        this.statusMsg = '';
      }
    },

    _listenToStream() {
      const evtSource = new EventSource(`/api/windows/${this.window.window_id}/stream`);

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
            was_executed: false,
          });
        }
        
        if (event.event === 'chat_turn') {
          // Already pushed as llm_message with response_type='chat'
          this.running = false;
          this.statusMsg = '';
        }

        if (event.event === 'executing') {
          this.statusMsg = event.message;
        }

        if (event.event === 'exec_result') {
          // Find the last assistant code message and mark it executed
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
          if (!this.outputsBySlot[slot]) {
             this.outputsBySlot[slot] = [];
          }
          this.outputsBySlot[slot].push(output);
          
          // Trigger reactivity
          this.outputsBySlot = { ...this.outputsBySlot };
          
          // Notify global app component for download dialog
          globalThis.dispatchEvent(new CustomEvent('fe:new-output', {detail: output}));
        }

        if (event.event === 'stopped') {
          this.messages.push({
            role: 'assistant',
            response_type: 'chat',
            content: event.message || '⏹ Stopped by user.',
          });
          this.running = false;
          this.statusMsg = '';
        }

        if (event.event === 'error') {
           this.messages.push({
            role: 'assistant',
            response_type: 'error',
            content: `✗ ${event.message}`,
          });
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

    _scrollChat() {
      this.$nextTick(() => {
        const el = this.$refs.chatHistory;
        if (el) el.scrollTop = el.scrollHeight;
      });
    },
  };
}
