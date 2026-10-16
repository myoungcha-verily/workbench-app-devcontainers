import { api, download, el, formatDate, formatNumber, toast, wsUrl } from './api.js';
import { escapeHtml, renderMarkdown } from './markdown.js';

export class CompareView {
    constructor(app) {
        this.app = app;
        this.columns = document.getElementById('compare-columns');
        document.getElementById('compare-run').addEventListener('click', () => this.run());
        document.getElementById('compare-close').addEventListener('click', () => app.showView('chat'));
    }

    renderModels(models) {
        const box = document.getElementById('compare-models');
        const checked = new Set([...box.querySelectorAll('input:checked')].map(i => i.value));
        box.innerHTML = '';
        models.filter(m => m.can_run).forEach(m => box.append(el('label', { class: 'checkbox-item' },
            el('input', { type: 'checkbox', value: m.model_id, disabled: m.status !== 'ready', checked: m.status === 'ready' && (checked.size ? checked.has(m.model_id) : true) }),
            el('span', {}, m.name), el('small', { class: 'muted' }, m.status === 'ready' ? '' : m.status))));
    }

    run() {
        const prompt = document.getElementById('compare-prompt').value.trim();
        const modelIds = [...document.querySelectorAll('#compare-models input:checked')].map(i => i.value);
        if (!prompt) return toast('Enter a prompt to compare.', 'warn');
        if (modelIds.length < 1) return toast('Select at least one ready model.', 'warn');
        this.app.showView('compare');
        document.getElementById('compare-prompt-view').textContent = prompt;
        this.columns.innerHTML = '';
        const cols = {};
        for (const id of modelIds) {
            const body = el('div', { class: 'markdown' });
            const thinking = el('div', { class: 'thinking-text' });
            const stats = el('div', { class: 'turn-footer' }, el('span', { class: 'muted' }, 'waiting…'));
            const col = el('div', { class: 'compare-col' },
                el('div', { class: 'compare-head' }, id),
                el('details', { class: 'thinking' }, el('summary', {}, 'Reasoning'), thinking),
                body, stats);
            this.columns.append(col);
            cols[id] = { body, thinking, stats, raw: '' };
        }
        const settings = { ...this.app.currentSettings(), preset: 'plain' };
        const ws = new WebSocket(wsUrl('ws/compare'));
        ws.onopen = () => ws.send(JSON.stringify({ prompt, model_ids: modelIds, settings }));
        ws.onmessage = e => {
            const ev = JSON.parse(e.data);
            const c = cols[ev.model_id];
            if (ev.type === 'model_start') c.stats.innerHTML = ev.loading ? '<span class="spinner"></span> loading model into GPU…' : '<span class="spinner"></span> generating…';
            if (ev.type === 'thinking') c.thinking.textContent += ev.content;
            if (ev.type === 'token') { c.raw += ev.content; renderMarkdown(c.body, c.raw); }
            if (ev.type === 'model_error') c.stats.textContent = `Error: ${ev.content}`;
            if (ev.type === 'model_done') {
                c.stats.innerHTML = '';
                c.stats.append(
                    el('span', {}, `${formatNumber(ev.tokens)} tokens`),
                    ev.tokens_per_s ? el('span', {}, `${ev.tokens_per_s} tok/s`) : null,
                    el('span', {}, `${(ev.latency_ms / 1000).toFixed(1)}s`));
            }
            if (ev.type === 'done') ws.close();
        };
    }
}

export class EvalView {
    constructor(app) {
        this.app = app;
        this.tasks = [];
        document.getElementById('eval-run').addEventListener('click', () => this.run());
        document.getElementById('eval-close').addEventListener('click', () => { clearTimeout(this.timer); app.showView('chat'); });
        document.getElementById('eval-toggle-all').addEventListener('click', () => {
            const boxes = [...document.querySelectorAll('#eval-tasks input')];
            const on = boxes.some(b => !b.checked);
            boxes.forEach(b => { b.checked = on; });
        });
    }

    async load() {
        this.tasks = await api('api/tasks');
        const box = document.getElementById('eval-tasks');
        box.innerHTML = '';
        this.tasks.forEach(t => box.append(el('label', { class: 'checkbox-item' },
            el('input', { type: 'checkbox', value: t.id, checked: true }), el('span', {}, t.name), el('small', { class: 'muted' }, t.category))));
        this.loadRuns();
    }

    renderModels(models) {
        const box = document.getElementById('eval-models');
        const checked = new Set([...box.querySelectorAll('input:checked')].map(i => i.value));
        box.innerHTML = '';
        models.filter(m => m.can_run).forEach(m => box.append(el('label', { class: 'checkbox-item' },
            el('input', { type: 'checkbox', value: m.model_id, disabled: m.status !== 'ready', checked: m.status === 'ready' && (checked.size ? checked.has(m.model_id) : true) }),
            el('span', {}, m.name))));
    }

