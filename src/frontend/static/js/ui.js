/**
 * ui.js — app-wide dialogs and toasts, replacing window.alert/confirm/prompt.
 *
 * Registered as Alpine.store('ui'); markup lives in partials/ui_dialogs.html
 * (included by base.html, so every page has it). Promise-based, so call
 * sites read like the natives they replace:
 *
 *   if (!(await $store.ui.confirm({title: 'Delete?', message: '…', danger: true}))) return;
 *   const name = await $store.ui.prompt({title: 'New pipeline', label: 'Name', value: 'My pipeline'});
 *   $store.ui.toast('Saved.', 'success');        // 'info' | 'success' | 'error'
 *   await $store.ui.alert({title: 'Stderr', message: text, mono: true});
 *
 * Outside an Alpine component (plain JS modules) use window.ui, same API.
 * Only one dialog is open at a time; opening another resolves the current
 * one as cancelled.
 */

document.addEventListener('alpine:init', () => {
  Alpine.store('ui', {
    dialog: null,          // {kind, title, message, label, value, placeholder, confirmText, cancelText, danger, multiline, mono, maxLength, checkbox}
    _resolve: null,
    toasts: [],
    _toastSeq: 0,

    _open(kind, opts) {
      if (this._resolve) this._finish(false);
      this.dialog = {
        kind,
        title: opts.title || '',
        message: opts.message || '',
        label: opts.label || '',
        value: opts.value ?? '',
        placeholder: opts.placeholder || '',
        confirmText: opts.confirmText || (kind === 'alert' ? 'OK' : kind === 'prompt' ? 'Save' : 'Confirm'),
        cancelText: opts.cancelText || 'Cancel',
        danger: !!opts.danger,
        multiline: !!opts.multiline,
        mono: !!opts.mono,
        required: !!opts.required,
        maxLength: opts.maxLength || 500,
        // optional {label, checked}: a prompt with a checkbox resolves to
        // {value, checked} instead of a string (null still means cancelled)
        checkbox: opts.checkbox ? {label: opts.checkbox.label || '', checked: !!opts.checkbox.checked} : null,
      };
      return new Promise(resolve => { this._resolve = resolve; });
    },

    confirm(opts) { return this._open('confirm', typeof opts === 'string' ? {message: opts} : opts); },
    prompt(opts) { return this._open('prompt', typeof opts === 'string' ? {label: opts} : opts); },
    alert(opts) { return this._open('alert', typeof opts === 'string' ? {message: opts} : opts); },

    get canSubmit() {
      const d = this.dialog;
      return !!d && !(d.kind === 'prompt' && d.required && !String(d.value).trim());
    },

    submit() {
      if (!this.canSubmit) return;
      const d = this.dialog;
      if (d.kind !== 'prompt') return this._finish(true);
      this._finish(d.checkbox ? {value: String(d.value), checked: d.checkbox.checked} : String(d.value));
    },

    cancel() {
      if (!this.dialog) return;
      this._finish(this.dialog.kind === 'prompt' ? null : false);
    },

    _finish(result) {
      const r = this._resolve;
      this._resolve = null;
      this.dialog = null;
      if (r) r(result);
    },

    toast(message, kind = 'info', ms) {
      const id = ++this._toastSeq;
      this.toasts.push({id, message: String(message), kind});
      const ttl = ms ?? (kind === 'error' ? 8000 : 4000);
      setTimeout(() => this.dismiss(id), ttl);
      return id;
    },

    dismiss(id) { this.toasts = this.toasts.filter(t => t.id !== id); },
  });

  window.ui = Alpine.store('ui');
});
