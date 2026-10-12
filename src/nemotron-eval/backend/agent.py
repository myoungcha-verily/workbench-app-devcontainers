"""The agent loop: stream a model step, run its tool calls, repeat."""
import asyncio
import os
import time
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import context
import ollama_client
import sqlrun
import tools
from catalog import Catalog
from mcp_client import MCPTools
from models import ChatMessage, ChatSession, ChatSettings
from store import Store

NUM_CTX = int(os.environ.get("OLLAMA_CONTEXT_LENGTH", "65536"))
# Output budget per step and maximum tool-calling steps per turn.
# Lightning's reasoning often runs 4-5K tokens and up to ~15K (eval-bench),
# so budgets below 16K cut answers off.
EFFORT = {
    "quick": {"num_predict": 4096, "max_steps": 6},
    "balanced": {"num_predict": 16384, "max_steps": 15},
    "thorough": {"num_predict": 32768, "max_steps": 25},
}
# Consecutive failed tool calls before the model is told to step back.
MAX_CONSECUTIVE_ERRORS = 3
TOOL_RESULT_CHARS_MODEL = 12000
TOOL_RESULT_CHARS_UI = 20000
# Rough chars-per-token used to keep history inside the context window.
CHARS_PER_TOKEN = 3.5

Emit = Callable[[Dict[str, Any]], Awaitable[None]]
Confirm = Callable[[str, str, Dict[str, Any], str], Awaitable[bool]]


def think_param(reasoning: str, values: List[Any]) -> Any:
    """Map the UI reasoning setting onto what this model accepts for `think`."""
    if not values:
        return None
    if reasoning == "off":
        return False if False in values else None
    if reasoning in values:
        return reasoning
    if reasoning == "on" or True in values:
        return True if True in values else ("medium" if "medium" in values else None)
    return None


def describe_error(e: Exception) -> str:
    import httpx

    if isinstance(e, httpx.ReadTimeout):
        return ("Timed out waiting for the model to respond. On a GPU this usually means it is still loading; "
                "try again in a minute.")
    if isinstance(e, httpx.ConnectError):
        return "Could not reach Ollama. It may be restarting; try again shortly."
    return str(e) or type(e).__name__


def to_ollama_messages(messages: List[ChatMessage]) -> List[Dict[str, Any]]:
    out = []
    for m in messages:
        if m.role == "tool":
            out.append({"role": "tool", "content": m.content, "tool_name": m.tool_name or ""})
        elif m.role == "assistant":
            msg: Dict[str, Any] = {"role": "assistant", "content": m.content}
            if m.tool_calls:
                msg["tool_calls"] = [{"function": {"name": c["name"], "arguments": c["arguments"]}} for c in m.tool_calls]
            out.append(msg)
        else:
            out.append({"role": m.role, "content": m.content})
    return out


def fit_history(system: str, messages: List[ChatMessage], budget_tokens: int) -> List[ChatMessage]:
    """Drop the oldest turns until the prompt fits, always starting at a user message."""
    budget_chars = int(budget_tokens * CHARS_PER_TOKEN) - len(system)
    sizes = [len(m.content) + len(str(m.tool_calls or "")) for m in messages]
    start = 0
    while start < len(messages) - 1 and sum(sizes[start:]) > budget_chars:
        start += 1
        while start < len(messages) - 1 and messages[start].role != "user":
            start += 1
    return messages[start:]


