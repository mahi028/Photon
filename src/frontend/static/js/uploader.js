/**
 * uploader.js — Alpine.js component for the upload panel.
 *
 * Handles drag-drop, file picker, and URL upload.
 * Fires fe:open-window event when user clicks "Chat with LLM" or "Write My Own Code".
 */

function uploader() {
  return {
    dragging: false,
    uploading: false,
    progress: 0,
    images: [], // Array of { image_id, metadata, preview_url }
    error: null,
    urlInput: '',
    copied: false,

    async uploadFiles(files) {
      this.error = null;
      this.uploading = true;
      this.progress = 10;

      // Fake progress for UX
      const progInterval = setInterval(() => {
        if (this.progress < 80) this.progress += 10;
      }, 200);

      try {
        for (const file of files) {
            const formData = new FormData();
            formData.append('file', file);
            const res = await fetch('/api/upload', {method: 'POST', body: formData});
            if (!res.ok) {
              const data = await res.json();
              throw new Error(data.error || 'Upload failed');
            }
            const data = await res.json();
            this._onUploadSuccess(data);
        }
      } catch (e) {
        this.error = e.message;
      } finally {
        clearInterval(progInterval);
        this.progress = 100;
        setTimeout(() => {
            this.uploading = false;
            this.progress = 0;
        }, 500);
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

        if (!res.ok) {
          const data = await res.json();
          throw new Error(data.error || 'Upload failed');
        }
        const data = await res.json();
        this._onUploadSuccess(data);
        this.urlInput = '';
      } catch (e) {
        this.error = e.message;
      } finally {
        setTimeout(() => {
            this.uploading = false;
            this.progress = 0;
        }, 500);
      }
    },

    _onUploadSuccess(data) {
      // Add to the beginning of the list
      this.images.unshift({
        image_id: data.image_id,
        metadata: data.metadata,
        preview_url: data.preview_url,
      });
    },

    removeImage(index) {
        this.images.splice(index, 1);
    },

    handleFileSelect(event) {
      const files = Array.from(event.target.files);
      if (files.length > 0) this.uploadFiles(files);
      event.target.value = ''; // Reset input
    },

    handleDrop(event) {
      this.dragging = false;
      const files = Array.from(event.dataTransfer.files);
      if (files.length > 0) this.uploadFiles(files);
    },

    openWindow(mode) {
      if (this.images.length === 0) return;
      
      const imageIds = this.images.map(img => img.image_id);
      // Pass primary image metadata for backward compatibility, although not strictly needed 
      // if backend relies on image_ids.
      const primaryImage = this.images[0];

      globalThis.dispatchEvent(new CustomEvent('fe:open-window', {
        detail: {
          imageIds: imageIds, // Pass the array of IDs
          mode,
          metadata: primaryImage.metadata,
          previewUrl: primaryImage.preview_url,
          images: this.images // Pass the full objects for rendering in the window
        }
      }));
    },
  };
}
