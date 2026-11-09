"""Run one headless OpenCode session (any provider/model it supports) and read its result.

    outcome = await opencode.run(prompt, cwd, model="openai/gpt-6-luna", budget_usd=5, system_prompt="...")

Spawns `opencode run --format json --model <provider/model> --dir <cwd>` with the prompt on stdin and
follows the event stream: `step_start`, `text`, `tool_use` (with state completed|error) and `step_finish`
(tokens and cost per step). `--session <id>` continues an earlier session. Permissions come from the
machine's OpenCode config (see docs/delegation.md); a tool call the rules reject shows up as a denial.
"""
import asyncio
import json
import re

import config
from skills.delegate.claude_code import PROCESSES, Outcome, environment, stop

_AUTH = re.compile(r"\b401\b|unauthorized|invalid api key|incorrect api key|authentication|no api key|missing api key", re.I)
_LIMIT = re.compile(r"\b429\b|rate limit|quota|too many requests|overloaded|no credits|insufficient[_ ]quota|billing", re.I)
_DENIED = re.compile(r"rejected permission|rule which prevents|permission", re.I)


def command(*, model: str, cwd: str, resume: str | None) -> list[str]:
    args = [config.OPENCODE_PATH, "run", "--format", "json", "--model", model, "--dir", cwd,
            "--print-logs", "--log-level", "ERROR"]
    if resume:
        args += ["--session", resume]
    return args


async def run(prompt: str, cwd: str, *, model: str, budget_usd: float, system_prompt: str,
              resume: str | None = None, key: int = 0, timeout_s: float | None = None,
              on_progress=None) -> Outcome:
    out = Outcome()
    proc = await asyncio.create_subprocess_exec(
        *command(model=model, cwd=cwd, resume=resume), cwd=cwd, env=environment(),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    PROCESSES[key] = proc
    # OpenCode has no system-prompt flag: the brief carries its own instructions.
    proc.stdin.write(f"{system_prompt}\n\n---\n\n{prompt}".encode())
    await proc.stdin.drain()
    proc.stdin.close()

    texts: dict[str, list[str]] = {}   # messageID -> text parts
    last_message = None
    errors: list[str] = []
    over_budget = False
    stderr_chunks: list[bytes] = []

    async def read_err():
        while chunk := await proc.stderr.read(4096):
            stderr_chunks.append(chunk)

    async def read_out():
        nonlocal last_message, over_budget
        async for raw in proc.stdout:
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            if ev.get("sessionID"):
                out.session_id = ev["sessionID"]
            part = ev.get("part") or {}
            kind = ev.get("type")
            if kind == "text":
                last_message = part.get("messageID")
                texts.setdefault(last_message, []).append(part.get("text") or "")
            elif kind == "tool_use":
                out.tools += 1
                state = part.get("state") or {}
                if state.get("status") == "error" and _DENIED.search(state.get("error") or ""):
                    out.denials += 1
                if on_progress:
                    r = on_progress(out.tools, f"{part.get('tool')} {json.dumps(state.get('input') or {})[:80]}")
                    if asyncio.iscoroutine(r):
                        await r
            elif kind == "step_finish":
                out.turns += 1
                out.cost_usd += float(part.get("cost") or 0)
                if budget_usd and out.cost_usd > budget_usd and not over_budget:
                    over_budget = True
                    await stop(key)
            elif kind == "error":
                err = ev.get("error") or {}
                errors.append(str((err.get("data") or {}).get("message") or err.get("name") or err))

    timed_out = False
    try:
        await asyncio.wait_for(asyncio.gather(read_out(), read_err(), proc.wait()), timeout_s)
    except asyncio.TimeoutError:
        timed_out = True
        await stop(key)
        await proc.wait()
    except asyncio.CancelledError:
        await stop(key)
        await proc.wait()
        raise
    finally:
        PROCESSES.pop(key, None)

    out.exit_code = proc.returncode
    out.stderr = b"".join(stderr_chunks).decode(errors="replace")[-4000:]
    out.text = "".join(texts.get(last_message, [])).strip() if last_message else ""
    blob = "\n".join(errors) + "\n" + out.stderr
    if timed_out:
        out.error_kind = "timeout"
    elif over_budget:
        out.error_kind = "budget"
    elif getattr(proc, "_stopped", False):
        out.error_kind = "stopped"
    elif (errors or not out.text) and _AUTH.search(blob):
        out.error_kind = "auth"
    elif (errors or not out.text) and _LIMIT.search(blob):
        out.error_kind = "limit"
    elif errors or proc.returncode or not out.text:
        out.error_kind = "error"  # retried provider errors in stderr don't count when an answer came back
    out.ok = out.error_kind is None
    if not out.ok and not out.text:
        out.text = ("\n".join(errors) or out.stderr.strip()[-1500:]
                    or f"opencode exited with code {out.exit_code} and no reply")
    if out.denials and out.ok:
        out.text += f"\n\n({out.denials} tool call(s) were blocked by the permission rules.)"
    return out
