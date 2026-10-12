import { copyText, el } from './api.js';

/* global marked, DOMPurify, hljs */

marked.setOptions({ gfm: true, breaks: false });

DOMPurify.addHook('afterSanitizeAttributes', node => {
    if (node.tagName === 'A') {
        node.setAttribute('target', '_blank');
        node.setAttribute('rel', 'noopener noreferrer');
    }
});

// Code-block languages that can be opened straight into the canvas.
const CANVAS_TYPES = { sql: 'sql', html: 'html', markdown: 'markdown', md: 'markdown', csv: 'csv', json: 'chart', 'vega-lite': 'chart' };

/**
 * Renders markdown into `target`. `onOpenInCanvas(type, content, language)`
 * adds an "Open in canvas" button to code blocks.
 */
export function renderMarkdown(target, text, { onOpenInCanvas } = {}) {
    target.innerHTML = DOMPurify.sanitize(marked.parse(text || ''));
    target.querySelectorAll('pre > code').forEach(code => {
        const lang = (code.className.match(/language-([\w-]+)/) || [])[1] || '';
        if (lang && hljs.getLanguage(lang)) {
            try { hljs.highlightElement(code); } catch { /* leave plain */ }
        }
        const pre = code.parentElement;
        const bar = el('div', { class: 'code-bar' },
            el('span', { class: 'code-lang' }, lang || 'text'),
            el('button', { class: 'btn tiny', onclick: e => copyText(code.textContent, e.target) }, 'Copy'),
        );
        const canvasType = CANVAS_TYPES[lang.toLowerCase()] || (lang ? 'code' : null);
        if (onOpenInCanvas && canvasType) {
            bar.append(el('button', {
                class: 'btn tiny',
                onclick: () => onOpenInCanvas(canvasType, code.textContent, lang),
            }, 'Open in canvas'));
        }
        pre.before(bar);
        bar.after(pre);
        const wrap = el('div', { class: 'code-block' });
        bar.before(wrap);
        wrap.append(bar, pre);
    });
}

export function highlight(code, language) {
    const lang = language && hljs.getLanguage(language) ? language : null;
    try {
        return lang ? hljs.highlight(code, { language: lang }).value : hljs.highlightAuto(code).value;
    } catch {
        return code.replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
    }
}

export function escapeHtml(text) {
    const d = document.createElement('div');
    d.textContent = text ?? '';
    return d.innerHTML;
}
