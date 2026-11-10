"""Queue worker for delegated tasks: starts queued runs when there's room, each in its own git worktree,
records the outcome and tells the owner. Driven by the skill's on_tick (every ~30 s)."""
import asyncio
import os
from datetime import datetime

import config
from core.app import app
from skills.delegate import claude_code, opencode, store

BACKENDS = {"claude": claude_code, "opencode": opencode}

ACTIVE: dict[int, asyncio.Task] = {}   # task id -> the asyncio task executing its run
LIVE: dict[int, dict] = {}             # task id -> {"started_at", "tools", "last"} for status displays

SYSTEM_PROMPT = """You were started unattended by {owner}'s personal assistant from a message they sent on Discord. \
They are away from the machine and will read your result on their phone.
You are in a dedicated git worktree of the repository "{repo}" on branch {branch}, based on {base}. Work only here.
When the work is done: run the project's tests if it has any, commit with a clear message, push the branch to origin \
and open a pull request against {base} with `gh pr create` (write a proper title and body). Never merge, never \
force-push, never touch other branches.
If the task is impossible, unsafe or too ambiguous, stop and say what you need instead of guessing. If a \
discord_ask tool is available you may use it for one essential question.
While working, if you find a clear bug unrelated to the assigned task, do not expand the code changes to fix it. \
Create an issue in the GitHub repository where the bug lives, with a concise title, evidence or reproduction steps, \
and impact. Identify the correct repository from its git remote and check for an existing issue first. If you cannot \
identify the repository or create the issue, report the bug and the blocker without claiming a ticket was filed. \
Include any created issue URL in your final summary. Bugs that are necessary to complete the assigned task remain in scope.
Your final message is sent to {owner} as-is: a short summary (Discord markdown, under 1500 characters) of what \
you found, what you changed, how you verified it, and the PR URL."""


# ---------- queue ----------
async def tick() -> None:
    while len(ACTIVE) < config.AGENT_MAX_CONCURRENT:
        run = store.next_queued_run()
        if not run:
            return
        task = store.get_task(run["task_id"])
        spent = store.month_spend()
        if spent + config.AGENT_MAX_BUDGET_USD > config.AGENT_MONTHLY_BUDGET_USD:
            msg = (f"Monthly agent budget is used up (${spent:.2f} of ${config.AGENT_MONTHLY_BUDGET_USD:.2f}; a run "
                   f"may cost up to ${config.AGENT_MAX_BUDGET_USD:.2f}). Raise AGENT_MONTHLY_BUDGET_USD or wait.")
            store.update_run(run["id"], status="failed", finished_at=store.ts(), error=msg)
            store.update_task(task["id"], status="failed", summary=msg)
            await app.notify(f"❌ Task #{task['id']} ({task['repository']}) not started. {msg}")
            continue
        store.update_run(run["id"], status="running", started_at=store.ts())
        store.update_task(task["id"], status="running")
        LIVE[task["id"]] = {"started_at": store.ts(), "tools": 0, "last": ""}
        print(f"[delegate] task #{task['id']} run #{run['id']} starting: {task['repository']} "
              f"({task['tier']}: {task['backend']} {task['model']})")
        ACTIVE[task["id"]] = asyncio.create_task(_execute(task, run))


async def wait_all() -> None:
    """Block until every active run has finished (tests, shutdown)."""
    if ACTIVE:
        await asyncio.gather(*list(ACTIVE.values()), return_exceptions=True)


async def _execute(task: dict, run: dict) -> None:
    try:
        outcome = await _work(task, run)
    except asyncio.CancelledError:
        outcome = claude_code.Outcome(text="Stopped by the owner.", error_kind="stopped")
    except Exception as e:  # noqa: BLE001 - anything that breaks a run must be recorded, not raised
        outcome = claude_code.Outcome(text=f"{e.__class__.__name__}: {e}", error_kind="error")
    finally:
        ACTIVE.pop(task["id"], None)
        live = LIVE.pop(task["id"], {})
    await _finish(task, run, outcome, live)


