import { api, ApiError, download, el, formatDate, prompt, toast } from './api.js';
import { renderMarkdown } from './markdown.js';
import { Canvas } from './canvas.js';
import { Chat } from './chat.js';
import { AuditView, CompareView, EvalView } from './views.js';

const PREFS_KEY = 'nemotron-eval-prefs';

const SLASH = [
    { cmd: '/help', desc: 'Show commands and shortcuts' },
    { cmd: '/new', desc: 'Start a new chat' },
    { cmd: '/ls', args: '[gs://bucket/path]', desc: 'List workspace buckets, or files under a path' },
    { cmd: '/tables', args: '<dataset>', desc: 'List tables in a BigQuery dataset' },
    { cmd: '/schema', args: '<project.dataset.table>', desc: 'Columns, types and value hints of a table' },
    { cmd: '/sql', args: '<query>', desc: 'Open a query in the canvas and run it' },
    { cmd: '/tools', desc: 'Show the tools the model can use' },
    { cmd: '/notes', desc: 'Edit the data notes shared with the model' },
    { cmd: '/compare', args: '<prompt>', desc: 'Compare models on a prompt' },
    { cmd: '/export', desc: 'Download this chat as Markdown' },
    { cmd: '/save', desc: 'Save this chat to a workspace bucket' },
    { cmd: '/canvas', desc: 'Show or hide the canvas' },
];

class App {
    constructor() {
        this.prefs = { theme: 'dark', showReasoning: false, notify: false, model: '', reasoning: 'on', effort: 'balanced', preset: 'workbench', ...JSON.parse(localStorage.getItem(PREFS_KEY) || '{}') };
        this.models = [];
        this.sessions = [];
        this.buckets = null;
        this.options = null;
        this.canvas = new Canvas(document.getElementById('canvas'), {
            getSessionId: () => this.chat.sessionId,
            ensureSession: () => this.ensureSavedSession(),
            getBuckets: () => this.getBuckets(),
        });
        this.chat = new Chat(this);
        this.compare = new CompareView(this);
        this.evalView = new EvalView(this);
        this.audit = new AuditView(this);
        this.applyTheme(this.prefs.theme);
        this.bind();
        this.init();
    }

    async init() {
        try { this.options = await api('api/options'); } catch { this.options = { presets: [], num_ctx: 0 }; }
        const presetSel = document.getElementById('preset-select');
        presetSel.innerHTML = '';
        (this.options.presets || []).forEach(p => presetSel.append(el('option', { value: p.id }, p.label)));
        presetSel.value = this.prefs.preset;
        document.getElementById('effort-select').value = this.prefs.effort;
        document.getElementById('governed-select').value = String(this.prefs.governed !== false);
        this.renderGoverned();
        document.getElementById('show-reasoning').checked = this.prefs.showReasoning;
        document.getElementById('notify-toggle').checked = this.prefs.notify;
        this.loadModels();
        this.loadSessions();
        this.loadPrompts();
        this.evalView.load();
        this.pollStatus();
        api('api/notes').then(n => { document.getElementById('data-notes').value = n.text || ''; }).catch(() => {});
    }

    savePrefs(patch) {
        Object.assign(this.prefs, patch);
        localStorage.setItem(PREFS_KEY, JSON.stringify(this.prefs));
    }

    // ------------------------------------------------------------ models and settings

    async loadModels() {
        try {
            this.models = await api('api/models');
        } catch {
            setTimeout(() => this.loadModels(), 5000);
            return;
        }
        this.renderModels();
        this.compare.renderModels(this.models);
        this.evalView.renderModels(this.models);
        const settled = this.models.every(m => !m.can_run || ['ready', 'error'].includes(m.status));
        if (!settled) setTimeout(() => this.loadModels(), 5000);
    }

    modelStatusText(m) {
        if (!m.can_run) return m.warning || 'unavailable';
        if (m.status === 'ready') return 'ready';
        return `${m.status}${m.detail ? ' — ' + m.detail : ''}`;
    }

