"""Client for the Workbench MCP server (wb-mcp-server feature, JSON-RPC over HTTP)."""
import itertools
import os
import time
from typing import Any, Dict, List, Optional

import httpx

MCP_URL = os.environ.get("WB_MCP_URL", "http://127.0.0.1:9242")

_ids = itertools.count(1)
# wb commands (BigQuery queries, workflow submissions) can take minutes.
_TIMEOUT = httpx.Timeout(connect=5.0, read=600.0, write=30.0, pool=5.0)


class MCPError(RuntimeError):
    pass


async def _rpc(method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = {"jsonrpc": "2.0", "id": next(_ids), "method": method}
    if params is not None:
        payload["params"] = params
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        resp = await c.post(MCP_URL, json=payload)
        resp.raise_for_status()
        body = resp.json()
    if "error" in body:
        raise MCPError(body["error"].get("message", str(body["error"])))
    return body.get("result", {})


class MCPTools:
    """Caches the server's tool list; the server starts after Workbench login."""

    def __init__(self):
        self.tools: List[Dict[str, Any]] = []
        self.by_name: Dict[str, Dict[str, Any]] = {}
        self.error: str = "not connected yet"
        self._last_attempt = 0.0

    @property
    def connected(self) -> bool:
        return bool(self.tools)

    async def refresh(self, force: bool = False) -> bool:
        if self.tools and not force:
            return True
        # Don't hammer a server that isn't up yet.
        if not force and time.time() - self._last_attempt < 10:
            return False
        self._last_attempt = time.time()
        try:
            result = await _rpc("tools/list")
            self.tools = result.get("tools", [])
            self.by_name = {t["name"]: t for t in self.tools}
            self.error = ""
            return True
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            return False

    async def call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Returns {"text": str, "is_error": bool}."""
        result = await _rpc("tools/call", {"name": name, "arguments": arguments})
        text = "\n".join(
            part.get("text", "") for part in result.get("content", []) if part.get("type") == "text"
        )
        return {"text": text, "is_error": bool(result.get("isError"))}
