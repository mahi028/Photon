function shareDialog() {
  return {
    loading: false,
    error: null,
    shareUrl: '',
    copied: false,
    
    init() {
      window.addEventListener('fe:share-opened', (e) => {
        this.fetchToken(e.detail);
      });
    },

    async fetchToken(windowId) {
      if (!windowId) return;
      this.loading = true;
      this.error = null;
      this.shareUrl = '';
      this.copied = false;

      try {
        const res = await fetch(`/api/windows/${windowId}/share`, {
          method: 'POST'
        });
        const data = await res.json();
        
        if (!res.ok) {
          throw new Error(data.error || 'Failed to share window');
        }
        
        // build full URL
        const fullUrl = window.location.origin + data.url;
        this.shareUrl = fullUrl;
      } catch (err) {
        this.error = err.message;
      } finally {
        this.loading = false;
      }
    },

    async unshare() {
      const windowId = this.$store.shareDialog.windowId;
      if (!windowId) return;
      
      this.loading = true;
      try {
        const res = await fetch(`/api/windows/${windowId}/share`, {
          method: 'DELETE'
        });
        if (!res.ok) {
          const data = await res.json();
          throw new Error(data.error || 'Failed to unshare');
        }
        this.$store.shareDialog.close();
      } catch (err) {
        this.error = err.message;
      } finally {
        this.loading = false;
      }
    },
    
    copyLink() {
      if (!this.shareUrl) return;
      navigator.clipboard.writeText(this.shareUrl).then(() => {
        this.copied = true;
        setTimeout(() => { this.copied = false; }, 2000);
      });
    }
  };
}
