"""Run one headless Claude Code session and read its result.

    outcome = await claude_code.run(prompt, cwd, model="sonnet", budget_usd=5, system_prompt="...")

Spawns `claude -p --output-format stream-json`, feeds the prompt on stdin and follows the event stream
(the same mechanism as claude-discord's daemon). The final `result` event carries the session id, the
summary text, the cost and whether it failed. `--resume <session id>` continues an earlier session.
"""
import asyncio
import glob
import json
import os
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import config

PROCESSES: dict[int, asyncio.subprocess.Process] = {}  # key -> live process, so a run can be stopped

DENIED = ["Bash(gh pr merge*)", "Bash(git push --force*)", "Bash(git push -f*)", "Bash(git reset --hard*)"]

_AUTH = re.compile(r"oauth token|not logged in|please run /login|authentication_error|invalid api key|expired", re.I)
_LIMIT = re.compile(r"rate limit|usage limit|limit reached|too many requests|\b429\b|overloaded|out of extra usage", re.I)
_BUDGET = re.compile(r"max(imum)? budget|budget (limit|exceeded|of \$)", re.I)


@dataclass
class Outcome:
    ok: bool = False
    text: str = ""
    session_id: str | None = None
    cost_usd: float = 0.0
    turns: int = 0
    tools: int = 0
    exit_code: int | None = None
    error_kind: str | None = None   # None | auth | limit | budget | timeout | stopped | error
    denials: int = 0
    stderr: str = ""
    events: list = field(default_factory=list)

    @property
    def error_hint(self) -> str:
        return {
            "auth": "The worker's credentials are missing or expired: log in to Claude Code on this machine, or check "
                    "the provider API key in .env.",
            "limit": "The provider's usage, rate or credit limit was hit. Wait for it to reset, add credits, or use "
                     "another tier (`!continue <n> <message>` retries once that's sorted).",
            "budget": f"The run hit its ${config.AGENT_MAX_BUDGET_USD:.2f} budget before finishing.",
            "timeout": f"The run exceeded {config.AGENT_TIMEOUT_MINUTES} minutes and was stopped.",
            "stopped": "The run was stopped.",
        }.get(self.error_kind or "", "")


def environment() -> dict:
    """The service's environment plus the tool paths a login shell would have (claude, node via nvm)."""
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)  # don't look like a nested session
    home = os.path.expanduser("~")
    extra = [os.path.join(home, ".local", "bin"), *sorted(glob.glob(os.path.join(home, ".nvm", "versions", "node", "*", "bin")), reverse=True)]
    env["PATH"] = os.pathsep.join([*extra, env.get("PATH", "")])
    return env


def command(*, model: str, budget_usd: float, system_prompt: str, resume: str | None) -> list[str]:
    args = [config.CLAUDE_PATH, "-p", "--output-format", "stream-json", "--verbose",
            "--permission-mode", config.AGENT_PERMISSION_MODE, "--max-budget-usd", f"{budget_usd:.2f}",
            "--model", model, "--append-system-prompt", system_prompt]
    if resume:
        args += ["--resume", resume]
    return args + ["--disallowedTools", *DENIED]  # variadic: keep last


def classify(text: str, stderr: str, is_error: bool, exit_code: int | None) -> str | None:
    blob = f"{text}\n{stderr}"
    if _AUTH.search(blob):
        return "auth"
    if _LIMIT.search(blob):
        return "limit"
    if _BUDGET.search(blob):
        return "budget"
    return "error" if (is_error or exit_code) else None


async def run(prompt: str, cwd: str, *, model: str, budget_usd: float, system_prompt: str,
              resume: str | None = None, key: int = 0, timeout_s: float | None = None,
              on_progress: Callable[[int, str], Awaitable[None] | None] | None = None) -> Outcome:
    out = Outcome()
    proc = await asyncio.create_subprocess_exec(
        *command(model=model, budget_usd=budget_usd, system_prompt=system_prompt, resume=resume),
        cwd=cwd, env=environment(), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    PROCESSES[key] = proc
    proc.stdin.write(prompt.encode())
    await proc.stdin.drain()
    proc.stdin.close()

    result = None
    stderr_chunks: list[bytes] = []

    async def read_err():
        while chunk := await proc.stderr.read(4096):
            stderr_chunks.append(chunk)

    async def read_out():
        nonlocal result
        async for raw in proc.stdout:
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            if ev.get("session_id"):
                out.session_id = ev["session_id"]
            if ev.get("type") == "assistant":
                for c in (ev.get("message") or {}).get("content") or []:
                    if c.get("type") == "tool_use":
                        out.tools += 1
                        if on_progress:
                            r = on_progress(out.tools, _describe(c))
                            if asyncio.iscoroutine(r):
                                await r
            elif ev.get("type") == "result":
                result = ev

    timed_out = False
    try:
        await asyncio.wait_for(asyncio.gather(read_out(), read_err(), proc.wait()), timeout_s)
    except asyncio.TimeoutError:
        timed_out = True
        await stop(key)
        await proc.wait()
    except asyncio.CancelledError:  # the run was cancelled by the owner: don't leave claude running
        await stop(key)
        await proc.wait()
        raise
    finally:
        PROCESSES.pop(key, None)

    out.exit_code = proc.returncode
    out.stderr = b"".join(stderr_chunks).decode(errors="replace")[-4000:]
    if result:
        out.text = (result.get("result") or "").strip()
        out.cost_usd = float(result.get("total_cost_usd") or 0)
        out.turns = int(result.get("num_turns") or 0)
        out.denials = len(result.get("permission_denials") or [])
        out.session_id = result.get("session_id") or out.session_id
    is_error = bool(result and result.get("is_error"))
    if timed_out:
        out.error_kind = "timeout"
    elif getattr(proc, "_stopped", False):
        out.error_kind = "stopped"
    else:
        out.error_kind = classify(out.text, out.stderr, is_error, out.exit_code if not result else 0)
        if not result and out.error_kind is None:
            out.error_kind = "error"
    out.ok = out.error_kind is None
    if not out.ok and not out.text:
        out.text = out.stderr.strip()[-1500:] or f"claude exited with code {out.exit_code} and no result"
    return out


async def stop(key: int) -> bool:
    proc = PROCESSES.get(key)
    if not proc or proc.returncode is not None:
        return False
    proc._stopped = True  # noqa: SLF001 (our own marker)
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except asyncio.TimeoutError:
        proc.kill()
    return True


def _describe(c: dict) -> str:
    inp = c.get("input") or {}
    detail = inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("description") or ""
    return f"{c.get('name')} {str(detail)[:80]}".strip()
