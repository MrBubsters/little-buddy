"""Discord front-end: routes your messages to commands or the agent, and delivers notifications.

All abilities live in skills/ and the engine in core/; this file only knows about Discord.
"""
import asyncio
import sys

import discord

import config
from core import agent, scheduler, skills
from core.app import app
from core.skills import Forward, command
from core.tools import ToolContext

# Windows consoles default to cp1252 and crash on emoji in log lines.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    import voice  # optional: needs the voice packages from requirements.txt
except Exception as _e:
    voice = None
    print(f"[voice] disabled ({_e!r})")

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)
_started = False


async def target() -> discord.abc.Messageable:
    if config.CHANNEL_ID:
        return bot.get_channel(config.CHANNEL_ID) or await bot.fetch_channel(config.CHANNEL_ID)
    user = bot.get_user(config.OWNER_ID) or await bot.fetch_user(config.OWNER_ID)
    return user.dm_channel or await user.create_dm()


async def send(dest: discord.abc.Messageable, text: str) -> discord.Message | None:
    last = None
    for i in range(0, len(text), 1900):
        last = await dest.send(text[i:i + 1900])
    return last


async def notify(text: str) -> int | None:
    """Unprompted message: @mention the owner so Discord sends a notification. Returns the message id."""
    msg = await send(await target(), f"<@{config.OWNER_ID}> {text}")
    return msg.id if msg else None


@bot.event
async def on_ready():
    global _started
    print(f"Logged in as {bot.user} — model {config.LLM_MODEL} via {config.LLM_PROVIDER} @ {config.LLM_BASE_URL}")
    if _started:
        return  # on_ready fires again after reconnects
    _started = True
    print("[llm] loading model…")
    try:
        print("[llm]", await agent.warm_up())
    except Exception as e:
        print(f"[llm] warm-up failed: {e!r}")
    await skills.run_hooks("on_start")
    asyncio.create_task(scheduler.run_forever())


# ---------- voice calls (Discord-specific, so they live here rather than in a skill) ----------
@command("!join", "join the voice channel you're in, then just talk")
async def _join_cmd(args: str, ctx: ToolContext) -> str:
    if not voice:
        return "Voice isn't available. Install the voice packages (see README)."
    channel = voice.owner_voice_channel(bot)
    if not channel:
        return "Join a voice channel in a server I'm in first, then `!join`."
    dest = ctx.extra["channel"]
    async with dest.typing():
        try:
            return await voice.join(channel, dest)
        except Exception as e:
            return f"⚠️ Couldn't join voice: {e.__class__.__name__}: {e}"


@command("!leave", "leave the voice channel")
async def _leave_cmd(args: str, ctx: ToolContext) -> str:
    left = bool(voice) and await voice.leave()
    return "👋 Left the voice channel." if left else "I'm not in a voice channel."


@bot.event
async def on_voice_state_update(member, before, after):
    """Leave the voice channel once every human has left it."""
    s = voice and voice.session
    if s and before.channel and before.channel == s.vc.channel and not voice.humans_left(before.channel):
        await voice.leave()
        await send(s.text_dest, "👋 Everyone left, so I left the voice channel too.")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.author.id != config.OWNER_ID:
        return
    is_dm = isinstance(message.channel, discord.DMChannel)
    if not (is_dm or (config.CHANNEL_ID and message.channel.id == config.CHANNEL_ID)):
        return
    text = message.content.strip()
    if not text:
        return
    ctx = ToolContext(conversation="main", speaker=message.author.display_name, is_owner=True,
                      mode="text", frontend="discord", extra={"channel": message.channel})

    try:
        result = await skills.run_command(text, ctx)
    except Exception as e:
        return await send(message.channel, f"⚠️ Command failed: {e.__class__.__name__}: {e}")
    if isinstance(result, str):
        return await send(message.channel, result)
    if isinstance(result, Forward):
        text = result.text
    elif text.startswith("!") and result is None and text.split()[0].lower() in skills.COMMANDS:
        return  # a command that chose not to reply

    meta = {"reply_to": message.reference.message_id if message.reference else None}
    for name, fn in skills.hooks("preprocess"):
        try:
            text = fn(text, ctx, meta)
        except Exception as e:
            print(f"[skills] {name}.preprocess failed: {e!r}")

    async with message.channel.typing():
        try:
            reply = await agent.chat(text, ctx)
        except Exception as e:
            reply = (f"⚠️ Couldn't reach the model ({e.__class__.__name__}: {e}). "
                     f"Is {'Ollama' if config.LLM_PROVIDER == 'ollama' else 'the LLM server'} running?")
            if text.startswith("[") and " logged]" in text:
                reply = f"✅ Logged. (Model unavailable: {e.__class__.__name__})"
    await send(message.channel, reply)


if __name__ == "__main__":
    if not config.DISCORD_TOKEN or not config.OWNER_ID:
        raise SystemExit("Set DISCORD_TOKEN and OWNER_ID in .env (see .env.example).")
    skills.load()
    print(f"[skills] loaded: {', '.join(skills.SKILLS)}")
    app.attach("discord", notify)
    bot.run(config.DISCORD_TOKEN)
