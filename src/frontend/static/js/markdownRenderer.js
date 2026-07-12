/**
 * markdownRenderer.js — shared rendering engine for LLM output and code display.
 *
 * Renders GitHub-flavored markdown (marked) sanitized via DOMPurify, with:
 *   - Syntax-highlighted code fences (highlight.js, Dracula theme)
 *   - LaTeX math via KaTeX ($...$ inline, $$...$$ display)
 *   - Mermaid diagrams via ```mermaid fences
 *
 * Registers two Alpine directives:
 *   x-markdown="expr"   — render expr (markdown string) into the element
 *   x-highlight="expr"  — render expr (python source) as a highlighted code block
 *
 * All libraries are loaded from CDN in base.html; every feature degrades
 * gracefully (plain text / unhighlighted code) if a library failed to load.
 */

(function () {
  // -------------------------------------------------------------------------
  // Markdown pipeline
  // -------------------------------------------------------------------------

  /**
   * Split markdown into code segments (fenced/inline, left untouched) and
   * text segments, so math/mermaid extraction never corrupts code.
   */
  function splitByCode(md) {
    return md.split(/(```[\s\S]*?(?:```|$)|`[^`\n]*`)/g);
  }

  /**
   * Render a markdown string to sanitized HTML.
   * Mermaid and math sources are pulled out into placeholder elements and
   * re-inserted as textContent after sanitization (see enhance()).
   * Returns {html, mermaid: string[], math: {src, display}[]}.
   */
  function renderMarkdown(md) {
    if (md == null) return { html: '', mermaid: [], math: [] };
    md = String(md);

    const mermaidBlocks = [];
    const mathBlocks = [];

    // 1. Extract mermaid fences (before marked sees them)
    md = md.replace(/```mermaid\s*\n([\s\S]*?)```/g, (_, src) => {
      mermaidBlocks.push(src.trim());
      return `\n<div class="mermaid-slot" data-mm-idx="${mermaidBlocks.length - 1}"></div>\n`;
    });

    // 2. Extract math from non-code segments only
    md = splitByCode(md)
      .map((seg, i) => {
        if (i % 2 === 1) return seg; // code segment — untouched
        // Display math first ($$...$$), then inline ($...$)
        seg = seg.replace(/\$\$([\s\S]+?)\$\$/g, (_, src) => {
          mathBlocks.push({ src: src.trim(), display: true });
          return `<span class="math-slot" data-math-idx="${mathBlocks.length - 1}"></span>`;
        });
        seg = seg.replace(/(?<!\\)\$([^$\n]+?)\$/g, (_, src) => {
          mathBlocks.push({ src: src.trim(), display: false });
          return `<span class="math-slot" data-math-idx="${mathBlocks.length - 1}"></span>`;
        });
        return seg;
      })
      .join('');

    // 3. Markdown → HTML
    let html;
    if (typeof marked !== 'undefined') {
      html = marked.parse(md, { gfm: true, breaks: true });
    } else {
      const esc = document.createElement('div');
      esc.textContent = md;
      html = `<p>${esc.innerHTML.replace(/\n/g, '<br>')}</p>`;
    }

    // 4. Sanitize — LLM output is untrusted (especially in shared windows)
    if (typeof DOMPurify !== 'undefined') {
      html = DOMPurify.sanitize(html, {
        ADD_ATTR: ['data-mm-idx', 'data-math-idx'],
      });
    }

    return { html, mermaid: mermaidBlocks, math: mathBlocks };
  }

  // -------------------------------------------------------------------------
  // Post-insertion enhancement (needs live DOM nodes)
  // -------------------------------------------------------------------------

  let _mermaidReady = false;

  function ensureMermaid() {
    if (_mermaidReady || typeof mermaid === 'undefined') return _mermaidReady;
    mermaid.initialize({ startOnLoad: false, theme: 'dark', securityLevel: 'strict' });
    _mermaidReady = true;
    return true;
  }

  /** Highlight, typeset math, and render mermaid inside el. */
  function enhance(el, mermaidBlocks, mathBlocks) {
    // Code fences → highlight.js (Dracula theme via CSS)
    if (typeof hljs !== 'undefined') {
      el.querySelectorAll('pre code').forEach((block) => {
        if (!block.dataset.highlighted) hljs.highlightElement(block);
      });
    }

    // Math → KaTeX
    if (typeof katex !== 'undefined') {
      el.querySelectorAll('.math-slot').forEach((slot) => {
        const m = mathBlocks[Number(slot.dataset.mathIdx)];
        if (!m) return;
        try {
          katex.render(m.src, slot, { displayMode: m.display, throwOnError: false });
        } catch (e) {
          slot.textContent = m.display ? `$$${m.src}$$` : `$${m.src}$`;
        }
      });
    }

    // Mermaid diagrams
    const slots = el.querySelectorAll('.mermaid-slot');
    if (slots.length && ensureMermaid()) {
      const nodes = [];
      slots.forEach((slot) => {
        const src = mermaidBlocks[Number(slot.dataset.mmIdx)];
        if (src == null) return;
        slot.classList.remove('mermaid-slot');
        slot.classList.add('mermaid');
        slot.textContent = src;
        nodes.push(slot);
      });
      if (nodes.length) {
        mermaid.run({ nodes }).catch((e) => console.warn('Mermaid render failed:', e));
      }
    }
  }

  /** Public one-shot: render markdown string into element. */
  function renderMarkdownInto(el, md) {
    const { html, mermaid: mm, math } = renderMarkdown(md);
    el.innerHTML = html;
    el.classList.add('markdown-body');
    enhance(el, mm, math);
  }

  /** Public one-shot: render a code string (python) highlighted into element. */
  function renderCodeInto(el, code) {
    el.textContent = code || '';
    el.className = 'language-python hljs';
    delete el.dataset.highlighted;
    if (typeof hljs !== 'undefined' && code) {
      hljs.highlightElement(el);
    }
  }

  globalThis.renderMarkdownInto = renderMarkdownInto;
  globalThis.renderCodeInto = renderCodeInto;

  // -------------------------------------------------------------------------
  // Alpine directives
  // -------------------------------------------------------------------------

  document.addEventListener('alpine:init', () => {
    Alpine.directive('markdown', (el, { expression }, { evaluateLater, effect }) => {
      const getValue = evaluateLater(expression);
      effect(() => {
        getValue((value) => renderMarkdownInto(el, value));
      });
    });

    Alpine.directive('highlight', (el, { expression }, { evaluateLater, effect }) => {
      const getValue = evaluateLater(expression);
      effect(() => {
        getValue((value) => renderCodeInto(el, value));
      });
    });
  });
})();
