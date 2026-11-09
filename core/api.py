"""Small HTTP API so a CLI on another machine can talk to the assistant (see buddyctl.py).

Off unless API_TOKEN is set. Every request needs `Authorization: Bearer <API_TOKEN>`.

    GET  /health            -> {"ok": true, "model": "...", "skills": [...]}
    POST /ask {"text": ..}  -> {"reply": ..}   the message goes through the assistant like a Discord DM

Skills add their own routes with `api.add_routes([...])` from on_start (the delegate skill adds /tasks).
"""
import hmac

from aiohttp import web

import config
from core import agent, skills
from core.skills import Forward
from core.tools import ToolContext

_routes: dict[tuple[str, str], web.RouteDef] = {}
_runner: web.AppRunner | None = None


def add_routes(routes: list[web.RouteDef]) -> None:
    for r in routes:
        _routes[(r.method, r.path)] = r


@web.middleware
async def _auth(request: web.Request, handler):
    header = request.headers.get("Authorization", "")
    token = header[7:] if header.startswith("Bearer ") else ""
    if not config.API_TOKEN or not hmac.compare_digest(token, config.API_TOKEN):
        return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


async def _health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "model": config.LLM_MODEL, "skills": list(skills.SKILLS)})


async def _ask(request: web.Request) -> web.Response:
    data = await request.json()
    text = (data.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "text is required"}, status=400)
    ctx = ToolContext(conversation=data.get("conversation") or "main", frontend="api", speaker=config.owner())
    result = await skills.run_command(text, ctx)
    if isinstance(result, str):
        return web.json_response({"reply": result})
    if isinstance(result, Forward):
        text = result.text
    elif text.startswith("!") and result is None and text.split()[0].lower() in skills.COMMANDS:
        return web.json_response({"reply": ""})
    for _, fn in skills.hooks("preprocess"):
        text = fn(text, ctx, {"reply_to": None})
    return web.json_response({"reply": await agent.chat(text, ctx)})


def make_app() -> web.Application:
    app = web.Application(middlewares=[_auth])
    app.add_routes([web.get("/health", _health), web.post("/ask", _ask), *_routes.values()])
    return app


async def start() -> str | None:
    """Serve the API if API_TOKEN is set. Returns the address, or None when off."""
    global _runner
    if not config.API_TOKEN:
        return None
    _runner = web.AppRunner(make_app(), access_log=None)
    await _runner.setup()
    await web.TCPSite(_runner, config.API_HOST, config.API_PORT).start()
    return f"http://{config.API_HOST}:{config.API_PORT}"
