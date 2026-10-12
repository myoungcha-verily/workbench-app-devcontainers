import { api, el, formatDate, formatNumber, toast, wsUrl } from './api.js';
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