    renderModels() {
        const sel = document.getElementById('model-select');
        const current = sel.value || this.prefs.model;
        sel.innerHTML = '';
        const ready = this.models.filter(m => m.status === 'ready');
        if (!ready.length) sel.append(el('option', { value: '' }, 'No model ready yet…'));
        this.models.forEach(m => sel.append(el('option', { value: m.model_id, disabled: m.status !== 'ready' },
            m.status === 'ready' ? m.name : `${m.name} — ${this.modelStatusText(m)}`)));
        const pick = ready.find(m => m.model_id === current) || [...ready].sort((a, b) => b.min_vram_gb - a.min_vram_gb)[0];
        sel.value = pick ? pick.model_id : '';
        this.renderReasoning();
    }

    renderReasoning() {
        const model = this.models.find(m => m.model_id === document.getElementById('model-select').value);
        const sel = document.getElementById('reasoning-select');
        const values = model?.thinking_values || [];
        const options = [];
        if (!values.length) options.push(['default', model ? 'Not supported' : '—']);
        if (values.includes(false)) options.push(['off', 'Off']);
        if (values.includes(true)) options.push(['on', 'On']);
        values.filter(v => typeof v === 'string').forEach(v => options.push([v, v[0].toUpperCase() + v.slice(1)]));
        sel.innerHTML = '';
        options.forEach(([v, label]) => sel.append(el('option', { value: v }, label)));
        sel.value = options.some(o => o[0] === this.prefs.reasoning) ? this.prefs.reasoning : options[options.length > 1 ? 1 : 0][0];
        sel.disabled = !values.length;
    }

    modelReady() {
        return this.models.some(m => m.model_id === document.getElementById('model-select').value && m.status === 'ready');
    }

    currentSettings() {
        const tempDefault = document.getElementById('temp-default').checked;
        const maxTokens = parseInt(document.getElementById('max-tokens').value, 10);
        return {
            model_id: document.getElementById('model-select').value,
            reasoning: document.getElementById('reasoning-select').value || 'on',
            effort: document.getElementById('effort-select').value,
            preset: document.getElementById('preset-select').value || 'workbench',
            system_prompt: document.getElementById('system-prompt').value,
            tools: document.getElementById('tools-toggle').checked,
            temperature: tempDefault ? null : parseFloat(document.getElementById('temperature').value),
            max_tokens: Number.isFinite(maxTokens) ? maxTokens : null,
            governed: document.getElementById('governed-select').value !== 'false',
        };
    }

    renderGoverned() {
        const governed = document.getElementById('governed-select').value !== 'false';
        document.body.classList.toggle('ungoverned', !governed);
    }

    applySettings(s) {
        if (!s) return;
        const modelSel = document.getElementById('model-select');
        if (this.models.some(m => m.model_id === s.model_id && m.status === 'ready')) modelSel.value = s.model_id;
        this.renderReasoning();
        if ([...document.getElementById('reasoning-select').options].some(o => o.value === s.reasoning)) document.getElementById('reasoning-select').value = s.reasoning;
        document.getElementById('effort-select').value = s.effort || 'balanced';
        document.getElementById('preset-select').value = s.preset || 'workbench';
        document.getElementById('system-prompt').value = s.system_prompt || '';
        document.getElementById('tools-toggle').checked = s.tools !== false;
        document.getElementById('temp-default').checked = s.temperature === null || s.temperature === undefined;
        document.getElementById('temperature').disabled = document.getElementById('temp-default').checked;
        if (s.temperature !== null && s.temperature !== undefined) document.getElementById('temperature').value = s.temperature;
        this.updateTempLabel();
        document.getElementById('max-tokens').value = s.max_tokens || '';
        document.getElementById('governed-select').value = String(s.governed !== false);
        this.renderGoverned();
    }

    updateTempLabel() {
        const def = document.getElementById('temp-default').checked;
        document.getElementById('temp-value').textContent = def ? 'model default' : document.getElementById('temperature').value;
    }

    // ------------------------------------------------------------ sessions

    async ensureSavedSession() {
        const id = this.chat.ensureSessionId();
        if (!this.sessions.some(s => s.id === id)) {
            // A canvas artifact can be created before the first message.
            this.sessions.unshift({ id, title: 'New chat', updated_at: new Date().toISOString(), messages: 0 });
            this.renderSessions();
        }
        return id;
    }

    async loadSessions() {
        try { this.sessions = await api('api/sessions'); } catch { this.sessions = []; }
        this.renderSessions();
    }

