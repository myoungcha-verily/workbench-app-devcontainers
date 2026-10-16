import { api, copyText, el, formatNumber, toast, wsUrl } from './api.js';
import { renderMarkdown } from './markdown.js';
import { resultTable } from './canvas.js';

const MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024;

function argsSummary(name, args) {
    const value = args.command || args.sql || args.query || args.title || args.resourceId || args.name || args.workspaceId;
    if (value) return String(value).replace(/\s+/g, ' ').slice(0, 90);
    const keys = Object.keys(args || {});
    return keys.length ? keys.join(', ') : '';
}

export class Chat {
    constructor(app) {
        this.app = app;
        this.messages = document.getElementById('chat-messages');
        this.input = document.getElementById('chat-input');
        this.sendBtn = document.getElementById('send-btn');
        this.attachList = document.getElementById('attachments');
        this.fileInput = document.getElementById('file-input');
        this.sessionId = null;
        this.ws = null;
        this.wsSessionId = null;
        this.running = false;
        this.attachments = [];
        this.cards = new Map();

        this.sendBtn.addEventListener('click', () => (this.running ? this.stop() : this.submit()));
        this.input.addEventListener('keydown', e => {
            if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && !this.app.slashMenuActive()) {
                e.preventDefault();
                this.submit();
            }
        });
        this.input.addEventListener('input', () => {
            this.input.style.height = 'auto';
            this.input.style.height = Math.min(this.input.scrollHeight, 240) + 'px';
            this.app.updateSlashMenu(this.input.value);
        });
        document.getElementById('attach-btn').addEventListener('click', () => this.fileInput.click());
        this.fileInput.addEventListener('change', () => { this.addFiles(this.fileInput.files); this.fileInput.value = ''; });
        const main = document.querySelector('.chat-container');
        main.addEventListener('dragover', e => { e.preventDefault(); main.classList.add('dragging'); });
        main.addEventListener('dragleave', () => main.classList.remove('dragging'));
        main.addEventListener('drop', e => { e.preventDefault(); main.classList.remove('dragging'); this.addFiles(e.dataTransfer.files); });
        this.showWelcome();
    }

    // ------------------------------------------------------------ sessions

    ensureSessionId() {
        if (!this.sessionId) {
            this.sessionId = 's' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
        }
        return this.sessionId;
    }

    newChat() {
        if (this.running) this.stop();
        this.sessionId = null;
        this.closeSocket();
        this.cards.clear();
        this.showWelcome();
        document.getElementById('chat-title').textContent = 'New chat';
        this.app.canvas.loadSession(null);
        this.app.onSessionChanged(null);
        this.input.focus();
    }

    async loadSession(id) {
        if (this.running) this.stop();
        const session = await api(`api/sessions/${encodeURIComponent(id)}`);
        this.sessionId = session.id;
        this.closeSocket();
        // Load artifacts first so chips in the history show their titles.
        await this.app.canvas.loadSession(session.id);
        this.render(session);
        if (session.settings?.governed) this.watchProposals({ once: true });
        document.getElementById('chat-title').textContent = session.title;
        this.app.applySettings(session.settings);
        this.app.onSessionChanged(session.id);
    }

    showWelcome() {
        this.messages.innerHTML = '';
        this.messages.append(el('div', { class: 'welcome' },
            el('div', { class: 'welcome-icon' }, '⚡'),
            el('h2', {}, 'Nemotron Eval Terminal'),
            el('p', {}, 'Self-hosted NVIDIA Nemotron with your Workbench workspace: it can browse buckets, query BigQuery, and build reports, charts and mini-apps in the canvas.'),
            el('div', { class: 'suggestions' }, [
                'What data is in this workspace?',
                'List the files in the synthetic-allofus-demo bucket and summarize them in a markdown report',
                'Chart the age distribution in person_data.csv',
                'Build an HTML mini-app that shows the row count of each table in a BigQuery dataset I choose',
            ].map(s => el('button', { class: 'suggestion', onclick: () => { this.input.value = s; this.input.focus(); } }, s))),
            el('p', { class: 'muted small' }, 'Type / for commands · Shift+Enter for a new line · Esc to stop'),
        ));
    }

    // ------------------------------------------------------------ rendering history

    render(session) {
        this.messages.innerHTML = '';
        this.cards.clear();
        let turn = null;
        const artifactTitles = new Map();
        session.messages.forEach((m, index) => {
            if (m.role === 'user') {
                turn = null;
                this.messages.append(this.userBubble(m, index));
                return;
            }
            if (!turn) turn = this.newTurn(m.model_id || session.model_id);
            if (m.role === 'assistant') {
                if (m.thinking) this.addThinking(turn, m.thinking, { open: false });
                if (m.meta?.notice) turn.body.append(el('div', { class: 'notice' }, m.content));
                else if (m.content) renderMarkdown(this.addSegment(turn), m.content, { onOpenInCanvas: this.openInCanvas });
                for (const call of m.tool_calls || []) this.addToolCard(turn, call);
                if (m.meta?.stopped) turn.body.append(el('div', { class: 'notice' }, 'Stopped.'));
                if (m.tokens) this.finishTurn(turn, { tokens: m.tokens, latency_ms: m.latency_ms, tokens_per_s: m.meta?.tokens_per_s, context_tokens: m.meta?.context_tokens });
            } else if (m.role === 'tool') {
                const isError = !!m.meta?.is_error;
                this.updateToolCard(m.tool_call_id, { ok: !isError, result: m.content, duration_ms: m.meta?.duration_ms, name: m.tool_name, blocked: m.meta?.blocked });
                if (m.meta?.artifact_id) {
                    const art = this.app.canvas.artifacts.get(m.meta.artifact_id);
                    this.artifactChip(turn, { id: m.meta.artifact_id, title: art?.title || artifactTitles.get(m.meta.artifact_id) || m.meta.artifact_id, type: art?.type || '' });
                }
            }
        });
        const lastTurn = [...this.messages.querySelectorAll('.turn')].pop();
        if (lastTurn) this.addRegenerate(lastTurn);
        this.scrollToBottom(true);
    }

    userBubble(m, index) {
        const text = m.meta?.display ?? m.content;
        const body = el('div', { class: 'bubble-text' });
        renderMarkdown(body, text);
        const bubble = el('div', { class: 'message user', dataset: { index: index ?? '' } },
            el('div', { class: 'bubble' },
                body,
                (m.meta?.attachments || []).length ? el('div', { class: 'chips' }, m.meta.attachments.map(n => el('span', { class: 'chip' }, `📎 ${n}`))) : null,
            ),
            el('div', { class: 'message-actions' },
                el('button', { class: 'btn tiny ghost', title: 'Edit and resend', onclick: () => this.startEdit(bubble, text) }, '✎ Edit'),
                el('button', { class: 'btn tiny ghost', onclick: e => copyText(text, e.target) }, 'Copy'),
            ),
        );
        return bubble;
    }

    newTurn(modelId) {
        const body = el('div', { class: 'turn-body' });
        const footer = el('div', { class: 'turn-footer' });
        const node = el('div', { class: 'message assistant turn' },
            el('div', { class: 'turn-header' }, el('span', { class: 'avatar' }, '⚡'), el('span', { class: 'model-name' }, modelId || 'assistant')),
            body, footer,
        );
        this.messages.append(node);
        return { node, body, footer, segment: null, raw: '', thinking: null, status: null, modelId };
    }

    addSegment(turn) {
        const seg = el('div', { class: 'markdown segment' });
        turn.body.append(seg);
        return seg;
    }

    addThinking(turn, text, { open = false } = {}) {
        const content = el('div', { class: 'thinking-text' }, text);
        const details = el('details', { class: 'thinking', open }, el('summary', {}, 'Reasoning'), content);
        turn.body.append(details);
        return content;
    }

    openInCanvas = (type, content, language) => {
        const title = { sql: 'Query', html: 'Mini-app', markdown: 'Document', chart: 'Chart', csv: 'Table' }[type] || 'Snippet';
        this.app.canvas.create(type, title, content, type === 'code' ? language : undefined);
    };

    addToolCard(turn, call) {
        const status = el('span', { class: 'tool-status running' }, '');
        const detail = el('div', { class: 'tool-detail' },
            el('div', { class: 'tool-label' }, 'Arguments'),
            el('pre', { class: 'tool-args' }, JSON.stringify(call.arguments, null, 2)),
        );
        const card = el('details', { class: 'tool-card' },
            el('summary', {},
                status,
                el('span', { class: 'tool-name' }, call.name),
                el('span', { class: 'tool-summary' }, argsSummary(call.name, call.arguments)),
                el('span', { class: 'tool-time' }),
            ),
            detail,
        );
        turn.body.append(card);
        this.cards.set(call.id, { card, status, detail, call });
        turn.segment = null;
        return card;
    }

    updateToolCard(id, ev) {
        const entry = this.cards.get(id);
        if (!entry) return;
        entry.card.classList.remove('awaiting');
        entry.card.querySelector('.confirm-box')?.remove();
        entry.status.className = `tool-status ${ev.ok ? 'ok' : 'error'}`;
        entry.card.querySelector('.tool-time').textContent = ev.duration_ms !== undefined ? `${(ev.duration_ms / 1000).toFixed(1)}s` : '';
        entry.detail.querySelector('.tool-result-wrap')?.remove();
        const wrap = el('div', { class: 'tool-result-wrap' }, el('div', { class: 'tool-label' }, ev.ok ? 'Result' : 'Error'));
        // Outside the <details> so it shows while the card is collapsed.
        entry.block?.remove();
        if (ev.blocked) {
            entry.card.classList.add('blocked');
            const events = ev.blocked.events || [];
            const layer = ev.blocked.layer === 'openshell' ? 'NVIDIA OpenShell sandbox' : 'Workbench governance policy';
            entry.block = el('div', { class: 'policy-block' },
                el('div', { class: 'policy-block-title' }, `🛡 Blocked by ${layer}`),
                events.length
                    ? el('ul', {}, events.map(e => el('li', {}, e.summary)))
                    : el('div', {}, ev.blocked.rule || ''),
                el('button', { class: 'btn tiny ghost', onclick: () => this.app.audit.show({ sessionId: this.sessionId }) }, 'View in audit trail'),
            );
            entry.card.after(entry.block);
            if (ev.blocked.layer === 'openshell') this.watchProposals();
        } else if (ev.missing) {
            entry.block = el('div', { class: 'policy-block missing' },
                el('div', { class: 'policy-block-title' }, `🛡 Not available in the OpenShell sandbox: ${ev.missing.join(', ')}`),
                el('div', { class: 'small' }, 'The sandbox has no such tool, so the step could not run. Recorded in the audit trail.'),
            );
            entry.card.after(entry.block);
        }
        if (ev.sql_result) {
            const r = ev.sql_result;
            wrap.append(el('div', { class: 'muted small' }, `${r.rows.length} of ${formatNumber(r.total_rows)} rows`), resultTable(r.columns, r.rows, { maxRows: 100 }));
        } else {
            wrap.append(el('pre', { class: 'tool-result' }, ev.result || ''));
        }
        if (ev.name === 'run_sql' && entry.call.arguments?.sql) {
            wrap.append(el('button', { class: 'btn tiny', onclick: () => this.app.canvas.create('sql', 'Query', entry.call.arguments.sql) }, 'Open query in canvas'));
        }
        entry.detail.append(wrap);
        if (!ev.ok) entry.card.open = true;
    }

    // OpenShell drafts a rule for each blocked connection about 10 s later.
    // Show the drafts so the user can widen this chat's policy, or refuse.
    watchProposals({ once = false } = {}) {
        const sid = this.sessionId;
        if (!sid) return;
        clearTimeout(this.proposalTimer);
        const deadline = Date.now() + 45000;
        const poll = async () => {
            if (this.sessionId !== sid) return;
            try {
                (await api(`api/governance/proposals?session_id=${encodeURIComponent(sid)}`)).forEach(p => this.showProposal(p));
            } catch { /* the sandbox may be gone */ }
            if (!once && Date.now() < deadline) this.proposalTimer = setTimeout(poll, 4000);
        };
        this.proposalTimer = setTimeout(poll, once ? 0 : 3000);
    }

    showProposal(p) {
        if (this.messages.querySelector(`.proposal-card[data-chunk="${CSS.escape(p.chunk)}"]`)) return;
        const sid = this.sessionId;
        const decide = async approve => {
            actions.replaceChildren(el('span', { class: 'muted small' }, approve ? 'Approving…' : 'Rejecting…'));
            try {
                const r = await api('api/governance/proposals/decide', { method: 'POST', body: { session_id: sid, chunk_id: p.chunk, approve } });
                actions.replaceChildren(el('span', { class: r.ok ? 'small' : 'error-box' },
                    r.ok ? (approve ? '✓ Approved for this chat. The blocked request is not retried automatically: ask the agent to try again.'
                                    : '✗ Rejected. The connection stays blocked.')
                         : `Failed: ${r.message}`));
            } catch (e) { actions.replaceChildren(el('span', { class: 'error-box' }, e.message)); }
        };
        const actions = el('div', { class: 'confirm-actions' },
            el('button', { class: 'btn danger', onclick: () => decide(true) }, 'Approve for this chat'),
            el('button', { class: 'btn', onclick: () => decide(false) }, 'Reject'));
        this.messages.append(el('div', { class: 'proposal-card', dataset: { chunk: p.chunk } },
            el('div', { class: 'policy-block-title' }, '🛡 OpenShell proposes a policy change'),
            el('div', {}, p.rationale || p.rule || ''),
            el('div', { class: 'mono small' }, [p.endpoints, p.binaries].filter(Boolean).join(' · ')),
            p.security ? el('div', { class: 'small warn-text' }, `Security notes: ${p.security}`) : null,
            p.prover ? el('div', { class: 'muted small' }, `Prover: ${p.prover}`) : null,
            p.hits ? el('div', { class: 'muted small' }, `Blocked attempts: ${p.hits}`) : null,
            el('div', { class: 'muted small' }, 'Approving adds this rule to this chat\'s sandbox only, and is recorded in the audit trail. Check the destination: planted instructions in data can make the agent ask for exactly this.'),
            actions));
        this.scrollToBottom();
        this.app.notify('OpenShell proposes a policy change');
    }

    showConfirm(ev) {
        const entry = this.cards.get(ev.id);
        if (!entry) return;
        entry.card.classList.add('awaiting');
        entry.card.open = true;
        entry.status.className = 'tool-status awaiting';
        const answer = approve => {
            this.wsSend({ type: 'confirm', id: ev.id, approve });
            box.replaceWith(el('div', { class: 'muted small confirm-box' }, approve ? 'Approved — running…' : 'Denied.'));
        };
        const box = el('div', { class: 'confirm-box' },
            el('div', {}, el('strong', {}, '⚠ Approval needed: '), ev.reason),
            el('div', { class: 'confirm-actions' },
                el('button', { class: 'btn danger', onclick: () => answer(true) }, 'Approve'),
                el('button', { class: 'btn', onclick: () => answer(false) }, 'Deny'),
            ),
        );
        entry.detail.prepend(box);
        this.scrollToBottom();
        this.app.notify(`Approval needed: ${ev.name}`);
    }

    artifactChip(turn, art) {
        turn.body.append(el('button', { class: 'artifact-chip', onclick: () => this.app.canvas.open(art.id) },
            `🎨 ${art.title}`, art.type ? el('span', { class: 'muted small' }, ` · ${art.type}`) : null, el('span', { class: 'chip-open' }, 'Open')));
    }

    finishTurn(turn, stats) {
        turn.footer.innerHTML = '';
        const ctx = stats.context_tokens ? `ctx ${formatNumber(stats.context_tokens)} / ${formatNumber(this.app.options?.num_ctx || 0)}` : null;
        const text = () => [...turn.body.querySelectorAll('.segment')].map(s => s.innerText).join('\n\n');
        turn.footer.append(
            stats.tokens ? el('span', {}, `${formatNumber(stats.tokens)} tokens`) : null,
            stats.tokens_per_s ? el('span', {}, `${stats.tokens_per_s} tok/s`) : null,
            stats.latency_ms ? el('span', {}, `${(stats.latency_ms / 1000).toFixed(1)}s`) : null,
            ctx ? el('span', { title: 'Context window used' }, ctx) : null,
            el('span', { class: 'spacer' }),
            el('button', { class: 'btn tiny ghost', onclick: e => copyText(text(), e.target) }, 'Copy'),
        );
    }

    addRegenerate(turnNode) {
        const footer = turnNode.querySelector('.turn-footer');
        if (!footer || footer.querySelector('.regen')) return;
        footer.append(el('button', { class: 'btn tiny ghost regen', title: 'Regenerate this answer', onclick: () => this.regenerate() }, '↻ Regenerate'));
    }

    // ------------------------------------------------------------ sending

    async submit() {
        const text = this.input.value.trim();
        if (!text && !this.attachments.length) return;
        if (text.startsWith('/') && await this.app.runSlash(text)) {
            this.input.value = '';
            this.input.style.height = 'auto';
            return;
        }
        if (!this.app.modelReady()) {
            toast('Select a model that is ready first.', 'warn');
            return;
        }
        this.input.value = '';
        this.input.style.height = 'auto';
        this.app.updateSlashMenu('');
        if (this.messages.querySelector('.welcome')) this.messages.innerHTML = '';
        const attachments = this.attachments;
        this.attachments = [];
        this.renderAttachments();
        this.messages.append(this.userBubble({ content: text, meta: { display: text, attachments: attachments.map(a => a.name) } }));
        this.dispatch({ type: 'send', message: text, attachments });
    }

    regenerate() {
        if (this.running) return;
        const turns = this.messages.querySelectorAll('.turn');
        const last = turns[turns.length - 1];
        // Remove everything after the last user message.
        const users = [...this.messages.querySelectorAll('.message.user')];
        const lastUser = users[users.length - 1];
        if (!lastUser) return;
        let node = lastUser.nextSibling;
        while (node) { const next = node.nextSibling; node.remove(); node = next; }
        if (last) last.remove();
        this.dispatch({ type: 'regenerate' });
    }

    startEdit(bubble, text) {
        if (this.running) return toast('Stop the current reply first.', 'warn');
        const index = bubble.dataset.index;
        if (index === '' || index === undefined) return toast('Wait for the reply to finish, then edit.', 'warn');
        const ta = el('textarea', { class: 'editor edit-box' }, text);
        const box = el('div', { class: 'edit-wrap' }, ta, el('div', { class: 'editor-actions' },
            el('button', { class: 'btn', onclick: () => box.replaceWith(bubble) }, 'Cancel'),
            el('button', { class: 'btn primary', onclick: () => {
                const message = ta.value.trim();
                if (!message) return;
                let node = box.nextSibling;
                while (node) { const next = node.nextSibling; node.remove(); node = next; }
                box.replaceWith(this.userBubble({ content: message, meta: { display: message } }));
                this.dispatch({ type: 'edit', index: Number(index), message });
            } }, 'Send'),
        ));
        bubble.replaceWith(box);
        ta.focus();
    }

    dispatch(payload) {
        this.ensureSessionId();
        this.setRunning(true);
        this.turn = null;
        this.wsSend({ ...payload, settings: this.app.currentSettings() });
        this.app.onSessionChanged(this.sessionId, { pending: true });
        this.scrollToBottom(true);
    }

    stop() {
        this.wsSend({ type: 'stop' });
    }

    setRunning(running) {
        this.running = running;
        this.sendBtn.textContent = running ? '■ Stop' : 'Send';
        this.sendBtn.classList.toggle('danger', running);
        this.sendBtn.classList.toggle('primary', !running);
    }

    closeSocket() {
        if (this.ws) {
            const ws = this.ws;
            this.ws = null;
            ws.close();
        }
    }

    wsSend(payload) {
        const sessionId = this.ensureSessionId();
        if (this.ws && this.wsSessionId === sessionId && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify(payload));
            return;
        }
        if (this.ws && this.wsSessionId === sessionId && this.ws.readyState === WebSocket.CONNECTING) {
            this.ws.addEventListener('open', () => this.ws.send(JSON.stringify(payload)), { once: true });
            return;
        }
        this.closeSocket();
        const ws = new WebSocket(wsUrl(`ws/chat/${encodeURIComponent(sessionId)}`));
        this.ws = ws;
        this.wsSessionId = sessionId;
        ws.onopen = () => ws.send(JSON.stringify(payload));
        ws.onmessage = e => { if (ws === this.ws) this.onEvent(JSON.parse(e.data)); };
        ws.onclose = () => {
            // A socket replaced by a newer chat must not touch the new reply.
            if (ws !== this.ws) return;
            this.ws = null;
            if (this.running) {
                this.setRunning(false);
                const turn = this.turn || this.newTurn('');
                turn.body.append(el('div', { class: 'error-box' }, 'Connection closed before the reply finished.'));
            }
        };
    }

    // ------------------------------------------------------------ live events

    currentTurn() {
        if (!this.turn) this.turn = this.newTurn(this.app.currentSettings().model_id);
        return this.turn;
    }

    clearStatus(turn) {
        if (turn.status) { turn.status.remove(); turn.status = null; }
    }

    onEvent(ev) {
        const turn = ['start', 'session'].includes(ev.type) ? null : this.currentTurn();
        switch (ev.type) {
            case 'start':
                this.currentTurn();
                if (ev.session) this.app.onSessionUpdated(ev.session);
                break;
            case 'status':
                this.clearStatus(turn);
                turn.status = el('div', { class: 'status-line' }, el('span', { class: 'spinner' }), ev.content);
                turn.body.append(turn.status);
                break;
            case 'step_start':
                turn.segment = null;
                turn.thinking = null;
                break;
            case 'thinking':
                this.clearStatus(turn);
                if (!turn.thinking) turn.thinking = this.addThinking(turn, '', { open: this.app.prefs.showReasoning });
                turn.thinking.textContent += ev.content;
                this.scrollToBottom();
                break;
            case 'token':
                this.clearStatus(turn);
                if (!turn.segment) { turn.segment = this.addSegment(turn); turn.raw = ''; }
                turn.raw += ev.content;
                this.scheduleRender(turn);
                break;
            case 'tool_call':
                this.clearStatus(turn);
                this.flushRender(turn);
                this.addToolCard(turn, ev);
                turn.thinking = null;
                this.scrollToBottom();
                break;
            case 'confirm_request':
                this.showConfirm(ev);
                break;
            case 'tool_result':
                this.updateToolCard(ev.id, ev);
                if (ev.artifact) this.artifactChip(turn, ev.artifact);
                this.scrollToBottom();
                break;
            case 'artifact':
                this.app.canvas.upsert(ev.artifact);
                break;
            case 'notice':
                turn.body.append(el('div', { class: 'notice' }, ev.content));
                break;
            case 'error':
                this.clearStatus(turn);
                this.flushRender(turn);
                turn.body.append(el('div', { class: 'error-box' }, ev.content));
                this.setRunning(false);
                this.syncIndices();
                break;
            case 'done':
                this.clearStatus(turn);
                this.flushRender(turn);
                if (ev.stopped) turn.body.append(el('div', { class: 'notice' }, 'Stopped.'));
                this.finishTurn(turn, ev);
                this.messages.querySelectorAll('.regen').forEach(b => b.remove());
                this.addRegenerate(turn.node);
                this.setRunning(false);
                this.turn = null;
                this.syncIndices();
                if (document.hidden) this.app.notify('Reply finished');
                break;
            case 'session':
                this.app.onSessionUpdated(ev.session);
                document.getElementById('chat-title').textContent = ev.session.title;
                break;
        }
    }

    scheduleRender(turn) {
        if (turn.renderPending) return;
        turn.renderPending = true;
        requestAnimationFrame(() => {
            turn.renderPending = false;
            if (turn.segment) renderMarkdown(turn.segment, turn.raw, { onOpenInCanvas: this.openInCanvas });
            this.scrollToBottom();
        });
    }

    flushRender(turn) {
        if (turn.segment) renderMarkdown(turn.segment, turn.raw, { onOpenInCanvas: this.openInCanvas });
        turn.segment = null;
    }

    // User-message indices let "Edit" truncate history on the server.
    async syncIndices() {
        if (!this.sessionId) return;
        try {
            const session = await api(`api/sessions/${encodeURIComponent(this.sessionId)}`);
            const indices = session.messages.map((m, i) => (m.role === 'user' ? i : -1)).filter(i => i >= 0);
            [...this.messages.querySelectorAll('.message.user')].forEach((b, i) => { b.dataset.index = indices[i] ?? ''; });
        } catch { /* ignore */ }
    }

    scrollToBottom(force = false) {
        const m = this.messages;
        if (force || m.scrollHeight - m.scrollTop - m.clientHeight < 200) m.scrollTop = m.scrollHeight;
    }

    // ------------------------------------------------------------ attachments

    async addFiles(files) {
        for (const file of files) {
            if (file.size > MAX_ATTACHMENT_BYTES) { toast(`${file.name} is larger than 2 MB`, 'warn'); continue; }
            const text = await file.text();
            if (/\u0000/.test(text.slice(0, 1000))) { toast(`${file.name} looks binary; attach text files (CSV, JSON, code, Markdown).`, 'warn'); continue; }
            this.attachments.push({ name: file.name, content: text });
        }
        this.renderAttachments();
    }

    renderAttachments() {
        this.attachList.innerHTML = '';
        this.attachments.forEach((a, i) => this.attachList.append(el('span', { class: 'chip' }, `📎 ${a.name}`,
            el('button', { class: 'chip-x', title: 'Remove', onclick: () => { this.attachments.splice(i, 1); this.renderAttachments(); } }, '×'))));
    }
}