    async run() {
        const model_ids = [...document.querySelectorAll('#eval-models input:checked')].map(i => i.value);
        const task_ids = [...document.querySelectorAll('#eval-tasks input:checked')].map(i => i.value);
        if (!model_ids.length || !task_ids.length) return toast('Select at least one ready model and one task.', 'warn');
        try {
            const res = await api('api/eval/run', { method: 'POST', body: { name: document.getElementById('eval-name').value.trim() || null, model_ids, task_ids } });
            this.show(res.run_id);
            this.loadRuns();
        } catch (e) {
            toast(`Failed to start evaluation: ${e.message}`, 'error');
        }
    }

    async loadRuns() {
        const runs = await api('api/eval/runs');
        const box = document.getElementById('eval-runs');
        box.innerHTML = '';
        runs.forEach(r => box.append(el('div', { class: 'run-item', onclick: () => this.show(r.id) },
            el('div', {}, r.name),
            el('div', { class: 'muted small' }, `${r.status}${r.status === 'running' ? ` ${r.completed}/${r.total}` : ''} · ${r.model_ids.length} models · ${formatDate(r.created_at)}`))));
    }

    async show(runId) {
        clearTimeout(this.timer);
        this.app.showView('eval');
        const run = await api(`api/eval/runs/${runId}`);
        document.getElementById('eval-title').textContent = `${run.name} — ${run.status} ${run.completed}/${run.total}`;
        const content = document.getElementById('eval-content');
        content.innerHTML = '';
        const grid = el('div', { class: 'compare-columns' });
        for (const modelId of run.model_ids) {
            let ok = 0, tokens = 0, ms = 0;
            const items = run.tasks.map(t => {
                const r = run.results[t.id]?.[modelId];
                if (!r) return null;
                ok += r.success ? 1 : 0; tokens += r.tokens || 0; ms += r.latency_ms || 0;
                const scores = Object.entries(r.scores || {}).map(([k, v]) => `<span class="score ${v > 0.5 ? 'high' : 'low'}">${escapeHtml(k)} ${(v * 100).toFixed(0)}%</span>`).join(' ');
                const body = el('div', { class: 'markdown small' });
                renderMarkdown(body, (r.output || '').slice(0, 1500) + ((r.output || '').length > 1500 ? '\n\n…' : ''));
                return el('details', { class: 'eval-item' },
                    el('summary', { html: `${escapeHtml(t.name)} ${scores}` }),
                    r.error ? el('div', { class: 'error-box' }, r.error) : null, body);
            }).filter(Boolean);
            grid.append(el('div', { class: 'compare-col' },
                el('div', { class: 'compare-head' }, modelId),
                el('div', { class: 'muted small' }, `${ok}/${items.length} ok · ${formatNumber(tokens)} tokens · ${(ms / 1000).toFixed(0)}s`),
                items));
        }
        content.append(el('p', { class: 'muted small' }, 'Scores are heuristics (length, has code block, exact match when set) for smoke-testing, not a quality measure.'), grid);
        if (['pending', 'running'].includes(run.status)) this.timer = setTimeout(() => this.show(runId), 4000);
        this.loadRuns();
    }
}

const LAYERS = { openshell: 'OpenShell sandbox', broker: 'SQL broker', app: 'App policy' };
const DECISIONS = { deny: 'blocked', fail: 'not available', allow: 'allowed', info: 'info', approve: 'approved by user', reject: 'rejected by user' };

export class AuditView {
    constructor(app) {
        this.app = app;
        this.events = [];
        const $ = id => document.getElementById(id);
        $('audit-open').addEventListener('click', () => this.show());
        $('audit-refresh').addEventListener('click', () => this.show(this.filter));
        $('audit-close').addEventListener('click', () => app.showView('chat'));
        $('audit-selftest').addEventListener('click', () => this.selftest());
        $('audit-download').addEventListener('click', () =>
            download('audit.jsonl', this.events.map(e => JSON.stringify(e)).join('\n') + '\n', 'application/x-ndjson'));
    }