    renderSessions() {
        const q = document.getElementById('session-search').value.toLowerCase();
        const list = document.getElementById('session-list');
        list.innerHTML = '';
        this.sessions.filter(s => s.title.toLowerCase().includes(q)).forEach(s => {
            list.append(el('li', { class: `session-item${s.id === this.chat.sessionId ? ' active' : ''}`, onclick: () => this.openSession(s.id) },
                el('div', { class: 'session-title' }, s.title),
                el('div', { class: 'session-meta' }, el('span', {}, s.model_id || ''), el('span', {}, formatDate(s.updated_at))),
                el('div', { class: 'session-actions' },
                    el('button', { class: 'btn tiny ghost', title: 'Rename', onclick: e => { e.stopPropagation(); this.renameSession(s); } }, '✎'),
                    el('button', { class: 'btn tiny ghost', title: 'Export Markdown', onclick: e => { e.stopPropagation(); this.exportSession(s.id); } }, '⬇'),
                    el('button', { class: 'btn tiny ghost', title: 'Delete', onclick: e => { e.stopPropagation(); this.deleteSession(s); } }, '🗑'),
                ),
            ));
        });
        if (!list.children.length) list.append(el('li', { class: 'muted small empty' }, q ? 'No matching chats' : 'No chats yet'));
    }

    async openSession(id) {
        this.showView('chat');
        try { await this.chat.loadSession(id); } catch (e) { toast(`Could not open chat: ${e.message}`, 'error'); }
    }

    onSessionChanged(id, { pending = false } = {}) {
        if (pending && id && !this.sessions.some(s => s.id === id)) {
            this.sessions.unshift({ id, title: 'New chat', updated_at: new Date().toISOString(), messages: 1 });
        }
        this.renderSessions();
    }

    onSessionUpdated(summary) {
        const i = this.sessions.findIndex(s => s.id === summary.id);
        if (i >= 0) this.sessions.splice(i, 1);
        this.sessions.unshift(summary);
        this.renderSessions();
    }

    async renameSession(s) {
        const title = await prompt('Rename chat', { value: s.title });
        if (!title) return;
        try {
            const updated = await api(`api/sessions/${encodeURIComponent(s.id)}`, { method: 'PATCH', body: { title } });
            Object.assign(s, updated);
            if (s.id === this.chat.sessionId) document.getElementById('chat-title').textContent = updated.title;
            this.renderSessions();
        } catch (e) { toast(e.message, 'error'); }
    }

    async deleteSession(s) {
        if (!confirm(`Delete "${s.title}" and its artifacts?`)) return;
        await api(`api/sessions/${encodeURIComponent(s.id)}`, { method: 'DELETE' });
        this.sessions = this.sessions.filter(x => x.id !== s.id);
        if (s.id === this.chat.sessionId) this.chat.newChat();
        this.renderSessions();
    }

    async exportSession(id = this.chat.sessionId) {
        if (!id) return toast('Nothing to export yet.', 'warn');
        try {
            const md = await api(`api/sessions/${encodeURIComponent(id)}/export.md`);
            const title = this.sessions.find(s => s.id === id)?.title || id;
            download(`${title.replace(/[^\w-]+/g, '-').slice(0, 50) || 'chat'}.md`, md, 'text/markdown');
        } catch (e) { toast(e.message, 'error'); }
    }

    async saveSessionToBucket(id = this.chat.sessionId) {
        if (!id) return toast('Nothing to save yet.', 'warn');
        const buckets = await this.getBuckets();
        if (!buckets.length) return toast('No workspace buckets found (Workbench tools may still be starting).', 'warn');
        const bucket = await prompt('Save chat to bucket', {
            label: `Saved as nemotron-eval/chats/${id}.md`,
            options: buckets.map(b => ({ value: b.bucket, label: `${b.id} (gs://${b.bucket})` })), okText: 'Save',
        });
        if (!bucket) return;
        try {
            const { uri } = await api(`api/sessions/${encodeURIComponent(id)}/save`, { method: 'POST', body: { bucket } });
            toast(`Saved to ${uri}`, 'ok', 6000);
        } catch (e) { toast(e.message, 'error', 8000); }
    }

    async getBuckets() {
        if (this.buckets && this.buckets.length) return this.buckets;
        try { this.buckets = await api('api/buckets'); } catch { this.buckets = []; }
        return this.buckets;
    }

