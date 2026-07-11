/**
 * imageViewer.js — Alpine.js component for side-by-side image compare + filmstrip.
 *
 * On image click, dispatches a 'view-carousel' event with a slides array and
 * start index, which the root app() component handles in a fullscreen carousel modal.
 */

function imageViewerSlot(slotIdx) {
  return {
    slotIdx,
    activeOutput: null,

    get imgData() {
      return this.window.images && this.window.images[this.slotIdx];
    },

    get originalUrl() {
      return this.imgData ? this.imgData.preview_url : null;
    },

    get isComposite() {
      return this.imgData && this.imgData.metadata && this.imgData.metadata.channel_count > 4;
    },

    get slotOutputs() {
      return this.outputsBySlot ? (this.outputsBySlot[this.slotIdx] || []) : [];
    },

    setActiveOutput(output) {
      this.activeOutput = output;
    },

    // Build the slides array: [{url, label, code}]
    get slides() {
      const s = [];
      if (this.originalUrl) {
        s.push({ url: this.originalUrl, label: 'Original', code: null });
      }
      this.slotOutputs.forEach((out, i) => {
        s.push({ url: out.preview_url, label: `v${i + 1}`, code: out.code || null });
      });
      return s;
    },

    // Returns the carousel index (0-based) for a given output
    carouselIndexOf(output) {
      if (!output) return 0;
      const idx = this.slotOutputs.findIndex(o => o.image_id === output.image_id);
      return idx === -1 ? 0 : idx + 1; // +1 because slide 0 is original
    },

    // Open the carousel modal at a given slide index
    openCarousel(startIndex) {
      if (this.slides.length === 0) return;
      this.$dispatch('view-carousel', { slides: this.slides, startIndex: startIndex || 0 });
    },

    init() {
      this.$watch('outputsBySlot', (newMap) => {
        const slotOuts = newMap[this.slotIdx] || [];
        if (slotOuts.length > 0) {
          this.activeOutput = slotOuts[slotOuts.length - 1];
        }
      });

      this.$nextTick(() => {
        const slotOuts = this.slotOutputs;
        if (slotOuts.length > 0) {
          this.activeOutput = slotOuts[slotOuts.length - 1];
        }
      });
    },
  };
}
