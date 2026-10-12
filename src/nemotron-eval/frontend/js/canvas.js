import { api, ApiError, copyText, download, el, formatBytes, formatNumber, loadScript, prompt, toast } from './api.js';
import { highlight, renderMarkdown } from './markdown.js';

const ICONS = { markdown: '📝', sql: '🗄️', html: '🧩', chart: '📊', code: '💻', csv: '📋' };
const EXT = { markdown: 'md', sql: 'sql', html: 'html', chart: 'json', csv: 'csv', code: 'txt' };

// Injected into HTML mini-apps: `await workbench.query(sql)` asks the parent
// page to run a SELECT through the backend. The iframe has an opaque origin,
// so it cannot call the API itself.
const BRIDGE = `<script>
(function () {
  var pending = {}, next = 1;
  window.addEventListener('message', function (e) {
    var m = e.data || {};
    if (m.type !== 'wb-query-result' || !pending[m.id]) return;
    var p = pending[m.id]; delete pending[m.id];
    m.error ? p.reject(new Error(m.error)) : p.resolve(m.result);
  });
  window.workbench = {
    query: function (sql, maxRows) {
      return new Promise(function (resolve, reject) {
        var id = next++;
        pending[id] = { resolve: resolve, reject: reject };
        parent.postMessage({ type: 'wb-query', id: id, sql: sql, maxRows: maxRows || 1000 }, '*');
      });
    }
  };
})();
</script>`;

export function parseCsv(text) {
    const rows = [];
    let row = [], field = '', quoted = false;
    for (let i = 0; i < text.length; i++) {
        const c = text[i];
        if (quoted) {
            if (c === '"' && text[i + 1] === '"') { field += '"'; i++; }
            else if (c === '"') quoted = false;
            else field += c;
        } else if (c === '"') quoted = true;
        else if (c === ',') { row.push(field); field = ''; }
        else if (c === '\n' || c === '\r') {
            if (c === '\r' && text[i + 1] === '\n') i++;
            row.push(field); rows.push(row); row = []; field = '';
        } else field += c;
    }
    if (field || row.length) { row.push(field); rows.push(row); }
    return rows;
}

function toCsv(columns, rows) {
    const esc = v => {
        const s = v === null || v === undefined ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v);
        return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    return [columns.map(esc).join(','), ...rows.map(r => r.map(esc).join(','))].join('\n');
}

export function resultTable(columns, rows, { maxRows = 500 } = {}) {
    const table = el('table', { class: 'data-table' },
        el('thead', {}, el('tr', {}, columns.map(c => el('th', { title: c.type || '' }, c.name ?? c)))),
        el('tbody', {}, rows.slice(0, maxRows).map(r => el('tr', {}, r.map(v =>
            el('td', {}, v === null || v === undefined ? el('span', { class: 'muted' }, 'null') : typeof v === 'object' ? JSON.stringify(v) : String(v))
        )))),
    );
    return el('div', { class: 'table-wrap' }, table);
}

