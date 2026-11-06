"""Chat with the assistant in the terminal, without Discord. Handy for developing and testing skills.

    .venv\\Scripts\\python cli.py              interactive chat (commands like !status work too)
    .venv\\Scripts\\python cli.py "message"    one message, then exit
    --tick                                     also run reminders, check-ins and jobs in the background

Uses the same database and settings as the bot, so set DB_PATH to a scratch file to experiment
without touching your real data.
"""
import asyncio
import sys

from core import agent, scheduler, skills
from core.app import app
from core.skills import Forward
from core.tools import ToolContext

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


async def _notify(text: str):
    print(f"\n🔔 {text}\n> ", end="", flush=True)
    return None


async def handle(text: str, ctx: ToolContext) -> str:
    result = await skills.run_command(text, ctx)
    if isinstance(result, str):
        return result
    if isinstance(result, Forward):
        text = result.text
    for _, fn in skills.hooks("preprocess"):
        text = fn(text, ctx, {"reply_to": None})
    return await agent.chat(text, ctx)


async def main(argv: list[str]):
    tick = "--tick" in argv
    argv = [a for a in argv if a != "--tick"]
    skills.load()
    app.attach("cli", _notify)
    ctx = ToolContext(conversation="cli", frontend="cli")
    print("[llm]", await agent.warm_up())
    await skills.run_hooks("on_start")
    if tick:
        asyncio.create_task(scheduler.run_forever())
    if argv:
        print(await handle(" ".join(argv), ctx))
        return
    print(f"[skills] {', '.join(skills.SKILLS)}. Type !help, or Ctrl+C to quit.")
    while True:
        try:
            text = (await asyncio.to_thread(input, "> ")).strip()
        except (EOFError, KeyboardInterrupt):
            return
        if text:
            print(await handle(text, ctx))


if __name__ == "__main__":
    try:
        asyncio.run(main(sys.argv[1:]))
    except KeyboardInterrupt:
        pass
