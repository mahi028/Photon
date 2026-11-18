/**
 * downloadDialog.js — Alpine.js component for the download dialog (D-020,
 * session-scoped: only the session it was opened from, via
 * POST /api/sessions/<id>/download).
 */

function downloadDialog() {
  return {
    sessionId: null,
    outputs: [],
    selected: [],
    loading: false,
    downloading: false,
    error: null,

    init() {
      window.addEventListener('fe:download-opened', (e) => this.loadOutputs(e.detail));
    },

    async loadOutputs(sessionId) {
      this.sessionId = sessionId;
      this.outputs = [];
      this.selected = [];
      this.error = null;
      this.loading = true;
      try {
        const res = await fetch(`/api/sessions/${sessionId}/outputs`);
        if (!res.ok) throw new Error('Failed to load outputs');
        this.outputs = await res.json();
      } catch (e) {
        this.error = e.message;
      } finally {
        this.loading = false;
      }
    },

    toggleAll() {
      this.selected = this.selected.length === this.outputs.length
        ? []
        : this.outputs.map(o => o.image_id);
    },

    async downloadZip() {
      if (this.selected.length === 0 || this.downloading || !this.sessionId) return;
      this.downloading = true;

      try {
        const res = await fetch(`/api/sessions/${this.sessionId}/download`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({image_ids: this.selected}),
        });

        if (!res.ok) {
          const data = await res.json();
          window.ui.toast(`Download failed: ${data.error || 'Unknown error'}`, 'error');
          return;
        }

        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `session_${this.sessionId.slice(0, 8)}_outputs.zip`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);

        this.$store.downloadDialog.close();
      } catch (e) {
        window.ui.toast(`Download error: ${e.message}`, 'error');
      } finally {
        this.downloading = false;
      }
    },
  };
}