    async show({ sessionId } = {}) {
        const onlyChat = sessionId || (document.getElementById('audit-this-chat').checked ? this.app.chat.sessionId : null);
        this.filter = { sessionId: onlyChat };
        const decisions = new Set([...document.querySelectorAll('#audit-decisions input:checked')].map(i => i.value));
        this.app.showView('audit');
        const content = document.getElementById('audit-content');
        content.innerHTML = '<div class="muted small"><span class="spinner"></span> loading…</div>';
        try {
            const [events, gov] = await Promise.all([
                api(`api/audit?limit=2000${onlyChat ? `&session_id=${encodeURIComponent(onlyChat)}` : ''}`),
                api('api/governance'),
            ]);
            this.events = events;
            const shown = events.filter(e => decisions.has(e.decision));
            const count = d => events.filter(e => e.decision === d).length;
            const chats = new Set(events.map(e => e.session_id)).size;
            document.getElementById('audit-title').textContent = onlyChat ? `Audit trail — chat ${onlyChat}` : 'Audit trail — all chats';
            const os = gov.openshell || {};
            const exp = gov.audit_export || {};
            content.innerHTML = '';
            content.append(
                el('div', { class: 'audit-summary' },
                    el('div', { class: 'audit-stat' }, el('b', {}, String(count('deny'))), 'blocked by policy'),
                    el('div', { class: 'audit-stat' }, el('b', {}, String(count('fail'))), 'not available in sandbox'),
                    el('div', { class: 'audit-stat' }, el('b', {}, String(count('allow'))), 'allowed actions'),
                    el('div', { class: 'audit-stat' }, el('b', {}, `${count('approve')} / ${count('reject')}`), 'rules approved / rejected'),
                    el('div', { class: 'audit-stat' }, el('b', {}, String(chats)), onlyChat ? 'chat' : 'chats'),
                    el('div', { class: 'audit-stat' }, el('b', {}, os.gateway === 'connected' && os.provider === 'ready' ? '✓' : '✗'),
                        `OpenShell ${os.gateway || 'unknown'}${os.error ? ` (${os.error})` : ''}`),
                    el('div', { class: 'audit-stat' }, el('b', {}, exp.uri ? (exp.error ? '✗' : '✓') : '–'),
                        exp.uri ? `copied to ${exp.uri}${exp.last_export ? ` at ${formatDate(exp.last_export * 1000)}` : ' (every 5 min)'}` : 'kept on the app disk only'),
                ),
                el('p', { class: 'muted small' }, `Governed SQL is limited to: ${(gov.datasets || []).join(', ') || 'none'}. Showing ${shown.length} of ${events.length} events, newest first.`),
                shown.length ? this.table(shown, !onlyChat) : el('p', { class: 'muted' }, 'No matching events yet.'),
            );
        } catch (e) {
            content.innerHTML = '';
            content.append(el('div', { class: 'error-box' }, `Could not load the audit trail: ${e.message}`));
        }
    }

    table(events, showChat) {
        const rows = events.map(e => {
            const detail = e.args?.command || e.args?.sql || e.args?.code || '';
            return el('tr', { class: e.decision },
                el('td', {}, formatDate(e.ts * 1000)),
                showChat ? el('td', {}, el('a', { href: '#', onclick: ev => { ev.preventDefault(); this.show({ sessionId: e.session_id }); } }, e.session_id)) : null,
                el('td', {}, LAYERS[e.layer] || e.layer),
                el('td', {}, el('span', { class: `decision ${e.decision}` }, DECISIONS[e.decision] || e.decision)),
                el('td', {}, e.action),
                el('td', { class: 'mono' }, e.detail + (detail ? `\n${detail.slice(0, 400)}` : '')));
        });
        return el('table', { class: 'audit-table' },
            el('thead', {}, el('tr', {}, ['Time', showChat ? 'Chat' : null, 'Layer', 'Decision', 'Action', 'Detail'].filter(Boolean).map(h => el('th', {}, h)))),
            el('tbody', {}, rows));
    }

    async selftest() {
        this.app.showView('audit');
        document.getElementById('audit-title').textContent = 'Sandbox self-test';
        const content = document.getElementById('audit-content');
        content.innerHTML = '<div class="muted small"><span class="spinner"></span> creating a fresh sandbox and running checks (about a minute)…</div>';
        try {
            const r = await api('api/governance/selftest', { method: 'POST' });
            content.innerHTML = '';
            content.append(
                el('p', { class: 'muted small' }, `Ran in a throwaway sandbox (${r.session_id}), deleted afterwards. Its events are in the audit trail.`),
                el('table', { class: 'audit-table' },
                    el('thead', {}, el('tr', {}, ['Check', 'Expected', 'Output', 'Policy decisions'].map(h => el('th', {}, h)))),
                    el('tbody', {}, r.results.map(c => el('tr', { class: 'selftest-row' },
                        el('td', {}, c.check), el('td', {}, c.expect),
                        el('td', { class: 'mono' }, c.output.slice(-600)),
                        el('td', { class: 'mono' }, (c.blocked?.events || []).map(e => e.summary).join('\n') || '–'))))),
            );
        } catch (e) {
            content.innerHTML = '';
            content.append(el('div', { class: 'error-box' }, `Self-test failed: ${e.message}`));
        }
    }
}
