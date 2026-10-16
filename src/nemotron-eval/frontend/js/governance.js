// Makes governance visible in the chat: the mode banner, where each tool call
// ran, plain-language policy decisions with the source of the instruction,
// risk flags for ungoverned chats, and the Sandbox panel.
import { api, el, formatDate } from './api.js';
import { escapeHtml } from './markdown.js';

const TOKEN = /ya29\.[\w.-]{20,}/g;
const URL_HOST = /https?:\/\/([a-z0-9][a-z0-9.-]*\.[a-z]{2,})/gi;
const UNRESOLVED = /Name or service not known|Could not resolve host|getaddrinfo|nodename nor servname|Temporary failure in name resolution/i;
const CATALOG = new Set(['describe_table', 'list_tables', 'find_tools', 'list_skills', 'read_skill']);
const CANVAS = new Set(['create_artifact', 'update_artifact', 'read_artifact']);

// Access tokens are masked on screen: demos get recorded.
export function maskTokens(text) {
    return String(text ?? '').replace(TOKEN, t => `${t.slice(0, 9)}…[access token, masked on screen]`);
}

export function placeOf(name, governed) {
    if (CANVAS.has(name)) return { label: 'Canvas', cls: 'neutral', title: 'Writes to this chat\'s canvas in the app' };
    if (CATALOG.has(name)) return { label: 'App · read-only', cls: 'neutral', title: 'Read-only lookup done by the app' };
    if (name === 'run_shell' || name === 'run_python') {
        return governed
            ? { label: 'OpenShell sandbox', cls: 'gov', title: 'Ran inside this chat\'s NVIDIA OpenShell sandbox: no network of its own, no real credentials' }
            : { label: 'App container · your credentials', cls: 'risk', title: 'Ran directly in the app as you: your Workbench credentials, open internet' };
    }
    if (name === 'run_sql') {
        return governed
            ? { label: 'SQL broker · SELECT only', cls: 'gov', title: 'Checked by the SQL broker: SELECT only, allowlisted datasets, row and byte caps' }
            : { label: 'BigQuery · as you', cls: 'risk', title: 'Ran with your BigQuery permissions, including writes and exports' };
    }
    return governed
        ? { label: 'Workbench API · read-only', cls: 'gov', title: 'Governed chats may use read-only Workbench tools only' }
        : { label: 'Workbench API · as you', cls: 'risk', title: 'Any Workbench tool, with your permissions' };
}

function hostsIn(text) {
    return [...new Set([...String(text ?? '').matchAll(URL_HOST)].map(m => m[1].toLowerCase()))];
}

// Where did an address come from? The user's own messages first (not an
// injection), then earlier tool results in this chat.
export function findSource(chat, host, excludeId) {
    if (!host) return null;
    const needle = host.toLowerCase();
    for (const bubble of chat.messages.querySelectorAll('.message.user .bubble-text')) {
        if (bubble.textContent.toLowerCase().includes(needle)) return { kind: 'user' };
    }
    for (const [id, entry] of chat.cards) {
        if (id === excludeId || !entry.result) continue;
        const text = entry.result;
        const at = text.toLowerCase().indexOf(needle);
        if (at < 0) continue;
        const start = Math.max(0, text.lastIndexOf('\n\n', at) + 2, at - 260);
        const end = Math.min(text.length, at + needle.length + 200);
        return { kind: 'tool', name: entry.call.name, args: entry.call.arguments, snippet: text.slice(start, end).trim(), host };
    }
    return null;
}

