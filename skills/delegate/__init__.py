"""Delegate skill: hand coding work to Claude Code, running headless on this machine.

"Fix the pathfinding in the Minecraft bot and open a PR" -> the model writes a brief and calls
delegate_task -> a run is queued -> on the next tick a Claude Code session starts in its own git
worktree -> when it finishes, the owner gets the summary and PR link on Discord.

Off unless AGENT_REPOS names at least one repository (see docs/delegation.md). The same tasks are
reachable over the HTTP API (core/api.py) for buddyctl.py on another machine.
"""
from typing import Annotated, Literal

from aiohttp import web

import config
from core import api
from core.skills import command
from core.tools import ToolContext, tool
from skills.delegate import runner, store

REPOS = tuple(config.AGENT_REPOS) or ("none",)
Repository = Literal[REPOS]  # type: ignore[valid-type]
TIERS = tuple(config.AGENT_TIERS)
Worker = Literal[TIERS]  # type: ignore[valid-type]
_TIER_LIST = "; ".join(f"{t} = {m} via {'Claude Code' if b == 'claude' else 'OpenCode'}" for t, (b, m) in config.AGENT_TIERS.items())

GUIDE = f"""Delegating coding work:
- When {{owner}} wants code written, fixed, investigated or reviewed in one of these repositories \
({', '.join(REPOS)}), call delegate_task. A Claude Code agent then works on it in the background and opens a pull \
request; you'll be told when it finishes. Don't attempt the coding yourself and don't guess at the outcome.
- The objective is the ONLY thing the agent sees. Write a complete brief in 2-5 sentences: what's wrong or wanted, \
where to look if known, and what done looks like. Don't invent file paths, branch names or details you weren't given.
- worker: "cheap" for documentation, summaries, small well-defined changes and PR chores; "standard" for ordinary \
bug fixes and features; "strong" only when {{owner}} asks for it. If they name a model or provider (GPT, Luna, opus, \
Claude), pick the tier that uses it. Tiers: {_TIER_LIST}.
- After delegating, tell them the task number and that `!cancel <number>` stops it. Don't promise how long it takes.
- Follow-ups on a finished task ("also add tests", "the PR fails CI") go through continue_task with its number.
- task_status answers "how's the pathfinding fix going"."""


def _enabled() -> bool:
    return bool(config.AGENT_REPOS)


# ---------- tools ----------
@tool(writes=True, owner_only=True, enabled=_enabled)
def delegate_task(objective: Annotated[str, "Complete brief for the agent: the problem or goal, where to look, what done looks like."],
                  repository: Repository, worker: Annotated[Worker, "Which tier of agent should do it."] = None,
                  ctx: ToolContext = None) -> str:
    """Hand a coding task to a coding agent that works in the background and opens a pull request."""
    if repository not in config.AGENT_REPOS:
        return f"Error: unknown repository '{repository}'. Options: {', '.join(config.AGENT_REPOS)}."
    if len(objective.strip()) < 20:
        return "Error: the objective is too short to act on. Describe the problem or goal fully."
    worker = worker if worker in config.AGENT_TIERS else config.AGENT_DEFAULT_TIER
    backend, model = config.AGENT_TIERS[worker]
    t = store.create_task(objective, repository, worker, backend, model, origin=ctx.frontend if ctx else "discord")
    return (f"Queued task #{t['id']} for {repository} ({worker}: {model}). It starts within 30 seconds; "
            f"`!cancel {t['id']}` stops it. The result will be sent when it's done.")


@tool(owner_only=True, enabled=_enabled)
def task_status(task: Annotated[int, "Task number, or 0 for an overview of recent tasks."] = 0) -> str:
    """Status of delegated coding tasks: what's running, finished, or waiting for review."""
    if task:
        t = store.get_task(task)
        return detail(t) if t else f"Error: no task #{task}."
    tasks = store.list_tasks(limit=6)
    return "\n".join(store.describe(t, runner.LIVE.get(t["id"])) for t in tasks) or "No delegated tasks yet."


@tool(writes=True, owner_only=True, enabled=_enabled)
def continue_task(task: int, message: Annotated[str, "What to do next, as a complete instruction for the agent."]) -> str:
    """Send a follow-up to a finished task's agent; it resumes with its earlier context."""
    return runner.continue_task(task, message)


