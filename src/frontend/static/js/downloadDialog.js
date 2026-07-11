/**
 * downloadDialog.js — Alpine.js component for the download dialog.
 *
 * Collects selected image_ids and posts to POST /api/download,
 * triggering a browser file download of the returned zip.
 */

function downloadDialog() {
  return {
    selected: [],
    downloading: false,

    toggleAll() {
      if (this.selected.length === this.allOutputs.length) {
        this.selected = [];
      } else {
        this.selected = this.allOutputs.map(o => o.image_id);
      }
    },

    async downloadZip() {
      if (this.selected.length === 0 || this.downloading) return;
      this.downloading = true;

      try {
        const res = await fetch('/api/download', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({image_ids: this.selected}),
        });

        if (!res.ok) {
          const data = await res.json();
          alert(`Download failed: ${data.error || 'Unknown error'}`);
          return;
        }

        // Trigger browser download
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'feature_explorer_outputs.zip';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);

        this.showDownload = false;
      } catch (e) {
        alert(`Download error: ${e.message}`);
      } finally {
        this.downloading = false;
      }
    },
  };
}