async function renderChart(target, specText) {
    let spec;
    try {
        spec = JSON.parse(specText);
    } catch (e) {
        target.append(el('div', { class: 'error-box' }, `Chart spec is not valid JSON: ${e.message}`));
        return;
    }
    const isVega = (spec.$schema && /vega/.test(spec.$schema)) || spec.mark || spec.layer || spec.encoding || spec.vconcat || spec.hconcat;
    const box = el('div', { class: 'chart-box' });
    target.append(box);
    try {
        if (isVega) {
            await loadScript('vendor/vega.min.js');
            await loadScript('vendor/vega-lite.min.js');
            await loadScript('vendor/vega-embed.min.js');
            const dark = document.documentElement.dataset.theme !== 'light';
            /* global vegaEmbed */
            await vegaEmbed(box, { width: 'container', ...spec }, { actions: { export: true, source: false, compiled: false, editor: false }, theme: dark ? 'dark' : undefined });
        } else if (spec.data) {
            await loadScript('vendor/plotly.min.js');
            const dark = document.documentElement.dataset.theme !== 'light';
            const layout = { autosize: true, margin: { t: 40, r: 20, b: 50, l: 60 }, ...(spec.layout || {}) };
            if (dark) Object.assign(layout, { paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)', font: { color: '#ddd', ...(layout.font || {}) } });
            /* global Plotly */
            await Plotly.newPlot(box, spec.data, layout, { responsive: true, displaylogo: false });
        } else {
            throw new Error('Expected a Vega-Lite spec (with "mark") or a Plotly spec (with "data").');
        }
    } catch (e) {
        box.replaceWith(el('div', { class: 'error-box' }, `Could not render chart: ${e.message}`));
    }
}

export class Canvas {
    constructor(root, { getSessionId, ensureSession, getBuckets }) {
        this.root = root;
        this.getSessionId = getSessionId;
        this.ensureSession = ensureSession;
        this.getBuckets = getBuckets;
        this.artifacts = new Map();   // id -> latest artifact
        this.order = [];
        this.activeId = null;
        this.viewVersion = null;      // null = latest
        this.editing = false;
        this.sqlState = new Map();    // id -> {result, error, running}
        this.tabs = root.querySelector('.canvas-tabs');
        this.toolbar = root.querySelector('.canvas-toolbar');
        this.body = root.querySelector('.canvas-body');
        root.querySelector('.canvas-close').addEventListener('click', () => this.hide());
        root.querySelector('.canvas-new').addEventListener('click', () => this.newBlank());
        window.addEventListener('message', e => this.onFrameMessage(e));
        this.renderTabs();
        this.renderBody();
    }

    get visible() { return document.body.classList.contains('canvas-open'); }
    show() { document.body.classList.add('canvas-open'); }
    hide() { document.body.classList.remove('canvas-open'); }
    toggle() { this.visible ? this.hide() : this.show(); }

    async loadSession(sessionId) {
        this.artifacts.clear();
        this.order = [];
        this.activeId = null;
        if (sessionId) {
            try {
                const list = await api(`api/sessions/${encodeURIComponent(sessionId)}/artifacts`);
                list.forEach(a => this.upsert(a, { focus: false }));
                if (list.length) this.activeId = list[list.length - 1].id;
            } catch { /* new chat */ }
        }
        this.renderTabs();
        this.renderBody();
    }

    upsert(artifact, { focus = true } = {}) {
        if (!this.artifacts.has(artifact.id)) this.order.push(artifact.id);
        this.artifacts.set(artifact.id, artifact);
        if (focus) {
            this.activeId = artifact.id;
            this.viewVersion = null;
            this.editing = false;
            this.show();
        }
        this.renderTabs();
        if (focus || artifact.id === this.activeId) this.renderBody();
    }

    open(id) {
        if (!this.artifacts.has(id)) return;
        this.activeId = id;
        this.viewVersion = null;
        this.editing = false;
        this.show();
        this.renderTabs();
        this.renderBody();
    }

    async create(type, title, content, language) {
        const sessionId = await this.ensureSession();
        const art = await api('api/artifacts', { method: 'POST', body: { session_id: sessionId, type, title, content, language } });
        this.upsert(art);
        return art;
    }

    async newBlank() {
        const type = await prompt('New artifact', {
            options: Object.keys(ICONS).map(t => ({ value: t, label: `${ICONS[t]} ${t}` })), value: 'sql', okText: 'Create',
        });
        if (!type) return;
        const starter = {
            sql: 'SELECT 1 AS x',
            markdown: '# Notes\n',
            html: '<!doctype html>\n<html><body>\n<h1>Mini-app</h1>\n<pre id="out"></pre>\n<script>\nworkbench.query("SELECT 1 AS x").then(r => out.textContent = JSON.stringify(r.rows));\n</script>\n</body></html>',
            chart: JSON.stringify({ $schema: 'https://vega.github.io/schema/vega-lite/v5.json', data: { values: [{ a: 'A', b: 28 }, { a: 'B', b: 55 }] }, mark: 'bar', encoding: { x: { field: 'a', type: 'nominal' }, y: { field: 'b', type: 'quantitative' } } }, null, 2),
            code: '',
            csv: 'a,b\n1,2\n',
        }[type];
        const art = await this.create(type, `Untitled ${type}`, starter);
        this.editing = type !== 'chart' && type !== 'html' ? true : false;
        this.renderBody();
        return art;
    }

    renderTabs() {
        // Badge on the header button so a closed canvas still shows it has content.
        const btn = document.getElementById('canvas-btn');
        if (btn) btn.dataset.count = this.order.length || '';
        this.tabs.innerHTML = '';
        if (!this.order.length) {
            this.tabs.append(el('span', { class: 'muted small' }, 'No artifacts yet'));
            return;
        }
        for (const id of this.order) {
            const a = this.artifacts.get(id);
            this.tabs.append(el('button', {
                class: `canvas-tab${id === this.activeId ? ' active' : ''}`,
                title: `${a.type} · v${a.version}`,
                onclick: () => this.open(id),
            }, `${ICONS[a.type] || '📄'} ${a.title}`));
        }
    }

    async current() {
        const latest = this.artifacts.get(this.activeId);
        if (!latest || this.viewVersion === null || this.viewVersion === latest.version) return latest;
        return api(`api/artifacts/${latest.id}?version=${this.viewVersion}`);
    }

    async renderBody() {
        this.toolbar.innerHTML = '';
        this.body.innerHTML = '';
        const latest = this.artifacts.get(this.activeId);
        if (!latest) {
            this.body.append(el('div', { class: 'canvas-empty' },
                el('div', { class: 'big' }, '🎨'),
                el('p', {}, 'Artifacts the model creates — reports, SQL, charts and mini-apps — appear here.'),
                el('p', { class: 'muted small' }, 'Ask for one ("chart the age distribution") or click ＋ to start one yourself.'),
            ));
            return;
        }
        const art = await this.current();
        this.renderToolbar(art, latest);
        if (this.editing && art.type !== 'sql') return this.renderEditor(art);
        const render = {
            markdown: () => this.renderMarkdownArtifact(art),
            sql: () => this.renderSql(art, latest),
            html: () => this.renderHtml(art),
            chart: () => this.renderChartArtifact(art),
            code: () => this.renderCode(art),
            csv: () => this.renderCsv(art),
        }[art.type] || (() => this.renderCode(art));
        render();
    }

    renderToolbar(art, latest) {
        const versions = el('select', { class: 'input small', title: 'Version', onchange: e => { this.viewVersion = Number(e.target.value); this.editing = false; this.renderBody(); } },
            Array.from({ length: latest.version }, (_, i) => latest.version - i).map(v =>
                el('option', { value: v, selected: v === art.version }, v === latest.version ? `v${v} (latest)` : `v${v}`)));
        const isOld = art.version !== latest.version;
        // append() would print null children as the text "null".
        this.toolbar.append(...[
            el('span', { class: 'canvas-title', title: 'Rename', onclick: () => this.rename(latest) }, `${ICONS[art.type] || '📄'} ${art.title}`),
            versions,
            el('span', { class: 'spacer' }),
            isOld ? el('button', { class: 'btn small', onclick: () => this.restore(art) }, 'Restore this version') : null,
            art.type !== 'sql' ? el('button', { class: `btn small${this.editing ? ' active' : ''}`, onclick: () => { this.editing = !this.editing; this.renderBody(); } }, this.editing ? 'Preview' : 'Edit') : null,
            el('button', { class: 'btn small', onclick: e => copyText(art.content, e.target) }, 'Copy'),
            el('button', { class: 'btn small', onclick: () => download(this.filename(art), art.content) }, 'Download'),
            el('button', { class: 'btn small', onclick: () => this.saveToBucket(art) }, 'Save to bucket'),
            el('button', { class: 'btn small', title: 'Full screen', onclick: () => this.root.classList.toggle('fullscreen') }, '⛶'),
        ].filter(Boolean));
    }

    filename(art) {
        const slug = art.title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || art.id;
        const ext = art.type === 'code' ? ({ python: 'py', r: 'R', javascript: 'js', bash: 'sh' }[(art.language || '').toLowerCase()] || 'txt') : EXT[art.type];
        return `${slug}.${ext}`;
    }

    async rename(art) {
        const title = await prompt('Rename artifact', { value: art.title });
        if (!title || title === art.title) return;
        const updated = await api(`api/artifacts/${art.id}`, { method: 'PUT', body: { content: art.content, title } });
        this.upsert(updated);
    }

    async restore(art) {
        const updated = await api(`api/artifacts/${art.id}`, { method: 'PUT', body: { content: art.content, title: art.title } });
        this.upsert(updated);
        toast(`Restored as v${updated.version}`);
    }

    async saveContent(art, content) {
        const updated = await api(`api/artifacts/${art.id}`, { method: 'PUT', body: { content } });
        this.editing = false;
        this.upsert(updated);
        toast(`Saved v${updated.version}`);
    }

    async saveToBucket(art) {
        const buckets = await this.getBuckets();
        if (!buckets.length) return toast('No workspace buckets found (Workbench tools may still be starting).', 'warn');
        const bucket = await prompt('Save artifact to bucket', {
            label: `Saved as nemotron-eval/artifacts/${this.filename(art).replace(/\.(\w+)$/, `-v${art.version}.$1`)}`,
            options: buckets.map(b => ({ value: b.bucket, label: `${b.id} (gs://${b.bucket})` })), okText: 'Save',
        });
        if (!bucket) return;
        try {
            const { uri } = await api(`api/artifacts/${art.id}/save`, { method: 'POST', body: { bucket } });
            toast(`Saved to ${uri}`, 'ok', 6000);
        } catch (e) {
            toast(e.message, 'error', 8000);
        }
    }

    renderEditor(art) {
        const ta = el('textarea', { class: 'editor', spellcheck: 'false' }, art.content);
        this.body.append(ta, el('div', { class: 'editor-actions' },
            el('button', { class: 'btn', onclick: () => { this.editing = false; this.renderBody(); } }, 'Cancel'),
            el('button', { class: 'btn primary', onclick: () => this.saveContent(art, ta.value) }, 'Save as new version'),
        ));
        ta.addEventListener('keydown', e => {
            if ((e.metaKey || e.ctrlKey) && e.key === 's') { e.preventDefault(); this.saveContent(art, ta.value); }
            if (e.key === 'Tab') { e.preventDefault(); ta.setRangeText('  ', ta.selectionStart, ta.selectionEnd, 'end'); }
        });
        ta.focus();
    }

    renderMarkdownArtifact(art) {
        const div = el('div', { class: 'markdown canvas-doc' });
        renderMarkdown(div, art.content);
        this.body.append(div);
    }

    renderCode(art) {
        const code = el('code', { html: highlight(art.content, art.language) });
        this.body.append(el('pre', { class: 'canvas-code hljs' }, code));
    }

    renderCsv(art) {
        const rows = parseCsv(art.content.trim());
        if (!rows.length) return this.body.append(el('p', { class: 'muted' }, 'Empty CSV'));
        this.body.append(
            el('div', { class: 'muted small' }, `${formatNumber(rows.length - 1)} rows × ${rows[0].length} columns`),
            resultTable(rows[0].map(name => ({ name })), rows.slice(1), { maxRows: 2000 }),
        );
    }

    renderChartArtifact(art) {
        renderChart(this.body, art.content);
    }

    renderHtml(art) {
        const doc = /<head[^>]*>/i.test(art.content)
            ? art.content.replace(/<head[^>]*>/i, m => m + BRIDGE)
            : BRIDGE + art.content;
        // No allow-same-origin: the app can't read the parent page, cookies or API.
        const frame = el('iframe', {
            class: 'miniapp', sandbox: 'allow-scripts allow-forms allow-modals allow-popups allow-downloads',
            title: art.title,
        });
        frame.srcdoc = doc;
        this.body.append(frame);
        this.frame = frame;
    }

    async onFrameMessage(e) {
        const m = e.data || {};
        if (m.type !== 'wb-query' || !this.frame || e.source !== this.frame.contentWindow) return;
        const reply = payload => e.source.postMessage({ type: 'wb-query-result', id: m.id, ...payload }, '*');
        try {
            const result = await api('api/sql/query', { method: 'POST', body: { sql: String(m.sql), max_rows: Number(m.maxRows) || 1000 } });
            reply({ result });
        } catch (err) {
            reply({ error: err.message });
        }
    }

    renderSql(art, latest) {
        const state = this.sqlState.get(art.id) || {};
        const editor = el('textarea', { class: 'editor sql-editor', spellcheck: 'false' }, art.content);
        const maxRows = el('input', { class: 'input small', type: 'number', min: 1, max: 5000, value: state.maxRows || 500, title: 'Max rows' });
        const runBtn = el('button', { class: 'btn primary', onclick: () => this.runSql(art, editor.value, Number(maxRows.value)) }, state.running ? 'Running…' : '▶ Run');
        runBtn.disabled = !!state.running;
        const dirty = () => editor.value !== art.content;
        const saveBtn = el('button', { class: 'btn', onclick: () => this.saveContent(latest, editor.value) }, 'Save version');
        editor.addEventListener('input', () => { saveBtn.disabled = !dirty(); });
        saveBtn.disabled = true;
        editor.addEventListener('keydown', e => {
            if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); runBtn.click(); }
            if ((e.metaKey || e.ctrlKey) && e.key === 's') { e.preventDefault(); if (dirty()) saveBtn.click(); }
        });
        const results = el('div', { class: 'sql-results' });
        this.body.append(editor, el('div', { class: 'sql-actions' }, runBtn, el('label', { class: 'muted small' }, 'Max rows ', maxRows), saveBtn,
            el('span', { class: 'muted small' }, 'Ctrl+Enter to run')), results);

        if (state.error) results.append(el('div', { class: 'error-box' }, state.error));
        if (state.result) {
            const r = state.result;
            results.append(
                el('div', { class: 'sql-meta' },
                    `${formatNumber(r.rows.length)} of ${formatNumber(r.total_rows)} rows · ${formatBytes(r.bytes_processed)} processed · ${(r.elapsed_ms / 1000).toFixed(1)}s`,
                    el('span', { class: 'spacer' }),
                    el('button', { class: 'btn tiny', onclick: () => download(`${this.filename(art).replace(/\.sql$/, '')}.csv`, toCsv(r.columns.map(c => c.name), r.rows), 'text/csv') }, 'Download CSV'),
                    el('button', { class: 'btn tiny', onclick: () => this.create('csv', `${art.title} results`, toCsv(r.columns.map(c => c.name), r.rows)) }, 'Results → CSV artifact'),
                    el('button', { class: 'btn tiny', onclick: () => this.quickChart(art, r) }, 'Chart it'),
                ),
                resultTable(r.columns, r.rows),
            );
        }
    }

    async runSql(art, sql, maxRows, confirmed = false) {
        const state = { running: true, maxRows };
        this.sqlState.set(art.id, state);
        if (art.id === this.activeId) this.renderBody();
        try {
            state.result = await api('api/sql/run', { method: 'POST', body: { sql, max_rows: maxRows, confirmed } });
        } catch (e) {
            if (e instanceof ApiError && e.status === 409 && e.detail?.needs_confirmation) {
                state.running = false;
                if (confirm(`This ${e.detail.statement_type} statement deletes data. Run it anyway?`)) return this.runSql(art, sql, maxRows, true);
                state.error = 'Cancelled.';
            } else {
                state.error = e.message;
            }
        }
        state.running = false;
        if (art.id === this.activeId) this.renderBody();
    }

    async quickChart(art, result) {
        const cols = result.columns.map(c => c.name);
        const numeric = result.columns.filter(c => ['INTEGER', 'INT64', 'FLOAT', 'FLOAT64', 'NUMERIC', 'BIGNUMERIC'].includes(c.type)).map(c => c.name);
        const x = await prompt('Chart: x axis / category', { options: cols.map(c => ({ value: c, label: c })), value: cols[0] });
        if (!x) return;
        const yOptions = numeric.length ? numeric : cols;
        const y = await prompt('Chart: y axis (numeric)', { options: yOptions.map(c => ({ value: c, label: c })), value: yOptions.find(c => c !== x) || yOptions[0] });
        if (!y) return;
        const values = result.rows.map(r => Object.fromEntries(cols.map((c, i) => [c, r[i]])));
        const xNumeric = numeric.includes(x);
        const spec = {
            $schema: 'https://vega.github.io/schema/vega-lite/v5.json',
            title: art.title,
            data: { values },
            mark: { type: xNumeric ? 'line' : 'bar', tooltip: true, point: xNumeric },
            encoding: {
                x: { field: x, type: xNumeric ? 'quantitative' : 'nominal', sort: xNumeric ? undefined : '-y' },
                y: { field: y, type: 'quantitative' },
            },
        };
        this.create('chart', `${art.title} chart`, JSON.stringify(spec, null, 2));
    }
}