def detail(t: dict) -> str:
    lines = [store.describe(t, runner.LIVE.get(t["id"]))]
    for r in store.task_runs(t["id"]):
        when = (r["finished_at"] or r["started_at"] or r["created_at"])[5:16].replace("T", " ")
        extra = f" ${r['cost_usd']:.2f} · {r['turns']} turns" if r["cost_usd"] is not None else ""
        extra += f" · {r['error']}" if r["error"] else ""
        lines.append(f"  run #{r['id']} {r['status']} {when}{extra} — {r['prompt'][:60]}")
    if t["summary"]:
        s = t["summary"]
        lines.append(s if len(s) <= 1500 else s[:1500] + "…")
    return "\n".join(lines)


# ---------- live context ----------
def context(ctx: ToolContext, query: str) -> str | None:
    if not _enabled():
        return None
    active = store.list_tasks(store.TASK_ACTIVE, 5)
    if not active:
        return None
    return "Delegated coding tasks in progress (don't start a duplicate):\n" + \
        "\n".join("  " + store.describe(t, runner.LIVE.get(t["id"])) for t in active)


# ---------- commands ----------
@command("!tasks", "delegated coding tasks")
def _tasks_cmd(args: str, ctx) -> str:
    return task_status(0)


@command("!task", "details of one task: `!task <n>`")
def _task_cmd(args: str, ctx) -> str:
    return task_status(_num(args)) if _num(args) else "Usage: `!task <number>`"


@command("!cancel", "stop a delegated task: `!cancel <n>`")
async def _cancel_cmd(args: str, ctx) -> str:
    return await runner.cancel(_num(args)) if _num(args) else "Usage: `!cancel <number>`"


@command("!continue", "follow up on a task: `!continue <n> <what to do next>`")
def _continue_cmd(args: str, ctx) -> str:
    n, _, rest = args.strip().partition(" ")
    return runner.continue_task(_num(n), rest) if _num(n) and rest.strip() else "Usage: `!continue <number> <message>`"


@command("!close", "remove a finished task's worktree: `!close <n>`")
async def _close_cmd(args: str, ctx) -> str:
    return await runner.close(_num(args)) if _num(args) else "Usage: `!close <number>`"


def _num(s: str) -> int:
    try:
        return int(s.strip().lstrip("#").split()[0])
    except (ValueError, IndexError):
        return 0


# ---------- HTTP API (core/api.py adds these when API_TOKEN is set) ----------
def _json_task(t: dict, runs: bool = False) -> dict:
    out = dict(t)
    if runs:
        out["runs"] = store.task_runs(t["id"])
    return out


async def _api_create(request: web.Request) -> web.Response:
    data = await request.json()
    objective = (data.get("objective") or "").strip()
    repository = data.get("repository") or ""
    worker = data.get("worker") or config.AGENT_DEFAULT_TIER
    if repository not in config.AGENT_REPOS:
        return web.json_response({"error": f"unknown repository; options: {', '.join(config.AGENT_REPOS)}"}, status=400)
    if len(objective) < 20:
        return web.json_response({"error": "objective too short"}, status=400)
    if worker not in config.AGENT_TIERS:
        return web.json_response({"error": f"unknown worker; options: {', '.join(config.AGENT_TIERS)}"}, status=400)
    backend, model = config.AGENT_TIERS[worker]
    return web.json_response(_json_task(store.create_task(objective, repository, worker, backend, model, origin="api")),
                             status=201)


async def _api_list(request: web.Request) -> web.Response:
    return web.json_response([_json_task(t) for t in store.list_tasks(limit=int(request.query.get("limit", 20)))])


async def _api_get(request: web.Request) -> web.Response:
    t = store.get_task(int(request.match_info["id"]))
    if not t:
        return web.json_response({"error": "no such task"}, status=404)
    out = _json_task(t, runs=True)
    out["live"] = runner.LIVE.get(t["id"])
    return web.json_response(out)


async def _api_continue(request: web.Request) -> web.Response:
    data = await request.json()
    msg = runner.continue_task(int(request.match_info["id"]), data.get("message") or "")
    return web.json_response({"message": msg}, status=400 if msg.startswith("Error") else 200)


async def _api_cancel(request: web.Request) -> web.Response:
    msg = await runner.cancel(int(request.match_info["id"]))
    return web.json_response({"message": msg}, status=400 if msg.startswith("Error") else 200)


ROUTES = [web.post("/tasks", _api_create), web.get("/tasks", _api_list), web.get("/tasks/{id}", _api_get),
          web.post("/tasks/{id}/continue", _api_continue), web.post("/tasks/{id}/cancel", _api_cancel)]


# ---------- hooks ----------
async def on_start() -> None:
    runner.recover()
    api.add_routes(ROUTES)


async def on_tick() -> None:
    if _enabled():
        await runner.tick()