    // ------------------------------------------------------------ prompt library

    async loadPrompts() {
        try { this.prompts = await api('api/prompts'); } catch { this.prompts = { saved: [], builtin: [] }; }
        this.renderPrompts();
    }

    renderPrompts() {
        const q = document.getElementById('prompt-search').value.toLowerCase();
        const box = document.getElementById('prompt-list');
        box.innerHTML = '';
        const match = p => (p.title + ' ' + p.prompt).toLowerCase().includes(q);
        const use = p => { this.showView('chat'); this.chat.input.value = p.prompt; this.chat.input.dispatchEvent(new Event('input')); this.chat.input.focus(); };
        const saved = this.prompts.saved.filter(match);
        if (saved.length) {
            box.append(el('div', { class: 'group-label' }, 'Saved'));
            saved.forEach(p => box.append(el('div', { class: 'prompt-item', onclick: () => use(p) },
                el('div', { class: 'prompt-title' }, p.title),
                el('div', { class: 'prompt-text' }, p.prompt.slice(0, 120)),
                el('button', { class: 'btn tiny ghost prompt-del', title: 'Delete', onclick: async e => {
                    e.stopPropagation();
                    await api(`api/prompts/${p.id}`, { method: 'DELETE' });
                    this.loadPrompts();
                } }, '🗑'))));
        }
        const groups = {};
        this.prompts.builtin.filter(match).forEach(p => (groups[p.category] = groups[p.category] || []).push(p));
        for (const [category, items] of Object.entries(groups)) {
            box.append(el('div', { class: 'group-label' }, `${category} (benchmark tasks)`));
            items.forEach(p => box.append(el('div', { class: 'prompt-item', onclick: () => use(p) },
                el('div', { class: 'prompt-title' }, p.title), el('div', { class: 'prompt-text' }, p.prompt.slice(0, 120)))));
        }
    }

    async saveCurrentPrompt() {
        const text = this.chat.input.value.trim();
        if (!text) return toast('Type a prompt in the chat box first.', 'warn');
        const title = await prompt('Save prompt', { value: text.slice(0, 50) });
        if (!title) return;
        await api('api/prompts', { method: 'POST', body: { title, prompt: text } });
        this.loadPrompts();
        toast('Prompt saved', 'ok');
    }

    // ------------------------------------------------------------ status

    async pollStatus() {
        try {
            this.status = await api('api/status');
            const s = this.status;
            const parts = [];
            if (s.gpu) parts.push(`GPU ${(s.gpu.used_mb / 1024).toFixed(1)}/${(s.gpu.total_mb / 1024).toFixed(0)} GB`);
            if (s.loaded.length) parts.push(s.loaded.map(m => m.name.replace(/^nemotron-/, '')).join(', '));
            parts.push(s.mcp.connected ? `tools ✓` : 'tools …');
            parts.push(s.context.available ? 'context ✓' : 'context …');
            document.getElementById('status-text').textContent = parts.join(' · ');
            const ready = this.models.some(m => m.status === 'ready');
            document.getElementById('status-dot').className = `dot ${ready && s.mcp.connected && s.context.available ? 'ok' : ready ? 'warn' : 'busy'}`;
        } catch {
            document.getElementById('status-text').textContent = 'backend unreachable';
            document.getElementById('status-dot').className = 'dot error';
        }
        setTimeout(() => this.pollStatus(), 8000);
    }

    showStatusDetails() {
        const s = this.status || {};
        const lines = [
            '### Models',
            ...this.models.map(m => `- **${m.name}** — ${this.modelStatusText(m)}${m.capabilities?.length ? ` (${m.capabilities.join(', ')})` : ''}`),
            '', '### GPU',
            s.gpu ? `${(s.gpu.used_mb / 1024).toFixed(1)} of ${(s.gpu.total_mb / 1024).toFixed(1)} GB used` : 'No GPU detected',
            s.loaded?.length ? `Loaded: ${s.loaded.map(m => m.name).join(', ')}` : 'No model loaded in memory',
            '', '### Workbench',
            s.mcp ? (s.mcp.connected ? `MCP server connected, ${s.mcp.tools} tools` : `MCP server not connected: ${s.mcp.error}`) : '',
            s.context ? (s.context.available ? `Workspace context generated ${s.context.generated_at} (${Math.round(s.context.chars / 1000)}K chars)` : 'Workspace context not generated yet') : '',
            s.catalog ? `Catalog: ${s.catalog.datasets} datasets, ${s.catalog.buckets} buckets${s.catalog.error ? ` (${s.catalog.error})` : ''}` : '',
            `Context window: ${s.num_ctx || '?'} tokens`,
        ];
        this.modal('Status', lines.join('\n'));
    }

