/**
 * newSessionDialog.js — Alpine.js component for the "+ New Session" modal.
 *
 * Uploads one or more images, then dispatches fe:create-session so
 * sessionManager.js creates the session and navigates into it.
 */

function newSessionDialog() {
  return {
    dragging: false,
    uploading: false,
    progress: 0,
    images: [],   // [{image_id, metadata, preview_url}]
    error: null,
    urlInput: '',
    titleInput: '',

    async uploadFiles(files) {
      this.error = null;
      this.uploading = true;
      this.progress = 10;

      const progInterval = setInterval(() => {
        if (this.progress < 80) this.progress += 10;
      }, 200);

      try {
        for (const file of files) {
          const formData = new FormData();
          formData.append('file', file);
          const res = await fetch('/api/upload', {method: 'POST', body: formData});
          if (!res.ok) throw new Error((await res.json()).error || 'Upload failed');
          this.images.unshift(await res.json());
        }
      } catch (e) {
        this.error = e.message;
      } finally {
        clearInterval(progInterval);
        this.progress = 100;
        setTimeout(() => { this.uploading = false; this.progress = 0; }, 400);
      }
    },

    async uploadFromUrl() {
      const url = this.urlInput.trim();
      if (!url) return;
      this.error = null;
      this.uploading = true;
      this.progress = 30;

      try {
        const res = await fetch('/api/upload', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({url}),
        });
        this.progress = 100;
        if (!res.ok) throw new Error((await res.json()).error || 'Upload failed');
        this.images.unshift(await res.json());
        this.urlInput = '';
      } catch (e) {
        this.error = e.message;
      } finally {
        setTimeout(() => { this.uploading = false; this.progress = 0; }, 400);
      }
    },

    removeImage(index) {
      this.images.splice(index, 1);
    },

    handleFileSelect(event) {
      const files = Array.from(event.target.files);
      if (files.length > 0) this.uploadFiles(files);
      event.target.value = '';
    },

    handleDrop(event) {
      this.dragging = false;
      const files = Array.from(event.dataTransfer.files);
      if (files.length > 0) this.uploadFiles(files);
    },

    create() {
      if (this.images.length === 0) return;
      const imageIds = this.images.map(img => img.image_id);
      globalThis.dispatchEvent(new CustomEvent('fe:create-session', {
        detail: {imageIds, images: this.images, title: this.titleInput.trim()},
      }));
      this.images = [];
      this.titleInput = '';
      this.$store.newSessionDialog.close();
    },
  };
}
