// Every path is relative so it resolves under the Workbench proxy prefix
// (/app/<uuid>/proxy/8080/). Never add a leading slash.

export class ApiError extends Error {
    constructor(status, detail) {
        super(typeof detail === 'string' ? detail : JSON.stringify(detail));
        this.status = status;
        this.detail = detail;
    }
}

export async function api(path, { method = 'GET', body } = {}) {
    const res = await fetch(path, {
        method,
        headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
        body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    const text = await res.text();
    let data = text;
    try { data = text ? JSON.parse(text) : null; } catch { /* plain text */ }
    if (!res.ok) throw new ApiError(res.status, data && data.detail !== undefined ? data.detail : data);
    return data;
}

export function wsUrl(path) {
    const url = new URL(path, location.href);
    url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return url.toString();
}

export function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
        if (v === undefined || v === null || v === false) continue;
        if (k === 'class') node.className = v;
        else if (k === 'text') node.textContent = v;
        else if (k === 'html') node.innerHTML = v;
        else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
        else if (k === 'dataset') Object.assign(node.dataset, v);
        else node.setAttribute(k, v === true ? '' : v);
    }
    for (const child of children.flat()) {
        if (child === undefined || child === null || child === false) continue;
        node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
}

export function download(filename, text, type = 'text/plain') {
    const url = URL.createObjectURL(new Blob([text], { type }));
    const a = el('a', { href: url, download: filename });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export async function copyText(text, button) {
    try {
        await navigator.clipboard.writeText(text);
    } catch {
        const ta = el('textarea', {}, text);
        document.body.append(ta);
        ta.select();
        document.execCommand('copy');
        ta.remove();
    }
    if (button) {
        const old = button.textContent;
        button.textContent = 'Copied';
        setTimeout(() => { button.textContent = old; }, 1200);
    }
}

export function toast(message, kind = 'info', ms = 4000) {
    let host = document.getElementById('toasts');
    if (!host) {
        host = el('div', { id: 'toasts' });
        document.body.append(host);
    }
    const t = el('div', { class: `toast ${kind}` }, message);
    host.append(t);
    setTimeout(() => t.remove(), ms);
}

export function formatNumber(n) {
    if (n === null || n === undefined) return '';
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return String(n);
}

export function formatBytes(n) {
    if (!n) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
    return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
}

export function formatDate(value) {
    const d = new Date(value);
    return isNaN(d) ? '' : d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

const loading = {};
// Loads a vendored script once; heavy chart libraries are only fetched when needed.
export function loadScript(src) {
    if (!loading[src]) {
        loading[src] = new Promise((resolve, reject) => {
            const s = el('script', { src });
            s.onload = resolve;
            s.onerror = () => reject(new Error(`Failed to load ${src}`));
            document.head.append(s);
        });
    }
    return loading[src];
}

// Simple modal returning a promise; used for rename / save dialogs.
export function prompt(title, { value = '', label = '', options = null, okText = 'OK' } = {}) {
    return new Promise(resolve => {
        const input = options
            ? el('select', { class: 'input' }, options.map(o => el('option', { value: o.value }, o.label)))
            : el('input', { class: 'input', value });
        if (options && value) input.value = value;
        const close = result => { overlay.remove(); resolve(result); };
        const overlay = el('div', { class: 'modal-overlay', onclick: e => { if (e.target === overlay) close(null); } },
            el('div', { class: 'modal' },
                el('h3', {}, title),
                label ? el('label', { class: 'muted' }, label) : null,
                input,
                el('div', { class: 'modal-actions' },
                    el('button', { class: 'btn', onclick: () => close(null) }, 'Cancel'),
                    el('button', { class: 'btn primary', onclick: () => close(input.value) }, okText),
                ),
            ),
        );
        input.addEventListener('keydown', e => {
            if (e.key === 'Enter') close(input.value);
            if (e.key === 'Escape') close(null);
        });
        document.body.append(overlay);
        input.focus();
        if (input.select) input.select();
    });
}
