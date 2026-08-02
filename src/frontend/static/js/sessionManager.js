/**
 * sessionManager.js — Alpine.js component managing the session sidebar +
 * active session routing (replaces windowManager.js — Phase 6, D-021).
 *
 * A session has no locked mode — sessionPane.js renders both chat and code
 * UI for whichever session is active, toggled client-side only.
 */

/**
 * Shared Alpine state mixin for the resizable right panel (chat / code editor).
 * Drag the panel's left edge to resize; width persists in localStorage.
 * Mirrors the left sidebar's drag behavior in index.html.
 */
function panelResizeState() {
  // Percentage of the pane container, so the 45:55 panel:image default ratio
  // holds at any viewport size. Clamped 25–65%.
  const saved = parseFloat(localStorage.getItem('photon:panelWidthPct'));
  return {
    panelWidthPct: Number.isFinite(saved) ? Math.max(25, Math.min(65, saved)) : 45,
    panelDragging: false,

    startPanelDrag(e) {
      const handle = e.currentTarget;
      const panel = handle.parentElement;
      const container = panel.parentElement;
      const containerWidth = container.clientWidth || 1;
      const startX = e.clientX;
      const startPct = this.panelWidthPct;
      this.panelDragging = true;

      const onMouseMove = (ev) => {
        // Panel sits on the right — dragging left grows it
        const deltaPct = ((ev.clientX - startX) / containerWidth) * 100;
        this.panelWidthPct = Math.max(25, Math.min(65, startPct - deltaPct));
      };
      const onMouseUp = () => {
        document.removeEventListener('mousemove', onMouseMove);
        document.removeEventListener('mouseup', onMouseUp);
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        this.panelDragging = false;
        localStorage.setItem('photon:panelWidthPct', this.panelWidthPct.toFixed(1));
      };
      document.addEventListener('mousemove', onMouseMove);
      document.addEventListener('mouseup', onMouseUp);
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
    },
  };
}

function sessionManager() {
  return {
    sessions: [],              // summaries: [{session_id, title, status, image_id, image_count, updated_at}]
    activeSessionId: null,
    activeSessionData: null,   // hydrated: {session_id, title, image_id, image_ids, images, metadata, previewUrl, current_code}
    loading: false,
    error: null,
    editingTitle: false,
    titleDraft: '',

    async loadSessions() {
      try {
        const res = await fetch('/api/sessions');
        if (res.ok) this.sessions = await res.json();
      } catch (e) {
        console.error('Failed to load sessions:', e);
      }
    },

    async activateSession(sessionId) {
      if (!sessionId || this.activeSessionId === sessionId) return;
      this.activeSessionId = sessionId;
      history.pushState({}, '', `/session/${sessionId}`);
      await this.hydrateActiveSession();
    },

    async hydrateActiveSession() {
      if (!this.activeSessionId) {
        this.activeSessionData = null;
        return;
      }
      this.loading = true;
      // Tear the current pane down first: the workspace is rendered under
      // x-if="activeSessionData", which only reacts to truthy/falsy changes —
      // replacing one truthy object with another would NOT destroy/recreate
      // sessionPane() (Alpine has no :key outside x-for), leaving the old
      // session on screen. Nulling forces a full unmount, then the new data
      // remounts a fresh pane.
      this.activeSessionData = null;
      try {
        const [infoRes, histRes] = await Promise.all([
          fetch(`/api/sessions/${this.activeSessionId}`),
          fetch(`/api/sessions/${this.activeSessionId}/history`),
        ]);
        if (!infoRes.ok) {
          this.activeSessionData = null;
          return;
        }
        const info = await infoRes.json();
        const hist = histRes.ok ? await histRes.json() : {};
        this.activeSessionData = {
          session_id: info.session_id,
          title: info.title,
          image_id: info.image_id,
          image_ids: info.image_ids || [],
          images: hist.images || [],
          metadata: hist.metadata || null,
          previewUrl: hist.preview_url || null,
          current_code: hist.current_code || null,
        };
      } catch (e) {
        console.error('Failed to hydrate session:', e);
        this.activeSessionData = null;
      } finally {
        this.loading = false;
      }
    },

    async createSession(imageIds, images, title) {
      const res = await fetch('/api/sessions', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({image_ids: imageIds, title: title || 'Untitled'}),
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      await this.loadSessions();
      await this.activateSession(data.session_id);
    },

    deleteSession(sessionId) {
      // Opens the type-to-confirm dialog; the actual delete happens in
      // performDelete() once the user has typed the session id.
      this.$store.deleteDialog.open(sessionId);
    },

    async performDelete(sessionId) {
      try {
        await fetch(`/api/sessions/${sessionId}`, {method: 'DELETE'});
      } catch (e) {
        console.warn('Delete session error (non-fatal):', e);
      }
      this.sessions = this.sessions.filter(s => s.session_id !== sessionId);
      if (this.activeSessionId === sessionId) {
        this.activeSessionId = null;
        this.activeSessionData = null;
        history.pushState({}, '', '/');
      }
    },

    startEditTitle() {
      if (!this.activeSessionData) return;
      this.titleDraft = this.activeSessionData.title;
      this.editingTitle = true;
    },

    async saveTitle() {
      const title = this.titleDraft.trim() || 'Untitled';
      this.editingTitle = false;
      if (!this.activeSessionData || title === this.activeSessionData.title) return;
      this.activeSessionData.title = title;
      const s = this.sessions.find(s => s.session_id === this.activeSessionId);
      if (s) s.title = title;
      try {
        await fetch(`/api/sessions/${this.activeSessionId}`, {
          method: 'PATCH',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({title}),
        });
      } catch (e) {
        console.warn('Rename failed:', e);
      }
    },

    async init() {
      await this.loadSessions();

      if (window.ACTIVE_SESSION_ID) {
        this.activeSessionId = window.ACTIVE_SESSION_ID;
        await this.hydrateActiveSession();
      }

      // New session created via the upload dialog
      window.addEventListener('fe:create-session', async (e) => {
        const {imageIds, images, title} = e.detail;
        try {
          await this.createSession(imageIds, images, title);
        } catch (err) {
          this.error = err.message;
        }
      });

      window.addEventListener('popstate', () => {
        const m = window.location.pathname.match(/^\/session\/([a-f0-9]+)$/);
        this.activeSessionId = m ? m[1] : null;
        this.hydrateActiveSession();
      });
    },
  };
}
