import json
import os
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

OLLAMA_URL = "http://" + os.environ.get("OLLAMA_HOST", "127.0.0.1:11434").removeprefix("http://")

DEFAULT_OPTIONS = {"num_predict": 4096, "temperature": 0.1}

# A long prompt can take a while before the first streamed byte (model load
# plus prompt processing), so allow 30 minutes between bytes.
_TIMEOUT = httpx.Timeout(connect=10.0, read=1800.0, write=60.0, pool=10.0)


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=OLLAMA_URL, timeout=_TIMEOUT)


async def stream_lines(method: str, path: str, payload: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
    async with client() as c:
        async with c.stream(method, path, json=payload) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode(errors="replace")
                raise RuntimeError(f"Ollama {path} returned {resp.status_code}: {body[:500]}")
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if chunk.get("error"):
                    raise RuntimeError(chunk["error"])
                yield chunk


async def chat_stream(
    model: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    think: Any = None,
    options: Optional[Dict[str, Any]] = None,
) -> AsyncIterator[Dict[str, Any]]:
    payload: Dict[str, Any] = {"model": model, "messages": messages, "stream": True, "options": options or DEFAULT_OPTIONS}
    if tools:
        payload["tools"] = tools
    if think is not None:
        payload["think"] = think
    async for chunk in stream_lines("POST", "/api/chat", payload):
        yield chunk


async def show(model: str) -> Dict[str, Any]:
    async with client() as c:
        resp = await c.post("/api/show", json={"model": model})
        resp.raise_for_status()
        return resp.json()


async def ps() -> List[Dict[str, Any]]:
    async with client() as c:
        resp = await c.get("/api/ps")
        resp.raise_for_status()
        return resp.json().get("models", [])


async def chat(model: str, messages: List[Dict[str, Any]], **kwargs) -> Dict[str, Any]:
    """Non-streaming chat. Returns content, thinking, and token count."""
    content, thinking, tokens = "", "", 0
    async for chunk in chat_stream(model, messages, **kwargs):
        message = chunk.get("message") or {}
        content += message.get("content", "")
        thinking += message.get("thinking", "")
        if chunk.get("done"):
            tokens = chunk.get("eval_count", 0) + chunk.get("prompt_eval_count", 0)
    return {"content": content, "thinking": thinking, "tokens": tokens}


async def warm_up(model: str):
    """Load a model into VRAM and run one short prompt through it."""
    await chat(model, [{"role": "user", "content": "Reply with OK."}])


async def loaded_models() -> List[str]:
    """Models currently resident in memory (`ollama ps`)."""
    async with client() as c:
        resp = await c.get("/api/ps")
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]


async def installed_models() -> List[str]:
    async with client() as c:
        resp = await c.get("/api/tags")
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]


async def is_up() -> bool:
    try:
        async with client() as c:
            return (await c.get("/api/version")).status_code == 200
    except httpx.HTTPError:
        return False
