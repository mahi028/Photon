/**
 * shareDialog.js — Alpine.js component for the share-as-clone dialog (D-019).
 *
 * Opening the generated link does NOT grant access to this session — it
 * deep-clones it into the visitor's own account. User picks one-time or
 * reusable before generating a link.
 */

function shareDialog() {
  return {
    loading: false,
    error: null,
    shareUrl: '',
    reusable: false,
    copied: false,

    init() {
      window.addEventListener('fe:share-opened', () => {
        this.shareUrl = '';
        this.error = null;
        this.reusable = false;
        this.copied = false;
      });
    },

    async createLink() {
      const sessionId = this.$store.shareDialog.sessionId;
      if (!sessionId) return;
      this.loading = true;
      this.error = null;

      try {
        const res = await fetch(`/api/sessions/${sessionId}/share`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({reusable: this.reusable}),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Failed to create share link');
        this.shareUrl = window.location.origin + data.url;
      } catch (err) {
        this.error = err.message;
      } finally {
        this.loading = false;
      }
    },

    async revokeAll() {
      const sessionId = this.$store.shareDialog.sessionId;
      if (!sessionId) return;
      this.loading = true;
      try {
        const res = await fetch(`/api/sessions/${sessionId}/share`, {method: 'DELETE'});
        if (!res.ok) throw new Error((await res.json()).error || 'Failed to revoke');
        this.shareUrl = '';
      } catch (err) {
        this.error = err.message;
      } finally {
        this.loading = false;
      }
    },

    copyLink() {
      if (!this.shareUrl) return;
      copyTextToClipboard(this.shareUrl).then((ok) => {
        if (!ok) return;
        this.copied = true;
        setTimeout(() => { this.copied = false; }, 2000);
      });
    },
  };
}