    modal(title, markdown) {
        const body = el('div', { class: 'markdown' });
        renderMarkdown(body, markdown);
        const overlay = el('div', { class: 'modal-overlay', onclick: e => { if (e.target === overlay) overlay.remove(); } },
            el('div', { class: 'modal wide' }, el('h3', {}, title), body,
                el('div', { class: 'modal-actions' }, el('button', { class: 'btn', onclick: () => overlay.remove() }, 'Close'))));
        document.body.append(overlay);
    }

    notify(text) {
        if (!this.prefs.notify || !document.hidden || !('Notification' in window) || Notification.permission !== 'granted') return;
        new Notification('Nemotron Eval Terminal', { body: text });
    }

    // ------------------------------------------------------------ views, theme, layout

    showView(name) {
        document.getElementById('chat-view').hidden = name !== 'chat';
        document.getElementById('compare-view').hidden = name !== 'compare';
        document.getElementById('eval-view').hidden = name !== 'eval';
        document.getElementById('audit-view').hidden = name !== 'audit';
    }

    applyTheme(theme) {
        document.documentElement.dataset.theme = theme;
        document.getElementById('hljs-theme').href = theme === 'light' ? 'vendor/hljs-light.min.css' : 'vendor/hljs-dark.min.css';
    }

    // ------------------------------------------------------------ slash commands

    slashMenuActive() { return false; }

    updateSlashMenu(text) {
        const menu = document.getElementById('slash-menu');
        if (!text.startsWith('/') || text.includes(' ') || text.includes('\n')) { menu.classList.remove('open'); return; }
        const matches = SLASH.filter(c => c.cmd.startsWith(text.toLowerCase()));
        menu.innerHTML = '';
        matches.forEach(c => menu.append(el('div', { class: 'slash-item', onclick: () => {
            this.chat.input.value = c.cmd + (c.args ? ' ' : '');
            this.chat.input.focus();
            menu.classList.remove('open');
        } }, el('span', { class: 'slash-cmd' }, c.cmd), c.args ? el('span', { class: 'muted' }, ` ${c.args}`) : null, el('span', { class: 'slash-desc' }, c.desc))));
        menu.classList.toggle('open', matches.length > 0);
    }

    note(markdown, kind = '') {
        if (this.chat.messages.querySelector('.welcome')) this.chat.messages.innerHTML = '';
        const body = el('div', { class: 'markdown' });
        renderMarkdown(body, markdown);
        this.chat.messages.append(el('div', { class: `message note ${kind}` }, body));
        this.chat.scrollToBottom(true);
    }

