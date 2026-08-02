function codeDialogViewer() {
  return {
    copied: false,
    
    copyCode() {
      const code = this.$store.codeDialog.code;
      if (!code) return;
      
      copyTextToClipboard(code).then((ok) => {
        if (!ok) return;
        this.copied = true;
        setTimeout(() => { this.copied = false; }, 2000);
      });
    }
  };
}
