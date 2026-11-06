"""Measure reply speed on this machine with a fixed conversation.

    .venv\\Scripts\\python tools\\bench.py            (uses a throwaway copy of your database)
    .venv\\Scripts\\python tools\\bench.py --runs 3   (repeat and report the median)
    --gap N   simulated minutes between messages (default 5; 0 = back-to-back)

For each message it prints the prompt size, how long the model server spent reading the prompt
("read s": low means its cache was reused), how long writing the reply took, and the total. Run it on
the machine you deploy to after changing models, context size or prompt layout.
"""
import argparse
import asyncio
import os
import shutil
import statistics
import sys
import tempfile
import time
from datetime import timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MESSAGES = [
    "hey, how's it going?",
    "what's a good way to explain recursion to a 10 year old? keep it short",
    "remind me to buy a birthday card tomorrow at 10am",
    "what do I have coming up this week?",
    "thanks! what's 18% of 64?",
]


def _prepare_db() -> str:
    """Point the app at a copy of the real database (or an empty one) before anything imports config."""
    tmp = os.path.join(tempfile.mkdtemp(), "bench.db")
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
    src = os.getenv("DB_PATH") or os.path.join(ROOT, "data", "jarofass.db")
    if os.path.exists(src):
        shutil.copy(src, tmp)
    os.environ.update(DB_PATH=tmp, GCAL_ENABLED="false")
    return tmp


_clock = {"offset": 0}


def _simulate_clock() -> None:
    """Let bench time run ahead of the wall clock, so a gap between messages can be simulated."""
    from core import db
    real = db.now

    def fake():
        return real() + timedelta(minutes=_clock["offset"])
    db.now = fake
    for name in ("skills.planner.store",):  # modules that imported now() by name
        if name in sys.modules:
            sys.modules[name].now = fake


async def run_once(conv: str, gap: int) -> list[dict]:
    from core import agent, llm
    from core.tools import ToolContext
    provider = llm.provider()
    original, calls = provider.chat, []

    async def timed(*a, **k):
        r = await original(*a, **k)
        calls.append(r)
        return r
    provider.chat = timed
    rows = []
    try:
        for text in MESSAGES:
            _clock["offset"] += gap  # real chats have minutes between messages, so the time line changes
            start, first = time.perf_counter(), len(calls)
            await agent.chat(text, ToolContext(conversation=conv, frontend="bench"))
            mine = calls[first:]
            rows.append({"msg": text, "total": time.perf_counter() - start, "steps": len(mine),
                         "prompt_tokens": sum(r.prompt_tokens or 0 for r in mine),
                         "read_s": sum(r.prompt_seconds or 0 for r in mine),
                         "write_s": sum(r.gen_seconds or 0 for r in mine)})
    finally:
        provider.chat = original
    return rows


async def main(runs: int, gap: int):
    import config
    from core import llm, skills
    skills.load()
    _simulate_clock()
    print(await llm.status())
    print(f"model {config.LLM_MODEL}, context {config.LLM_CONTEXT}, {gap} min between messages, "
          f"skills: {', '.join(skills.SKILLS)}\n")
    results = [await run_once(f"bench-{i}", gap) for i in range(runs)]
    print(f"{'message':44} {'steps':>5} {'prompt tok':>10} {'read s':>7} {'write s':>8} {'total s':>8}")
    for i, text in enumerate(MESSAGES):
        med = {k: statistics.median(r[i][k] for r in results)
               for k in ("steps", "prompt_tokens", "read_s", "write_s", "total")}
        print(f"{text[:44]:44} {med['steps']:5.0f} {med['prompt_tokens']:10.0f} {med['read_s']:7.2f} "
              f"{med['write_s']:8.2f} {med['total']:8.2f}")
    after_first = [r[i] for r in results for i in range(1, len(MESSAGES))]
    print(f"\nmessages 2-{len(MESSAGES)}: avg prompt {statistics.mean(x['prompt_tokens'] for x in after_first):.0f} "
          f"tokens, {statistics.mean(x['read_s'] for x in after_first):.2f} s reading, "
          f"{statistics.mean(x['total'] for x in after_first):.2f} s total per message")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--gap", type=int, default=5, help="simulated minutes between messages (default 5)")
    args = ap.parse_args()
    _prepare_db()
    asyncio.run(main(args.runs, args.gap))
