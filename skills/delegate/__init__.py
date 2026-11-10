"""Delegate skill: coding tickets handed to agents (Claude Code or OpenCode) running headless on this machine.

"Fix the pathfinding in the Minecraft bot and open a PR" -> the model writes a brief and calls
open_ticket -> a run is queued -> on the next tick a Claude Code session starts in its own git
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
_TIER_LIST = ", ".join(f"{t}={m}" for t, (b, m) in config.AGENT_TIERS.items())

GUIDE = f"""Coding tickets (code work handed to an agent that opens a PR; NOT planner items):
- To get code written, fixed, investigated or reviewed, call open_ticket with a complete brief (what, where if known, \
constraints, what done looks like). Never code yourself or guess the outcome.
- repository: one of {', '.join(REPOS)} ONLY if {{owner}} named it or clearly means that project. Otherwise omit it: \
the work starts as a NEW local project (give a short project_name). Never guess a repository.
- worker: cheap = docs, summaries, small well-defined changes; standard = normal fixes and features; strong = only \
when asked. A named model or provider means its tier ({_TIER_LIST}).
- "ticket 3" = a ticket (ticket_status, continue_ticket, `!cancel 3`); "task 30" or an agenda item = a planner item. \
Never change one because of the other.
- Follow-ups and answers to an agent's questions go to continue_ticket, not a new ticket.
- Tell {{owner}} the ticket number and that `!cancel <n>` stops it; don't promise how long it takes."""


def _enabled() -> bool:
    return bool(config.AGENT_REPOS)


# ---------- tools ----------
@tool(writes=True, owner_only=True, enabled=_enabled)
def open_ticket(objective: Annotated[str, "Complete brief: goal or problem, where to look, constraints, what done looks like."],
                repository: Repository = None, worker: Worker = None,
                project_name: Annotated[str, "For a new project only: short kebab-case name."] = "",
                ctx: ToolContext = None) -> str:
    """Hand code work to a background agent: a PR in an existing repository, or a new local project."""
    if len(objective.strip()) < 20:
        return "Error: the objective is too short to act on. Describe the problem or goal fully."
    target, err = resolve_target(repository, project_name, objective)
    if err:
        return err
    worker = worker if worker in config.AGENT_TIERS else config.AGENT_DEFAULT_TIER
    backend, model = config.AGENT_TIERS[worker]
    t = store.create_task(objective, target, worker, backend, model, origin=ctx.frontend if ctx else "discord")
    where = f"a new project '{target[len(store.NEW):]}'" if target.startswith(store.NEW) else target
    return (f"Queued ticket #{t['id']} for {where} ({worker}: {model}). It starts within 30 seconds; "
            f"`!cancel {t['id']}` stops it. The result will be sent when it's done.")


def resolve_target(repository: str | None, project_name: str, objective: str) -> tuple[str, str | None]:
    """(repository value for the ticket, error). No repository -> a new project when AGENT_NEW_PROJECTS is set."""
    if repository in config.AGENT_REPOS:
        return repository, None
    if repository:
        return "", f"Error: unknown repository {repository!r}. Options: {', '.join(config.AGENT_REPOS)}, or omit it for a new project."
    if not config.AGENT_NEW_PROJECTS:
        return "", (f"Error: no repository given and new projects are off. Ask {config.owner()} which repository: "
                    f"{', '.join(config.AGENT_REPOS)}.")
    return store.NEW + store.slug(project_name or " ".join(objective.split()[:4])), None


@tool(owner_only=True, enabled=_enabled)
def ticket_status(ticket: Annotated[int, "Ticket number; 0 = recent tickets."] = 0) -> str:
    """Status of coding tickets."""
    if ticket:
        t = store.get_task(ticket)
        return detail(t) if t else f"Error: no ticket #{ticket}."
    tasks = store.list_tasks(limit=6)
    return "\n".join(store.describe(t, runner.LIVE.get(t["id"])) for t in tasks) or "No coding tickets yet."


@tool(writes=True, owner_only=True, enabled=_enabled)
def continue_ticket(ticket: int, message: Annotated[str, "Next instruction or answers, complete in itself."]) -> str:
    """Follow up on a finished ticket; its agent resumes with its context."""
    return runner.continue_task(ticket, message)


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
    return "Coding tickets in progress (don't open a duplicate; these are not planner items):\n" + \
        "\n".join("  " + store.describe(t, runner.LIVE.get(t["id"])) for t in active)


# ---------- commands ----------
@command("!tickets", "coding tickets handed to agents")
def _tickets_cmd(args: str, ctx) -> str:
    return ticket_status(0)


@command("!ticket", "details of one coding ticket: `!ticket <n>`")
def _ticket_cmd(args: str, ctx) -> str:
    return ticket_status(_num(args)) if _num(args) else "Usage: `!ticket <number>`"


@command("!cancel", "stop a coding ticket: `!cancel <n>`")
async def _cancel_cmd(args: str, ctx) -> str:
    return await runner.cancel(_num(args)) if _num(args) else "Usage: `!cancel <number>`"


@command("!continue", "follow up on a coding ticket: `!continue <n> <what to do next>`")
def _continue_cmd(args: str, ctx) -> str:
    n, _, rest = args.strip().partition(" ")
    return runner.continue_task(_num(n), rest) if _num(n) and rest.strip() else "Usage: `!continue <number> <message>`"


@command("!close", "remove a finished ticket's worktree: `!close <n>`")
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
    worker = data.get("worker") or config.AGENT_DEFAULT_TIER
    if len(objective) < 20:
        return web.json_response({"error": "objective too short"}, status=400)
    repository, err = resolve_target(data.get("repository") or None, data.get("project_name") or "", objective)
    if err:
        return web.json_response({"error": err[7:]}, status=400)
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
        return web.json_response({"error": "no such ticket"}, status=404)
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


ROUTES = [web.post("/tickets", _api_create), web.get("/tickets", _api_list), web.get("/tickets/{id}", _api_get),
          web.post("/tickets/{id}/continue", _api_continue), web.post("/tickets/{id}/cancel", _api_cancel)]


# ---------- hooks ----------
async def on_start() -> None:
    runner.recover()
    api.add_routes(ROUTES)


async def on_tick() -> None:
    if _enabled():
        await runner.tick()