async def _work(task: dict, run: dict) -> claude_code.Outcome:
    if not task["worktree"] or not os.path.isdir(task["worktree"]):
        path, branch, base = await _worktree(task)
        store.update_task(task["id"], worktree=path, branch=branch)
        task = store.get_task(task["id"])
    base = await _default_branch(config.AGENT_REPOS[task["repository"]])
    system = SYSTEM_PROMPT.format(owner=config.owner(), repo=task["repository"], branch=task["branch"], base=base)

    def progress(n: int, last: str) -> None:
        LIVE.get(task["id"], {}).update(tools=n, last=last)

    backend = BACKENDS.get(task["backend"])
    if not backend:
        raise RuntimeError(f"unknown backend '{task['backend']}'")
    return await backend.run(
        run["prompt"], task["worktree"], model=task["model"], budget_usd=config.AGENT_MAX_BUDGET_USD,
        system_prompt=system, resume=task["session_id"], key=task["id"],
        timeout_s=config.AGENT_TIMEOUT_MINUTES * 60, on_progress=progress)


async def _finish(task: dict, run: dict, o: claude_code.Outcome, live: dict) -> None:
    task = store.get_task(task["id"])  # _work may have set the worktree and branch
    pr = store.pr_url(o.text) or task["pr_url"] or await _pr_for_branch(task)
    if not o.ok and o.error_kind == "error" and o.text and pr:
        # The worker did the job (a PR exists) but exited non-zero, e.g. after provider rate-limit retries.
        o.ok, o.error_kind = True, None
        o.text += "\n\n(The worker exited with an error code after opening the PR; check the PR is complete.)"
    run_status = "completed" if o.ok else ("cancelled" if o.error_kind == "stopped" else "failed")
    store.update_run(run["id"], status=run_status, finished_at=store.ts(), session_id=o.session_id,
                     cost_usd=o.cost_usd, turns=o.turns, tools=o.tools, exit_code=o.exit_code,
                     result=o.text, error=o.error_kind)
    if o.ok:
        status = "awaiting_review" if pr else "completed"
    else:
        status = "cancelled" if o.error_kind == "stopped" else "failed"
    store.update_task(task["id"], status=status, session_id=o.session_id or task["session_id"], pr_url=pr,
                      summary=o.text[:4000], cost_usd=(task["cost_usd"] or 0) + o.cost_usd)
    print(f"[delegate] task #{task['id']} run #{run['id']} {run_status}: {o.turns} turns, {o.tools} tools, "
          f"${o.cost_usd:.2f}, exit {o.exit_code}, error={o.error_kind}, pr={pr}")
    await app.notify(_report(store.get_task(task["id"]), o, live))


