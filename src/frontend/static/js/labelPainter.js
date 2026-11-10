/**
 * labelPainter.js — paint training labels over a session image (D-044).
 *
 * The labels live in a full-resolution Uint8Array (0 = unlabeled, 1..N =
 * class). The screen shows the downscaled preview with a coloured overlay
 * canvas, also at full resolution and scaled by CSS. Strokes are stamped into
 * the array directly (no canvas antialiasing, so every pixel is exactly one
 * class). Saved as an RGBA PNG: red = class, alpha 0 = unlabeled. Pipelines
 * read it with the load_labels op.
 */

const LABEL_COLORS = ['#ef4444', '#3b82f6', '#22c55e', '#eab308', '#a855f7',
                      '#f97316', '#06b6d4', '#ec4899', '#84cc16'];

function labelPainter() {
  return {
    image: null,          // {image_id, preview_url, metadata}
    W: 0, H: 0,
    labels: null,         // Uint8Array(W*H)
    overlay: null,        // ImageData for the overlay canvas
    classes: [],
    current: 1,           // 0 = eraser
    brush: 12,            // radius in screen pixels
    opacity: 55,
    annotations: [],
    annotationId: null,
    name: 'labels',
    undoStack: [],
    painting: false,
    last: null,
    dirty: false,
    saving: false,
    loading: false,
    error: '',
    counts: {},

    init() {
      window.addEventListener('fe:labels-opened', (e) => this.start(e.detail));
    },

    defaultClasses() {
      return [{value: 1, name: 'object', color: LABEL_COLORS[0]},
              {value: 2, name: 'background', color: LABEL_COLORS[1]}];
    },

    async start(image) {
      this.image = image;
      const shape = (image.metadata && image.metadata.shape) || [];
      this.H = shape[0]; this.W = shape[1];
      this.error = '';
      this.undoStack = [];
      this.dirty = false;
      this.current = 1;
      this.annotationId = null;
      this.name = 'labels';
      this.classes = this.defaultClasses();
      this.labels = new Uint8Array(this.W * this.H);
      await this.$nextTick();
      const cv = this.$refs.overlay;
      cv.width = this.W; cv.height = this.H;
      this.overlay = cv.getContext('2d').createImageData(this.W, this.H);
      this.loading = true;
      try {
        const r = await fetch(`/api/annotations?image_id=${encodeURIComponent(image.image_id)}`);
        this.annotations = r.ok ? (await r.json()).annotations : [];
        if (this.annotations.length) await this.load(this.annotations[0]);
        else this.redraw();
      } finally {
        this.loading = false;
      }
    },

    async pick(a) {
      if (this.dirty && !(await this.ask('Discard the strokes you have not saved?'))) return;
      await this.load(a);
    },

    async load(a) {
      const img = new Image();
      img.src = `/api/annotations/${a.annotation_id}/image?t=${Date.now()}`;
      await img.decode();
      const c = document.createElement('canvas');
      c.width = this.W; c.height = this.H;
      const ctx = c.getContext('2d');
      ctx.drawImage(img, 0, 0);
      const px = ctx.getImageData(0, 0, this.W, this.H).data;
      for (let i = 0; i < this.labels.length; i++) this.labels[i] = px[i * 4];
      this.annotationId = a.annotation_id;
      this.name = a.name;
      if (a.classes && a.classes.length) this.classes = a.classes.map(c => ({...c}));
      const used = new Set(this.labels);
      for (const v of used) {
        if (v && !this.classes.find(c => c.value === v))
          this.classes.push({value: v, name: `class ${v}`, color: LABEL_COLORS[(v - 1) % 9]});
      }
      this.undoStack = [];
      this.dirty = false;
      this.redraw();
    },

    async ask(message, confirmText = 'Discard') {
      return window.ui.confirm({title: 'Unsaved labels', message, confirmText, danger: true});
    },

    async newAnnotation() {
      if (this.dirty && !(await this.ask('Discard the strokes you have not saved?'))) return;
      this.labels.fill(0);
      this.annotationId = null;
      this.name = `labels ${this.annotations.length + 1}`;
      this.undoStack = [];
      this.dirty = false;
      this.redraw();
    },

    rgb(hex) {
      return [parseInt(hex.slice(1, 3), 16), parseInt(hex.slice(3, 5), 16), parseInt(hex.slice(5, 7), 16)];
    },

    lut() {
      const t = new Array(256).fill(null);
      for (const c of this.classes) t[c.value] = this.rgb(c.color || LABEL_COLORS[(c.value - 1) % 9]);
      return t;
    },

    redraw(x0 = 0, y0 = 0, x1 = this.W, y1 = this.H) {
      if (!this.overlay) return;
      const d = this.overlay.data, lut = this.lut();
      for (let y = y0; y < y1; y++) {
        for (let x = x0; x < x1; x++) {
          const i = y * this.W + x, v = this.labels[i], o = i * 4;
          const col = v ? (lut[v] || [255, 255, 255]) : null;
          if (col) { d[o] = col[0]; d[o + 1] = col[1]; d[o + 2] = col[2]; d[o + 3] = 255; }
          else d[o + 3] = 0;
        }
      }
      this.$refs.overlay.getContext('2d').putImageData(this.overlay, 0, 0, x0, y0, x1 - x0, y1 - y0);
      this.recount();
    },

    recount() {
      const n = {};
      for (const c of this.classes) n[c.value] = 0;
      // Sample for big images; exact counts come back from the server on save.
      const step = this.labels.length > 4e6 ? 7 : 1;
      for (let i = 0; i < this.labels.length; i += step) if (this.labels[i]) n[this.labels[i]] = (n[this.labels[i]] || 0) + step;
      this.counts = n;
    },

    scale() {
      const r = this.$refs.overlay.getBoundingClientRect();
      return this.W / r.width;
    },

    toImage(ev) {
      const r = this.$refs.overlay.getBoundingClientRect();
      return [(ev.clientX - r.left) * this.W / r.width, (ev.clientY - r.top) * this.H / r.height];
    },

    stamp(cx, cy, rad) {
      const x0 = Math.max(0, Math.floor(cx - rad)), x1 = Math.min(this.W, Math.ceil(cx + rad) + 1);
      const y0 = Math.max(0, Math.floor(cy - rad)), y1 = Math.min(this.H, Math.ceil(cy + rad) + 1);
      const r2 = rad * rad, v = this.current;
      for (let y = y0; y < y1; y++) {
        const dy = y - cy;
        for (let x = x0; x < x1; x++) {
          const dx = x - cx;
          if (dx * dx + dy * dy <= r2) this.labels[y * this.W + x] = v;
        }
      }
      return [x0, y0, x1, y1];
    },

    down(ev) {
      if (ev.button !== 0) return;
      ev.target.setPointerCapture(ev.pointerId);
      this.undoStack.push(this.labels.slice());
      if (this.undoStack.length > (this.labels.length > 4e6 ? 3 : 10)) this.undoStack.shift();
      this.painting = true;
      this.last = this.toImage(ev);
      this.paintTo(this.last);
    },

    move(ev) {
      if (!this.painting) return;
      this.paintTo(this.toImage(ev));
    },

    up() {
      this.painting = false;
      this.last = null;
    },

    paintTo([x, y]) {
      const rad = Math.max(0.5, this.brush * this.scale());
      const [lx, ly] = this.last || [x, y];
      const dist = Math.hypot(x - lx, y - ly);
      const steps = Math.max(1, Math.ceil(dist / Math.max(1, rad / 2)));
      let bx0 = this.W, by0 = this.H, bx1 = 0, by1 = 0;
      for (let s = 1; s <= steps; s++) {
        const t = s / steps;
        const [a, b, c, d] = this.stamp(lx + (x - lx) * t, ly + (y - ly) * t, rad);
        bx0 = Math.min(bx0, a); by0 = Math.min(by0, b); bx1 = Math.max(bx1, c); by1 = Math.max(by1, d);
      }
      this.last = [x, y];
      this.dirty = true;
      if (bx1 > bx0 && by1 > by0) this.redraw(bx0, by0, bx1, by1);
    },

    undo() {
      if (!this.undoStack.length) return;
      this.labels = this.undoStack.pop();
      this.dirty = true;
      this.redraw();
    },

    clear() {
      this.undoStack.push(this.labels.slice());
      this.labels.fill(0);
      this.dirty = true;
      this.redraw();
    },

    addClass() {
      const used = new Set(this.classes.map(c => c.value));
      let v = 1;
      while (used.has(v) && v <= 9) v++;
      if (v > 9) return;
      this.classes.push({value: v, name: `class ${v}`, color: LABEL_COLORS[(v - 1) % 9]});
      this.classes.sort((a, b) => a.value - b.value);
      this.current = v;
    },

    async removeClass(c) {
      if (this.counts[c.value] && !(await this.ask(`Erase all "${c.name}" strokes?`, 'Erase'))) return;
      this.undoStack.push(this.labels.slice());
      for (let i = 0; i < this.labels.length; i++) if (this.labels[i] === c.value) this.labels[i] = 0;
      this.classes = this.classes.filter(x => x !== c);
      if (this.current === c.value) this.current = this.classes.length ? this.classes[0].value : 0;
      this.dirty = true;
      this.redraw();
    },

    key(ev) {
      if (ev.target.tagName === 'INPUT') return;
      if ((ev.ctrlKey || ev.metaKey) && ev.key === 'z') { ev.preventDefault(); this.undo(); return; }
      if (ev.key === 'e') this.current = 0;
      else if (/^[1-9]$/.test(ev.key) && this.classes.find(c => c.value === +ev.key)) this.current = +ev.key;
      else if (ev.key === '[') this.brush = Math.max(1, this.brush - 2);
      else if (ev.key === ']') this.brush = Math.min(80, this.brush + 2);
    },

    pngDataUrl() {
      const c = document.createElement('canvas');
      c.width = this.W; c.height = this.H;
      const ctx = c.getContext('2d');
      const im = ctx.createImageData(this.W, this.H), d = im.data;
      for (let i = 0; i < this.labels.length; i++) {
        const v = this.labels[i];
        if (v) { d[i * 4] = v; d[i * 4 + 3] = 255; }
      }
      ctx.putImageData(im, 0, 0);
      return c.toDataURL('image/png');
    },

    async save() {
      this.saving = true;
      this.error = '';
      try {
        const res = await fetch('/api/annotations', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({image_id: this.image.image_id, png: this.pngDataUrl(),
                                name: this.name, classes: this.classes,
                                annotation_id: this.annotationId}),
        });
        const data = await res.json();
        if (!res.ok) { this.error = data.error || 'Save failed'; return; }
        this.annotationId = data.annotation_id;
        this.dirty = false;
        const r = await fetch(`/api/annotations?image_id=${encodeURIComponent(this.image.image_id)}`);
        if (r.ok) this.annotations = (await r.json()).annotations;
        window.ui && window.ui.toast('Labels saved. Use a load_labels node to train on them.', 'success');
      } finally {
        this.saving = false;
      }
    },

    async remove() {
      if (!this.annotationId) return;
      if (!(await window.ui.confirm({title: 'Delete labels?', message: `"${this.name}" is deleted for good.`,
                                     confirmText: 'Delete', danger: true}))) return;
      const res = await fetch(`/api/annotations/${this.annotationId}`, {method: 'DELETE'});
      if (!res.ok) { this.error = 'Delete failed'; return; }
      this.annotations = this.annotations.filter(a => a.annotation_id !== this.annotationId);
      this.annotationId = null;
      this.labels.fill(0);
      this.dirty = false;
      this.redraw();
    },

    async close() {
      if (this.dirty && !(await this.ask('Close without saving your strokes?'))) return;
      this.$store.labelPainter.close();
    },
  };
}