function sourceBox(source) {
    if (!source) return null;
    if (source.kind === 'user') return el('div', { class: 'small muted' }, 'You mentioned this address in your own message.');
    const what = { describe_table: 'the table description', list_tables: 'the table list', run_sql: 'query results', read_skill: 'a skill file' }[source.name] || `the result of ${source.name}`;
    const arg = source.args?.table || source.args?.dataset || source.args?.sql || source.args?.command || '';
    const html = escapeHtml(maskTokens(source.snippet)).replace(new RegExp(escapeHtml(source.host).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'), m => `<mark>${m}</mark>`);
    return el('div', { class: 'source-box' },
        el('div', { class: 'small' }, el('strong', {}, 'Where this came from: '),
            `the address appears in ${what}`, arg ? el('code', {}, ` ${String(arg).slice(0, 80)}`) : null,
            ', which the agent read earlier in this chat. Text in data can carry instructions.'),
        el('blockquote', { class: 'source-snippet', html: `…${html}…` }));
}

const PROGRAMS = { 'python3.11': 'Python', python3: 'Python', python: 'Python', curl: 'curl', wget: 'wget', bash: 'shell' };

function parseDecision(e) {
    // NET:OPEN target: "/usr/local/bin/python3.11(41) -> host:443"; HTTP:<M> target: "POST http://host:8080/path"
    const net = /^(\S+?)(?:\(\d+\))?\s*->\s*([^\s:]+):(\d+)/.exec(e.target || '');
    if (net) {
        const prog = net[1].split('/').pop();
        return { program: PROGRAMS[prog] || prog, host: net[2], port: net[3], text: `connect to ${net[2]}${net[3] === '443' ? '' : `:${net[3]}`}` };
    }
    const http = /^(\w+)\s+(https?:\/\/[^\s]+)/.exec(e.target || '');
    if (http) {
        const host = new URL(http[2]).hostname;
        return { program: null, host, text: `send ${http[1]} ${http[2]}` };
    }
    return { program: null, host: null, text: e.target || e.kind };
}

// OpenShell's reason codes, in words. L7 denials carry the request after the code.
function reasonText(reason) {
    if (!reason) return 'Denied by policy.';
    if (/no matching policy|transparent_tcp_policy_denied/.test(reason)) return 'This destination is not in the chat\'s policy.';
    if (/not permitted by policy/.test(reason)) return 'The policy allows this host, but not this request.';
    return `Denied: ${reason}.`;
}

// The card shown under a tool call the policy stopped.
export function blockedCard(chat, entry, blocked) {
    if (blocked.layer !== 'openshell') {
        return el('div', { class: 'policy-block' },
            el('div', { class: 'policy-block-title' }, '🛡 Blocked by Workbench governance policy'),
            el('div', {}, blocked.rule || ''),
            el('div', { class: 'block-links' },
                el('button', { class: 'btn tiny ghost', onclick: () => chat.app.audit.show({ sessionId: chat.sessionId }) }, 'View in audit trail')));
    }
    const events = (blocked.events || []).map(e => ({ ...e, ...parseDecision(e) }));
    const source = events.map(e => findSource(chat, e.host, entry.call.id)).find(Boolean);
    return el('div', { class: 'policy-block' },
        el('div', { class: 'policy-block-title' }, '🛡 Blocked by NVIDIA OpenShell'),
        events.map(e => el('div', { class: 'block-line' },
            `The agent's ${e.program ? `${e.program} code` : 'code'} tried to ${e.text}. `,
            el('strong', {}, reasonText(e.reason)),
            ' Nothing was sent.')),
        sourceBox(source),
        el('details', { class: 'small' }, el('summary', {}, 'Technical detail'),
            el('pre', { class: 'tool-result' }, events.map(e => e.raw || e.summary).join('\n'))),
        el('div', { class: 'block-links' },
            el('button', { class: 'btn tiny ghost', onclick: () => chat.app.audit.show({ sessionId: chat.sessionId }) }, 'View in audit trail'),
            el('button', { class: 'btn tiny ghost', onclick: () => chat.app.sandbox.open() }, 'Sandbox panel')));
}

export function missingCard(missing) {
    return el('div', { class: 'policy-block missing' },
        el('div', { class: 'policy-block-title' }, `🛡 Not available in the OpenShell sandbox: ${missing.join(', ')}`),
        el('div', { class: 'small' }, 'The sandbox does not contain this program, so the step could not run. Recorded in the audit trail.'));
}

// What happened that a governed chat would have stopped. Heuristics, so the
// card says so.
export function riskCards(chat, entry, ev, governed) {
    const cards = [];
    const result = String(ev.result ?? '');
    if (TOKEN.test(result)) {
        TOKEN.lastIndex = 0;
        cards.push(el('div', { class: 'risk-card' },
            el('div', { class: 'risk-title' }, '⚠ A real Google access token was shown to the model'),
            el('div', { class: 'small' }, governed
                ? 'This should not happen in a governed chat. Check the sandbox configuration.'
                : 'Ungoverned code runs with your Workbench credentials. The agent can read this token and send it anywhere; it stays valid for up to an hour. It is masked on screen only.'),
            el('div', { class: 'risk-note' }, 'Detected by this demo app. Ungoverned mode enforces nothing.')));
    }
    if (!governed && ['run_shell', 'run_python'].includes(entry.call.name)) {
        const code = entry.call.arguments?.command || entry.call.arguments?.code || '';
        for (const host of hostsIn(code)) {
            const source = findSource(chat, host, entry.call.id);
            if (!source || source.kind === 'user') continue;
            cards.push(el('div', { class: 'risk-card' },
                el('div', { class: 'risk-title' }, `⚠ The agent sent a request to ${host}, an address it found in the data`),
                el('div', { class: 'small' }, 'Nothing checked this request. ',
                    UNRESOLVED.test(result) ? el('strong', {}, 'It failed only because this host does not exist; a real attacker\'s server would have received the data.') : null),
                sourceBox(source),
                el('div', { class: 'risk-note' }, 'Detected by this demo app. Ungoverned mode enforces nothing.')));
        }
    }
    return cards;
}

// ------------------------------------------------------------ mode banner

export function renderBanner(chat, governed) {
    const box = document.getElementById('mode-banner');
    if (!box) return;
    const blocked = chat.messages.querySelectorAll('.tool-card.blocked').length;
    const missing = chat.messages.querySelectorAll('.policy-block.missing').length;
    const risks = chat.messages.querySelectorAll('.risk-card').length;
    box.className = `mode-banner ${governed ? 'gov' : 'risk'}`;
    // replaceChildren would print a null as the text "null".
    box.replaceChildren(...[
        el('span', { class: 'mode-title' }, governed ? '🛡 Governed by NVIDIA OpenShell' : '⚠ Ungoverned'),
        el('span', { class: 'mode-text' }, governed
            ? 'Code runs in this chat\'s sandbox · network: SQL broker and read-only storage only · no real credentials inside · every action logged'
            : 'Code runs in the app with your Workbench credentials and open internet. Nothing is checked or logged.'),
        el('span', { class: 'spacer' }),
        governed && (blocked || missing) ? el('span', { class: 'mode-count' }, `${blocked} blocked${missing ? ` · ${missing} not available` : ''}`) : null,
        !governed && risks ? el('span', { class: 'mode-count risk' }, `${risks} risk${risks > 1 ? 's' : ''} flagged`) : null,
        el('button', { class: 'btn tiny', onclick: () => chat.app.sandbox.toggle() }, governed ? 'Sandbox panel' : 'What is exposed?'),
    ].filter(Boolean));
}

// ------------------------------------------------------------ sandbox panel

const ICONS = { deny: '⛔', fail: '◌', allow: '✓', approve: '👤✓', reject: '👤✗', info: '·' };

function eventText(e) {
    if (e.action === 'exec') return `ran ${String(e.args?.command || '').split('\n').find(l => l.trim()) || 'code'}`.slice(0, 140);
    if (e.action === 'broker_sql') return `SQL broker ${e.decision === 'allow' ? 'answered' : 'refused'}: ${String(e.args?.sql || '').replace(/\s+/g, ' ').slice(0, 90)}`;
    if (e.action === 'missing_tool') return e.detail;
    if (e.action === 'policy_rule') return e.detail;
    if (e.action === 'sandbox') return e.detail;
    if (e.layer === 'openshell' && e.decision === 'deny') return `blocked ${e.detail.replace(/^\S+\s+/, '').replace(/\/\S*\/(\S+?)\(\d+\)/, '$1')}`;
    return `${e.action}${e.decision === 'deny' ? ` refused: ${e.detail}` : ''}`;
}

export class SandboxPanel {
    constructor(app) {
        this.app = app;
        this.node = document.getElementById('sandbox-panel');
        this.body = this.node.querySelector('.sandbox-body');
        this.node.querySelector('.sandbox-close').addEventListener('click', () => this.close());
        document.getElementById('sandbox-btn').addEventListener('click', () => this.toggle());
    }

    get isOpen() { return document.body.classList.contains('sandbox-open'); }
    open() { document.body.classList.add('sandbox-open'); this.refresh(); }
    close() { document.body.classList.remove('sandbox-open'); clearTimeout(this.timer); }
    toggle() { this.isOpen ? this.close() : this.open(); }

    schedule() {
        clearTimeout(this.timer);
        if (this.isOpen) this.timer = setTimeout(() => this.refresh(), this.app.chat.running ? 2000 : 15000);
    }

    async refresh() {
        if (!this.isOpen) return;
        const governed = this.app.currentSettings().governed;
        const sid = this.app.chat.sessionId;
        try {
            if (!governed) {
                this.body.replaceChildren(this.ungoverned());
            } else {
                const [info, events] = await Promise.all([
                    api(`api/governance/session?session_id=${encodeURIComponent(sid || 'none')}`),
                    sid ? api(`api/audit?limit=60&session_id=${encodeURIComponent(sid)}`) : Promise.resolve([]),
                ]);
                this.body.replaceChildren(this.governed(info, events, sid));
            }
        } catch (e) {
            this.body.replaceChildren(el('div', { class: 'error-box' }, `Could not load the sandbox status: ${e.message}`));
        }
        this.schedule();
    }

    governed(info, events, sid) {
        const ok = info.available;
        const sb = info.sandbox;
        const expires = info.status?.storage_token_expires;
        return el('div', { class: 'sandbox-sections' },
            el('section', {},
                el('h4', {}, el('span', { class: `dot ${ok ? 'ok' : 'bad'}` }), ok ? ' OpenShell gateway connected' : ' OpenShell unavailable'),
                ok ? null : el('div', { class: 'error-box small' }, info.status?.error || info.status?.gateway || ''),
                el('div', { class: 'kv' }, el('span', {}, 'Sandbox'), el('code', {}, sid ? sb.name : '—'),
                    el('span', {}, 'State'), el('span', {}, !sid ? 'starts with the first message' : sb.created ? 'running' : 'created when the agent first runs code'),
                    el('span', {}, 'Image'), el('code', {}, sb.image),
                    expires ? el('span', {}, 'Storage token') : null,
                    expires ? el('span', {}, `read-only; valid until ${formatDate(expires * 1000)}, renewed automatically`) : null)),
            el('section', {}, el('h4', {}, 'Isolation'), el('ul', {}, info.isolation.map(t => el('li', {}, t)))),
            el('section', {}, el('h4', {}, 'Policy: what this chat may reach'),
                el('ul', { class: 'policy-list' },
                    info.allowed.map(a => el('li', {}, el('strong', {}, a.what), el('div', { class: 'small' }, a.detail), el('div', { class: 'small muted' }, `${a.scope} · program: ${a.program}`))),
                    info.approved.map(a => el('li', { class: 'approved' }, el('strong', {}, `Approved in this chat: ${a.endpoints || a.rule}`), el('div', { class: 'small muted' }, a.binaries || a.binary || ''))),
                    el('li', { class: 'denied' }, el('strong', {}, 'Everything else: blocked and logged'), el('div', { class: 'small muted' }, 'including the internet, the metadata server and the app\'s other endpoints')))),
            el('section', {}, el('h4', {}, 'Live activity ', el('span', { class: 'muted small' }, this.app.chat.running ? '(updating)' : '')),
                events.length
                    ? el('ul', { class: 'activity' }, events.filter(e => e.action !== 'storage_token').slice(0, 40).map(e => el('li', { class: e.decision },
                        el('span', { class: 'act-icon' }, ICONS[e.decision] || '·'),
                        el('span', { class: 'act-time' }, new Date(e.ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })),
                        el('span', { class: 'act-text' }, maskTokens(eventText(e))))))
                    : el('div', { class: 'muted small' }, 'Nothing yet. Decisions appear here as the agent works.'),
                el('button', { class: 'btn tiny ghost', onclick: () => this.app.audit.show({ sessionId: sid }) }, 'Full audit trail')));
    }

    ungoverned() {
        return el('div', { class: 'sandbox-sections' },
            el('section', { class: 'exposed' },
                el('h4', {}, '⚠ This chat is ungoverned'),
                el('p', { class: 'small' }, 'There is no sandbox. This is how most coding agents run today.'),
                el('div', { class: 'kv' },
                    el('span', {}, 'Code runs'), el('span', {}, 'in the app container, as you'),
                    el('span', {}, 'Network'), el('span', {}, 'unrestricted: any website, the metadata server'),
                    el('span', {}, 'Credentials'), el('span', {}, 'your Workbench credentials: gcloud, bq, gsutil, wb'),
                    el('span', {}, 'Data'), el('span', {}, 'every dataset and bucket you can access, with write and export rights'),
                    el('span', {}, 'Logging'), el('span', {}, 'none beyond the app\'s console log'))),
            el('p', { class: 'small muted' }, 'Warnings in the chat are heuristics from this demo app, not enforcement. Switch the Policy to Governed to run the same request in an OpenShell sandbox.'));
    }
}