def _report(t: dict, o: claude_code.Outcome, live: dict) -> str:
    word = {"awaiting_review": "done, PR ready for review", "completed": "done (no PR)",
            "failed": "failed", "cancelled": "stopped"}[t["status"]]
    head = f"{store.ICONS[t['status']]} Task #{t['id']} ({t['repository']}, {t['tier']}: {t['model']}) {word}"
    body = o.text if len(o.text) <= 1500 else o.text[:1500] + "…"
    lines = [head, body]
    if t["pr_url"] and t["pr_url"] not in body:
        lines.append(f"PR: {t['pr_url']}")
    if o.error_hint:
        lines.append(o.error_hint)
    mins = 0
    if live.get("started_at"):
        mins = int((datetime.fromisoformat(store.ts()) - datetime.fromisoformat(live["started_at"])).total_seconds() // 60)
    meta = f"-# {o.turns} turns · {o.tools} tools · ${o.cost_usd:.2f} · {mins}m"
    if o.denials:
        meta += f" · {o.denials} tool call(s) denied by permission mode {config.AGENT_PERMISSION_MODE}"
    lines.append(meta)
    lines.append(f"-# `!task {t['id']}` full result · `!continue {t['id']} <message>` follow up · `!close {t['id']}` when merged")
    return "\n".join(x for x in lines if x)


# ---------- actions ----------
def continue_task(task_id: int, message: str) -> str:
    t = store.get_task(task_id)
    if not t:
        return f"Error: no task #{task_id}."
    if t["status"] in store.TASK_ACTIVE:
        return f"Error: task #{task_id} is still {t['status']}; wait for it to finish (or `!cancel {task_id}`)."
    if t["status"] == "closed":
        return f"Error: task #{task_id} is closed; start a new task instead."
    if len(message.strip()) < 5:
        return "Error: say what the agent should do next."
    store.add_run(task_id, message)
    store.update_task(task_id, status="queued")
    how = "resuming its earlier session" if t["session_id"] else "starting fresh in its worktree"
    return f"Queued follow-up for task #{task_id} ({t['repository']}), {how}."


async def cancel(task_id: int) -> str:
    t = store.get_task(task_id)
    if not t:
        return f"Error: no task #{task_id}."
    if t["status"] not in store.TASK_ACTIVE:
        return f"Error: task #{task_id} is {t['status']}, nothing to cancel."
    if task_id in ACTIVE:
        ACTIVE[task_id].cancel()  # _execute records it as stopped and notifies
        return f"Stopping task #{task_id}…"
    for r in store.task_runs(task_id):
        if r["status"] == "queued":
            store.update_run(r["id"], status="cancelled", finished_at=store.ts())
    store.update_task(task_id, status="cancelled")
    return f"Cancelled task #{task_id} before it started."


async def close(task_id: int) -> str:
    """Remove the worktree (the branch and PR stay on GitHub) and mark the task closed."""
    t = store.get_task(task_id)
    if not t:
        return f"Error: no task #{task_id}."
    if t["status"] in store.TASK_ACTIVE:
        return f"Error: task #{task_id} is still {t['status']}."
    if t["worktree"] and os.path.isdir(t["worktree"]):
        try:
            await _git(config.AGENT_REPOS[t["repository"]], "worktree", "remove", "--force", t["worktree"])
        except (RuntimeError, KeyError) as e:
            return f"Error: couldn't remove the worktree: {e}"
    store.update_task(task_id, status="closed")
    return f"Closed task #{task_id}; worktree removed."


def recover() -> None:
    """After a restart: runs that were mid-flight are gone. Record that; queued ones start on the next tick."""
    for r in store.running_runs():
        store.update_run(r["id"], status="failed", finished_at=store.ts(), error="interrupted by restart")
        t = store.get_task(r["task_id"])
        if t and t["status"] == "running":
            store.update_task(t["id"], status="failed",
                              summary="The assistant restarted while this run was in progress. "
                                      f"Use `!continue {t['id']} <message>` to resume it.")


async def _pr_for_branch(task: dict) -> str | None:
    """The PR GitHub has for the task's branch, if any (workers sometimes forget to quote the URL)."""
    if not task.get("branch") or not task.get("worktree") or not os.path.isdir(task["worktree"]):
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "pr", "list", "--head", task["branch"], "--state", "all", "--json", "url", "--jq", ".[0].url",
            cwd=task["worktree"], env=claude_code.environment(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(proc.communicate(), 30)
    except (OSError, asyncio.TimeoutError):
        return None
    url = out.decode(errors="replace").strip()
    return url if url.startswith("http") else None


# ---------- git ----------
async def _git(cwd: str, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec("git", *args, cwd=cwd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, env=claude_code.environment())
    out, err = await proc.communicate()
    if proc.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {err.decode(errors='replace').strip()[-500:]}")
    return out.decode(errors="replace").strip()


async def _git_ok(cwd: str, *args: str) -> bool:
    try:
        await _git(cwd, *args)
        return True
    except RuntimeError:
        return False


async def _default_branch(repo: str) -> str:
    try:
        return (await _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")).split("/", 1)[1]
    except (RuntimeError, IndexError):
        pass
    for name in ("main", "master"):
        if await _git_ok(repo, "rev-parse", "--verify", "--quiet", f"origin/{name}") or \
           await _git_ok(repo, "rev-parse", "--verify", "--quiet", name):
            return name
    return await _git(repo, "rev-parse", "--abbrev-ref", "HEAD")


async def _worktree(task: dict) -> tuple[str, str, str]:
    repo = config.AGENT_REPOS.get(task["repository"])
    if not repo or not os.path.isdir(os.path.join(repo, ".git")):
        raise RuntimeError(f"repository '{task['repository']}' is not a git checkout at {repo}")
    base = await _default_branch(repo)
    branch = f"agent/task-{task['id']}"
    path = os.path.join(config.AGENT_WORKTREES, task["repository"], f"task-{task['id']}")
    await _git_ok(repo, "fetch", "--quiet", "origin")  # best effort: offline or no remote still works
    start = f"origin/{base}" if await _git_ok(repo, "rev-parse", "--verify", "--quiet", f"origin/{base}") else base
    await _git_ok(repo, "worktree", "prune")
    if not os.path.isdir(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        await _git(repo, "worktree", "add", "-B", branch, path, start)
    return path, branch, base
