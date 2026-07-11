/**
 * windowManager.js — Alpine.js component managing the window tab bar.
 *
 * Shared state: windows array, activeWindowId.
 * Listens for fe:open-window events from uploader.js.
 */

function windowManager() {
  return {
    windows: [],         // [{window_id, mode, image_id, metadata, label, previewUrl}]
    activeWindowId: null,
    loading: false,
    error: null,

    get activeWindow() {
      return this.windows.find(w => w.window_id === this.activeWindowId) || null;
    },

    async openWindowForImage(imageIds, mode, metadata, previewUrl, images) {
      try {
        const res = await fetch('/api/windows', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          // For backward compat, pass the first image as image_id too
          body: JSON.stringify({image_ids: imageIds, image_id: imageIds[0], mode}),
        });
        if (!res.ok) throw new Error(await res.text());
        const data = await res.json();

        const win = {
          window_id: data.window_id,
          mode,
          image_id: imageIds[0],
          image_ids: imageIds,
          images: images || [], // Store the full list
          metadata,
          previewUrl,
          label: mode === 'llm' ? (imageIds.length > 1 ? 'Batch LLM Chat' : 'LLM Chat') : 'Code Editor',
        };
        this.windows.push(win);
        this.activeWindowId = win.window_id;
      } catch (e) {
        console.error('Failed to create window:', e);
      }
    },

    activateWindow(windowId) {
      this.activeWindowId = windowId;
    },

    async closeWindow(windowId) {
      try {
        await fetch(`/api/windows/${windowId}`, {method: 'DELETE'});
      } catch (e) {
        console.warn('Delete window error (non-fatal):', e);
      }
      this.windows = this.windows.filter(w => w.window_id !== windowId);
      if (this.activeWindowId === windowId) {
        this.activeWindowId = this.windows.length > 0
          ? this.windows[this.windows.length - 1].window_id
          : null;
      }
    },

    async init() {
      // Listen for window-open requests from the uploader
      window.addEventListener('fe:open-window', async (e) => {
        const {imageIds, mode, metadata, previewUrl, images} = e.detail;
        await this.openWindowForImage(imageIds, mode, metadata, previewUrl, images);
      });

      // If we are in the shared view, load that specific window
      if (window.SHARED_WINDOW_ID) {
        this.loading = true;
        try {
          const res = await fetch(`/api/windows/${window.SHARED_WINDOW_ID}/history`);
          if (!res.ok) throw new Error("Failed to load shared window");
          const data = await res.json();
          this.windows.push({
            window_id: window.SHARED_WINDOW_ID,
            mode: data.mode,
            image_id: data.image_id,
            image_ids: data.image_ids || [data.image_id],
            images: data.images || [],
            metadata: data.metadata,
            previewUrl: data.preview_url,
            label: data.mode === 'llm' ? 'LLM Chat' : 'Code Editor',
          });
          this.activeWindowId = window.SHARED_WINDOW_ID;
        } catch (e) {
          this.error = e.message;
        } finally {
          this.loading = false;
        }
      } else {
        // Fetch all active windows on init for standard view
        try {
          const res = await fetch('/api/windows');
          if (res.ok) {
            const list = await res.json();
            for (const item of list) {
              // We just push the summary. The chatPane/codeEditor init will fetch the full history/metadata!
              // Wait, the chatPane needs `win.previewUrl` and `win.metadata` synchronously to render correctly!
              // Let's fetch history for each to hydrate them fully
              const histRes = await fetch(`/api/windows/${item.window_id}/history`);
              if (histRes.ok) {
                const data = await histRes.json();
                this.windows.push({
                  window_id: item.window_id,
                  mode: data.mode,
                  image_id: data.image_id,
                  image_ids: data.image_ids || [data.image_id],
                  images: data.images || [],
                  metadata: data.metadata,
                  previewUrl: data.preview_url,
                  label: data.mode === 'llm' ? 'LLM Chat' : 'Code Editor',
                });
              }
            }
            if (this.windows.length > 0) {
              this.activeWindowId = this.windows[0].window_id;
            }
          }
        } catch (e) {
          console.error("Failed to load windows", e);
        }
      }
    }
  };
}