class Turn:
    def __init__(
        self,
        session: ChatSession,
        settings: ChatSettings,
        store: Store,
        mcp: MCPTools,
        catalog: Catalog,
        model_info: Dict[str, Any],
        emit: Emit,
        confirm: Confirm,
    ):
        self.session = session
        self.settings = settings
        self.store = store
        self.mcp = mcp
        self.catalog = catalog
        self.model_info = model_info
        # Loop guard: identical calls that already failed, and the error streak.
        self.failed_calls: Dict[str, str] = {}
        self.error_streak = 0
        self.emit = emit
        self.confirm = confirm
        self.partial = ""
        self.partial_thinking = ""

    def _options(self) -> Dict[str, Any]:
        effort = EFFORT.get(self.settings.effort, EFFORT["balanced"])
        options: Dict[str, Any] = {"num_ctx": NUM_CTX, "num_predict": self.settings.max_tokens or effort["num_predict"]}
        if self.settings.temperature is not None:
            options["temperature"] = self.settings.temperature
        return options

    def _tools_enabled(self) -> bool:
        preset = context.PRESETS.get(self.settings.preset, context.PRESETS["workbench"])
        supports = "tools" in self.model_info.get("capabilities", ["tools"])
        return bool(preset["tools"]) and self.settings.tools and supports

    async def run(self):
        model = self.settings.model_id
        effort = EFFORT.get(self.settings.effort, EFFORT["balanced"])
        use_tools = self._tools_enabled()
        if use_tools:
            await self.mcp.refresh()
            if not self.mcp.connected:
                await self.emit({"type": "notice", "content": f"Workbench tools are not available yet ({self.mcp.error}); only local tools can be used."})
        system = context.system_prompt(self.settings.preset, self.settings.system_prompt, use_tools)
        think = think_param(self.settings.reasoning, self.model_info.get("thinking_values", []))

        started = time.time()
        eval_tokens, eval_ns, last_prompt_tokens = 0, 0, 0
        stopped = False
        try:
            for step in range(effort["max_steps"]):
                history = fit_history(system, self.session.messages, int(NUM_CTX * 0.8))
                messages = [{"role": "system", "content": system}] + to_ollama_messages(history)
                schemas = tools.tool_schemas(self.mcp.by_name, set(self.session.active_tools)) if use_tools else None

                content, thinking, calls, final = await self._stream_step(model, messages, schemas, think)
                eval_tokens += final.get("eval_count", 0)
                eval_ns += final.get("eval_duration", 0)
                last_prompt_tokens = final.get("prompt_eval_count", 0) + final.get("eval_count", 0)

                self.session.messages.append(ChatMessage(
                    role="assistant", content=content, model_id=model, thinking=thinking or None,
                    tool_calls=calls or None,
                ))
                self.partial = self.partial_thinking = ""
                self._save()

                if not calls:
                    break
                for call in calls:
                    await self._run_tool(call)
                self._save()
            else:
                note = f"Stopped after {effort['max_steps']} tool steps (effort: {self.settings.effort}). Ask me to continue or raise the effort."
                self.session.messages.append(ChatMessage(role="assistant", content=note, model_id=model, meta={"notice": True}))
                await self.emit({"type": "notice", "content": note})
        except asyncio.CancelledError:
            stopped = True
            if self.partial or self.partial_thinking:
                self.session.messages.append(ChatMessage(
                    role="assistant", content=self.partial, thinking=self.partial_thinking or None,
                    model_id=model, meta={"stopped": True},
                ))
        except Exception as e:
            await self.emit({"type": "error", "content": describe_error(e), "model_id": model})
            self._save()
            return

        latency_ms = int((time.time() - started) * 1000)
        tps = round(eval_tokens / (eval_ns / 1e9), 1) if eval_ns else None
        print(f"[agent] {self.session.id} {model} turn done in {latency_ms}ms, {eval_tokens} tokens, "
              f"{tps} tok/s, context {last_prompt_tokens}{' (stopped)' if stopped else ''}", flush=True)
        # Stats go on the last assistant message so reloaded chats show them.
        for m in reversed(self.session.messages):
            if m.role == "assistant":
                m.tokens, m.latency_ms = eval_tokens, latency_ms
                m.meta = {**m.meta, "tokens_per_s": tps, "context_tokens": last_prompt_tokens}
                break
        self._save()
        await self.emit({
            "type": "done", "model_id": model, "tokens": eval_tokens, "latency_ms": latency_ms,
            "tokens_per_s": tps, "context_tokens": last_prompt_tokens, "num_ctx": NUM_CTX, "stopped": stopped,
        })

    async def _stream_step(self, model, messages, schemas, think) -> Tuple[str, str, List[Dict[str, Any]], Dict[str, Any]]:
        content, thinking, calls, final = "", "", [], {}
        await self.emit({"type": "step_start"})
        async for chunk in ollama_client.chat_stream(model, messages, tools=schemas, think=think, options=self._options()):
            msg = chunk.get("message") or {}
            if msg.get("thinking"):
                thinking += msg["thinking"]
                self.partial_thinking = thinking
                await self.emit({"type": "thinking", "content": msg["thinking"]})
            if msg.get("content"):
                content += msg["content"]
                self.partial = content
                await self.emit({"type": "token", "content": msg["content"]})
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                args = fn.get("arguments") or {}
                if not isinstance(args, dict):
                    args = {"input": args}
                calls.append({"id": tc.get("id") or "call_" + uuid.uuid4().hex[:8], "name": fn.get("name", ""), "arguments": args})
            if chunk.get("done"):
                final = chunk
        return content, thinking, calls, final

    async def _run_tool(self, call: Dict[str, Any]):
        name, args, call_id = call["name"], call["arguments"], call["id"]
        await self.emit({"type": "tool_call", "id": call_id, "name": name, "arguments": args})
        started = time.time()
        extra: Dict[str, Any] = {}
        signature = f"{name}:{tools.dumps(args)}"
        repeated = signature in self.failed_calls
        try:
            if repeated:
                # eval-bench: the same broken query was re-run up to 25 times.
                raise RuntimeError(
                    "This exact call already failed with:\n" + self.failed_calls[signature][:1500]
                    + "\nDo not repeat it. Change the arguments or try a different approach."
                )
            reason = tools.needs_confirmation(name, args)
            if name == "run_sql" and not reason:
                info = await sqlrun.check(args.get("sql", ""))
                if info["destructive"]:
                    reason = f"this {info['statement_type']} statement deletes data"
            if reason and not await self.confirm(call_id, name, args, reason):
                text, is_error = "The user denied this action. Do not retry it; ask the user how to proceed.", True
            else:
                text, is_error, extra = await self._execute(name, args, confirmed=bool(reason))
        except Exception as e:
            text, is_error = f"Error: {sqlrun.friendly_error(e)}", True
            if name == "run_sql" and not repeated:
                text += await self.catalog.explain_sql_error(str(args.get("sql", "")), text)

        if is_error:
            self.failed_calls.setdefault(signature, text)
            self.error_streak += 1
            if self.error_streak >= MAX_CONSECUTIVE_ERRORS:
                text += ("\n\nSeveral tool calls in a row have failed. Stop and reconsider: check names with "
                         "describe_table / list_tables, simplify, or explain the problem to the user.")
        else:
            self.error_streak = 0

        duration_ms = int((time.time() - started) * 1000)
        # One line per tool call, so `wb app logs` shows what the agent did.
        print(f"[agent] {self.session.id} {self.settings.model_id} {name} "
              f"{'error' if is_error else 'ok'} {duration_ms}ms {tools.dumps(args)[:160]}", flush=True)
        self.session.messages.append(ChatMessage(
            role="tool", content=tools.truncate(text, TOOL_RESULT_CHARS_MODEL), tool_name=name, tool_call_id=call_id,
            meta={"is_error": is_error, "duration_ms": duration_ms, **({"artifact_id": extra["artifact"]["id"]} if "artifact" in extra else {})},
        ))
        await self.emit({
            "type": "tool_result", "id": call_id, "name": name, "ok": not is_error, "duration_ms": duration_ms,
            "result": tools.truncate(text, TOOL_RESULT_CHARS_UI), **extra,
        })
        if "artifact" in extra:
            await self.emit({"type": "artifact", "artifact": extra["artifact"]})

    async def _execute(self, name: str, args: Dict[str, Any], confirmed: bool) -> Tuple[str, bool, Dict[str, Any]]:
        sid = self.session.id
        if name == "find_tools":
            enabled = set(tools.CORE_MCP_TOOLS) | set(self.session.active_tools)
            candidates = {k: v for k, v in self.mcp.by_name.items() if k not in enabled}
            found = tools.search_tools(candidates, str(args.get("query", "")))
            if not found:
                return "No matching tools. Try other keywords.", False, {}
            self.session.active_tools = sorted(set(self.session.active_tools) | {f["name"] for f in found})
            lines = "\n".join(f"- {f['name']}: {f['description']}" for f in found)
            return f"Enabled these tools for this chat:\n{lines}", False, {}
        if name == "list_tables":
            full, tables = await self.catalog.list_tables(str(args.get("dataset", "")))
            if not tables:
                return f"`{full}` has no tables.", False, {}
            lines = "\n".join(f"- `{full}.{t['table']}`" + (f" ({t['type'].lower()})" if t["type"] != "TABLE" else "") for t in tables)
            return f"Tables in `{full}`:\n{lines}", False, {}
        if name == "describe_table":
            return await self.catalog.describe(str(args.get("table", ""))), False, {}
        if name == "run_sql":
            result = await sqlrun.run(args.get("sql", ""), int(args.get("max_rows") or 50), confirmed=confirmed)
            preview = {**result, "rows": result["rows"][:100]}
            return sqlrun.as_text({**result, "rows": result["rows"][:500]}), False, {"sql_result": preview}
        if name == "list_skills":
            skills = context.list_skills()
            return ("\n".join(skills) if skills else "No skills are installed yet."), False, {}
        if name == "read_skill":
            return context.read_skill(str(args.get("name", ""))), False, {}
        if name == "create_artifact":
            kind = str(args.get("type", "markdown")).lower()
            if kind not in tools.ARTIFACT_TYPES:
                return f"Unknown artifact type {kind!r}; use one of {tools.ARTIFACT_TYPES}", True, {}
            art = self.store.create_artifact(sid, kind, str(args.get("title") or kind), str(args.get("content", "")), args.get("language"))
            return f"Created artifact {art.id} (version 1): {art.title}", False, {"artifact": art.model_dump(mode="json")}
        if name == "update_artifact":
            art = self.store.update_artifact(str(args.get("artifact_id", "")), str(args.get("content", "")), args.get("title"))
            return f"Updated artifact {art.id} to version {art.version}", False, {"artifact": art.model_dump(mode="json")}
        if name == "read_artifact":
            art = self.store.get_artifact(str(args.get("artifact_id", "")))
            if not art:
                return "No such artifact.", True, {}
            return f"{art.type} artifact {art.id} v{art.version}: {art.title}\n\n{art.content}", False, {}
        if name in self.mcp.by_name:
            result = await self.mcp.call(name, args)
            return result["text"] or "(no output)", result["is_error"], {}
        return f"Unknown tool {name!r}. Use find_tools to discover available tools.", True, {}

    def _save(self):
        self.session.updated_at = datetime.now()
        self.store.save_session(self.session)
