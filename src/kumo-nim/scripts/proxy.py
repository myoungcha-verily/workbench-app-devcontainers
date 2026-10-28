"""Token-checking reverse proxy in front of the Kumo Relational NIM (which has no authentication of its own).

Accepts the token as X-API-Key (what kumo-relational-client sends) or Authorization: Bearer. /proxy-health is open
and reports whether the NIM answers, for start-up checks.
"""
import hmac
import os

import aiohttp
from aiohttp import web

TOKEN = os.environ["TOKEN"]
UPSTREAM = os.environ["UPSTREAM"]
HOP = {"host", "content-length", "transfer-encoding", "connection", "x-api-key", "authorization"}


def authorized(req):
    got = req.headers.get("X-API-Key") or req.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    return bool(got) and hmac.compare_digest(got, TOKEN)


async def health(req):
    try:
        async with req.app["session"].get(f"{UPSTREAM}/v1/health/ready", timeout=aiohttp.ClientTimeout(total=5)) as r:
            return web.json_response({"nim_status": r.status})
    except Exception as e:  # noqa: BLE001
        return web.json_response({"nim_status": None, "error": type(e).__name__}, status=503)


async def forward(req):
    if not authorized(req):
        return web.json_response({"error": "unauthorized"}, status=401)
    headers = {k: v for k, v in req.headers.items() if k.lower() not in HOP}
    async with req.app["session"].request(req.method, f"{UPSTREAM}{req.rel_url}", headers=headers,
                                          data=await req.read(), timeout=aiohttp.ClientTimeout(total=3600)) as r:
        resp = web.StreamResponse(status=r.status, headers={k: v for k, v in r.headers.items() if k.lower() not in HOP})
        await resp.prepare(req)
        async for chunk in r.content.iter_chunked(1 << 16):
            await resp.write(chunk)
        await resp.write_eof()
        return resp


async def start(app):
    app["session"] = aiohttp.ClientSession()


async def stop(app):
    await app["session"].close()


app = web.Application(client_max_size=2 * 1024 ** 3)
app.on_startup.append(start)
app.on_cleanup.append(stop)
app.router.add_get("/proxy-health", health)
app.router.add_route("*", "/{tail:.*}", forward)
web.run_app(app, host="0.0.0.0", port=8080)