    /** Returns true when `text` was a command (handled locally, not sent to the model). */
    async runSlash(text) {
        const [cmd, ...rest] = text.split(/\s+/);
        const arg = text.slice(cmd.length).trim();
        const known = SLASH.find(c => c.cmd === cmd.toLowerCase());
        if (!known) return false;
        document.getElementById('slash-menu').classList.remove('open');
        try {
            switch (known.cmd) {
                case '/help': return this.showHelp(), true;
                case '/new': return this.chat.newChat(), true;
                case '/canvas': return this.canvas.toggle(), true;
                case '/notes': return this.openSettings(true), true;
                case '/export': return this.exportSession(), true;
                case '/save': return this.saveSessionToBucket(), true;
                case '/compare': {
                    document.querySelector('[data-tab="compare"]').click();
                    document.getElementById('compare-prompt').value = arg;
                    if (arg) this.compare.run();
                    return true;
                }
                case '/ls': {
                    if (!arg) {
                        const buckets = await this.getBuckets();
                        this.note(`**Workspace buckets**\n\n${buckets.map(b => `- \`${b.id}\` → \`gs://${b.bucket}/\``).join('\n') || '_none found yet_'}`);
                    } else {
                        const path = arg.startsWith('gs://') ? arg : `gs://${arg}`;
                        const res = await api('api/tools/call', { method: 'POST', body: { name: 'gsutil_execute', arguments: { command: `ls -l ${path}` } } });
                        const out = res.text.split('\n').filter(l => !/legacy tool|Learn more|gsutil-transition/.test(l)).join('\n').trim();
                        this.note(`**${path}**\n\n\`\`\`\n${out || '(empty)'}\n\`\`\``);
                    }
                    return true;
                }
                case '/tables': {
                    if (!arg) return this.note('Usage: `/tables <dataset>`'), true;
                    const res = await api(`api/catalog/tables?dataset=${encodeURIComponent(arg)}`);
                    this.note(`**Tables in \`${res.dataset}\`** (${res.tables.length})\n\n${res.tables.map(t => `- \`${t.table}\``).join('\n')}`);
                    return true;
                }
                case '/schema': {
                    if (!arg) return this.note('Usage: `/schema <project.dataset.table>`'), true;
                    this.note(`_Describing \`${arg}\`…_`);
                    const text = await api(`api/catalog/describe?table=${encodeURIComponent(arg)}`);
                    this.chat.messages.lastChild.remove();
                    this.note(text.replace(/^Table (`[^`]+`)/, '**Table** $1'));
                    return true;
                }
                case '/sql': {
                    if (!arg) return this.note('Usage: `/sql SELECT …`'), true;
                    const art = await this.canvas.create('sql', 'Query', arg);
                    this.canvas.runSql(art, arg, 500);
                    return true;
                }
                case '/tools': {
                    const t = await api('api/tools');
                    this.note([
                        `**Workbench MCP:** ${t.connected ? `connected, ${t.all.length} tools` : `not connected (${t.error})`}`,
                        '', '**Always available:** ' + [...t.local.map(x => x.name), ...t.core].map(n => `\`${n}\``).join(', '),
                        '', `The model can enable any of the other ${Math.max(0, t.all.length - t.core.length)} tools with \`find_tools\`. Deletes always ask for your approval.`,
                    ].join('\n'));
                    return true;
                }
            }
        } catch (e) {
            const detail = e instanceof ApiError && e.detail?.reason ? e.detail.reason : e.message;
            this.note(`**${cmd} failed:** ${detail}`, 'error');
            return true;
        }
        return false;
    }

    showHelp() {
        this.modal('Commands and shortcuts', [
            '### Slash commands', ...SLASH.map(c => `- \`${c.cmd}${c.args ? ' ' + c.args : ''}\` — ${c.desc}`),
            '', '### Shortcuts',
            '- **Enter** send · **Shift+Enter** new line · **Tab** complete a command',
            '- **Esc** stop the reply / close dialogs',
            '- **Ctrl/⌘+K** new chat · **Ctrl/⌘+B** sidebar · **Ctrl/⌘+.** canvas · **Ctrl/⌘+/** focus input',
            '- In the canvas: **Ctrl/⌘+Enter** run SQL · **Ctrl/⌘+S** save a version',
            '', '### Tips',
            '- Attach CSV, JSON or code files with 📎 or drag and drop.',
            '- **Effort** sets the output budget and how many tool steps the agent may take.',
            '- Put dataset conventions (codes, units, flags) in **Settings → Data notes**; the model sees them in every chat.',
        ].join('\n'));
    }

    // ------------------------------------------------------------ settings drawer

    openSettings(focusNotes = false) {
        const drawer = document.getElementById('settings-drawer');
        drawer.hidden = false;
        if (focusNotes) document.getElementById('data-notes').focus();
    }

    // ------------------------------------------------------------ events

    bind() {
        const $ = id => document.getElementById(id);
        $('model-select').addEventListener('change', e => { this.savePrefs({ model: e.target.value }); this.renderReasoning(); });
        $('reasoning-select').addEventListener('change', e => this.savePrefs({ reasoning: e.target.value }));
        $('effort-select').addEventListener('change', e => this.savePrefs({ effort: e.target.value }));
        $('preset-select').addEventListener('change', e => this.savePrefs({ preset: e.target.value }));
        $('governed-select').addEventListener('change', e => { this.savePrefs({ governed: e.target.value !== 'false' }); this.renderGoverned(); });
        $('new-chat-btn').addEventListener('click', () => { this.showView('chat'); this.chat.newChat(); });
        $('session-search').addEventListener('input', () => this.renderSessions());
        $('prompt-search').addEventListener('input', () => this.renderPrompts());
        $('save-prompt-btn').addEventListener('click', () => this.saveCurrentPrompt());
        $('export-btn').addEventListener('click', () => this.exportSession());
        $('save-chat-btn').addEventListener('click', () => this.saveSessionToBucket());
        $('status-pill').addEventListener('click', () => this.showStatusDetails());
        $('help-btn').addEventListener('click', () => this.showHelp());
        $('canvas-btn').addEventListener('click', () => this.canvas.toggle());
        $('sidebar-toggle').addEventListener('click', () => document.body.classList.toggle('sidebar-collapsed'));
        $('theme-btn').addEventListener('click', () => {
            const theme = this.prefs.theme === 'light' ? 'dark' : 'light';
            this.savePrefs({ theme });
            this.applyTheme(theme);
        });
        $('settings-btn').addEventListener('click', () => { const d = $('settings-drawer'); d.hidden = !d.hidden; });
        $('settings-close').addEventListener('click', () => { $('settings-drawer').hidden = true; });
        $('temp-default').addEventListener('change', e => { $('temperature').disabled = e.target.checked; this.updateTempLabel(); });
        $('temperature').addEventListener('input', () => this.updateTempLabel());
        $('show-reasoning').addEventListener('change', e => this.savePrefs({ showReasoning: e.target.checked }));
        $('notify-toggle').addEventListener('change', async e => {
            if (e.target.checked && 'Notification' in window && Notification.permission !== 'granted') {
                const p = await Notification.requestPermission();
                if (p !== 'granted') { e.target.checked = false; toast('Notifications were blocked by the browser.', 'warn'); }
            }
            this.savePrefs({ notify: e.target.checked });
        });
        $('save-notes').addEventListener('click', async () => {
            try {
                const r = await api('api/notes', { method: 'PUT', body: { text: $('data-notes').value } });
                $('notes-status').textContent = `Saved (${r.chars} characters). Applies from the next message.`;
            } catch (e) { toast(e.message, 'error'); }
        });

        document.querySelectorAll('.tab-btn').forEach(btn => btn.addEventListener('click', () => {
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b === btn));
            document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === `tab-${btn.dataset.tab}`));
        }));

        this.chat.input.addEventListener('keydown', e => {
            const menu = $('slash-menu');
            if (e.key === 'Tab' && menu.classList.contains('open')) {
                e.preventDefault();
                menu.querySelector('.slash-item')?.click();
            }
        });

        document.addEventListener('keydown', e => {
            const mod = e.metaKey || e.ctrlKey;
            if (e.key === 'Escape') {
                const overlay = document.querySelector('.modal-overlay');
                if (overlay) return overlay.remove();
                if (!$('settings-drawer').hidden) return ($('settings-drawer').hidden = true);
                if (document.querySelector('.canvas.fullscreen')) return document.querySelector('.canvas.fullscreen').classList.remove('fullscreen');
                if (this.chat.running) this.chat.stop();
            }
            if (mod && e.key.toLowerCase() === 'k') { e.preventDefault(); this.showView('chat'); this.chat.newChat(); }
            if (mod && e.key.toLowerCase() === 'b') { e.preventDefault(); document.body.classList.toggle('sidebar-collapsed'); }
            if (mod && e.key === '.') { e.preventDefault(); this.canvas.toggle(); }
            if (mod && e.key === '/') { e.preventDefault(); this.chat.input.focus(); }
        });

        // Drag the canvas edge to resize it.
        const canvas = $('canvas');
        const handle = el('div', { class: 'canvas-resize', title: 'Drag to resize' });
        canvas.prepend(handle);
        handle.addEventListener('pointerdown', e => {
            e.preventDefault();
            handle.setPointerCapture(e.pointerId);
            const move = ev => {
                const width = Math.min(Math.max(window.innerWidth - ev.clientX, 320), window.innerWidth * 0.75);
                document.documentElement.style.setProperty('--canvas-width', `${width}px`);
            };
            const up = () => { handle.removeEventListener('pointermove', move); handle.removeEventListener('pointerup', up); };
            handle.addEventListener('pointermove', move);
            handle.addEventListener('pointerup', up);
        });
    }
}

window.app = new App();
