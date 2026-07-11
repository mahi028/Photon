function codeDialogViewer() {
  return {
    copied: false,
    
    copyCode() {
      const code = this.$store.codeDialog.code;
      if (!code) return;
      
      navigator.clipboard.writeText(code).then(() => {
        this.copied = true;
        setTimeout(() => { this.copied = false; }, 2000);
      }).catch(err => {
        console.error('Failed to copy code: ', err);
      });
    }
  };
}
