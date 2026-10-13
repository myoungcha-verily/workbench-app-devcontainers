"""Token-protected access for the eval-bench harness, plus a guard for direct (non-proxy) API calls.

- /v1/*: pass-through to Ollama's OpenAI-compatible API (127.0.0.1:11434/v1), so external harnesses
  (eval-bench / OpenCode) can use the self-hosted models. Always requires `Authorization: Bearer <token>`.
- /api/*: the UI reaches the backend through the Workbench proxy, which connects from the container network.
  Requests from anywhere else (e.g. another VM on the workspace VPC) must carry the same token; without this,
  any VM on the network could run SQL and tools with this app's credentials.

The token comes from EVAL_API_TOKEN, or from the GCS object in EVAL_TOKEN_GCS (gs://bucket/path), read at
startup. With no token configured, /v1 is disabled and direct /api calls are refused.
"""
import hmac
import ipaddress
import os
import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

OLLAMA_V1 = "http://127.0.0.1:11434/v1"
# Docker bridge networks (the Workbench proxy and in-container calls arrive from these).
_INTERNAL = [ipaddress.ip_network(n) for n in ("127.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "::1/128")]

router = APIRouter()
_token = None
_last_load = 0.0


def load_token():
    global _token, _last_load
    _last_load = time.time()
    _token = os.environ.get("EVAL_API_TOKEN") or None
    uri = os.environ.get("EVAL_TOKEN_GCS", "")
    if not _token and uri.startswith("gs://"):
        try:
            from google.cloud import storage
            bucket, _, path = uri[5:].partition("/")
            _token = storage.Client().bucket(bucket).blob(path).download_as_text().strip() or None
        except Exception as e:  # app still works through the proxy; /v1 stays disabled
            print(f"eval_api: could not read token from {uri}: {e}", flush=True)
    print(f"eval_api: external access {'enabled' if _token else 'disabled (no token)'}", flush=True)


def _authorized(request: Request) -> bool:
    if not _token and time.time() - _last_load > 60:  # token may be created after the app starts
        load_token()
    if not _token:
        return False
    auth = request.headers.get("authorization", "")
    return auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip(), _token)


def _internal(request: Request) -> bool:
    try:
        ip = ipaddress.ip_address(request.client.host)
    except (ValueError, AttributeError):
        return False
    return any(ip in net for net in _INTERNAL)


# Read-only status routes stay open (health checks may not come through the container network).
_OPEN = {"/api/health", "/api/status", "/api/models", "/api/hardware"}


async def guard(request: Request, call_next):
    """Middleware: direct /api calls from outside the container network need the token."""
    path = request.url.path
    if path.startswith("/api/") and path not in _OPEN and not _internal(request) and not _authorized(request):
        return JSONResponse({"detail": "direct API access requires a bearer token"}, status_code=403)
    return await call_next(request)


@router.api_route("/v1/{path:path}", methods=["GET", "POST"])
async def openai_passthrough(path: str, request: Request):
    if not _authorized(request):
        return JSONResponse({"error": {"message": "invalid or missing bearer token"}}, status_code=401)
    body = await request.body()
    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=1800.0, write=60.0, pool=10.0))
    upstream = client.build_request(request.method, f"{OLLAMA_V1}/{path}", content=body,
                                    headers={"content-type": request.headers.get("content-type", "application/json")})
    resp = await client.send(upstream, stream=True)

    async def relay():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(relay(), status_code=resp.status_code,
                             media_type=resp.headers.get("content-type", "application/json"))
