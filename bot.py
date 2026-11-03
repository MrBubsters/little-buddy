"""Discord front-end: chat with the agent, plus reminder loop and daily digest."""
import asyncio
import sys

import discord
from discord.ext import tasks

import agent
import checkins
import config
import db
import gcal

# Windows consoles default to cp1252 and crash on emoji in log lines.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)
llm_lock = asyncio.Lock()  # one LLM request at a time — keeps VRAM use flat

HELP = f"""**{config.BOT_NAME}** — just talk to me normally, e.g.
• "dentist next tuesday at 3pm"
• "project report due friday 5pm, remind me thursday evening"
• "gym every weekday at 7am"
• "my sister's name is Ana, birthday is March 4"
Commands: `!agenda` `!week` (add `all` to include work on weekends) `!facts` `!digest` `!sync` (Google Calendar) `!reset` (clear chat history) `!help`
Check-ins: `!update <what you did>` `!skip` (skip today's check-ins) `!worklog` (last 7 days)
📆 = synced with Google Calendar"""


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


async def notify(dest: discord.abc.Messageable, text: str) -> discord.Message | None:
    """Unprompted message: @mention the owner so Discord sends a notification."""
    return await send(dest, f"<@{config.OWNER_ID}> {text}")


def agenda(days: int, include_work: bool | None = None) -> str:
    """Agenda text. Work items are hidden on non-work days unless include_work=True."""
    n = db.now()
    if include_work is None:
        include_work = db.is_work_day(n)
    today_end = db.fmt(n.replace(hour=23, minute=59))
    overdue, today, later, undated = [], [], [], []
    hidden = 0 if include_work else len(db.list_items(days, category="work"))
    for it in db.list_items(days, exclude_category=None if include_work else "work"):
        if not it["due_at"]:
            undated.append(it)
        elif it["due_at"] < db.fmt(n):
            overdue.append(it)
        elif it["due_at"] <= today_end:
            today.append(it)
        else:
            later.append(it)
    parts = [f"📅 **{n.strftime('%A, %B %d')}**"]
    for label, group in (("⚠️ Overdue", overdue), ("Today", today),
                         (f"Next {days} days", later), ("No date", undated)):
        if group:
            parts.append(f"**{label}**\n" + "\n".join("• " + db.describe(i) for i in group))
    if len(parts) == 1:
        parts.append("Nothing scheduled. 🎉")
    if hidden:
        parts.append(f"_💼 {hidden} work item(s) hidden for the weekend. `!agenda all` to see them._")
    return "\n\n".join(parts)


@tasks.loop(seconds=30)
async def scheduler():
    try:
        dest = await target()
        for it in db.due_reminders():
            if it["category"] == "work" and not db.is_work_day():
                db.postpone_reminder(it, db.next_work_start())  # no work pings on days off
                continue
            when = f" at {db.pretty(it['due_at'])}" if it["due_at"] else ""
            await notify(dest, f"⏰ **Reminder:** {it['title']}{when}" + (f"\n{it['notes']}" if it["notes"] else ""))
            db.mark_reminded(it)

        n = db.now()
        today = n.strftime("%Y-%m-%d")
        if n.strftime("%H:%M") >= config.DIGEST_TIME and db.get_meta("last_digest") != today:
            db.set_meta("last_digest", today)
            await notify(dest, "☀️ Good morning!\n" + agenda(3))

        for c in checkins.CHECKINS:
            if checkins.due(c):
                msg = await notify(dest, checkins.message_for(c))
                checkins.record_sent(c, msg.id)
    except Exception as e:
        print(f"[scheduler] {e!r}")


@tasks.loop(minutes=config.GCAL_SYNC_MINUTES)
async def gcal_loop():
    try:
        n = await gcal.sync()
        print(f"[gcal] synced {n} events")
    except Exception as e:
        print(f"[gcal] sync failed: {e!r}")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} — model {config.OLLAMA_MODEL} @ {config.OLLAMA_HOST}")
    print("[llm] loading model…")
    try:
        print("[llm]", await agent.warm_up())
    except Exception as e:
        print(f"[llm] warm-up failed: {e!r}")
    if gcal.enabled() and not gcal_loop.is_running():
        gcal_loop.start()  # first run is immediate, before reminders/digest go out
    elif config.GCAL_ENABLED:
        print(f"[gcal] enabled but {config.GCAL_TOKEN} missing — run: .venv\\Scripts\\python gcal.py")
    if not scheduler.is_running():
        scheduler.start()


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.author.id != config.OWNER_ID:
        return
    is_dm = isinstance(message.channel, discord.DMChannel)
    if not (is_dm or (config.CHANNEL_ID and message.channel.id == config.CHANNEL_ID)):
        return

    text = message.content.strip()
    cmd = text.lower()
    if cmd == "!help":
        return await send(message.channel, HELP)
    if cmd in ("!agenda", "!digest", "!agenda all"):
        return await send(message.channel, agenda(3, include_work=True if cmd.endswith("all") else None))
    if cmd in ("!week", "!week all"):
        return await send(message.channel, agenda(7, include_work=True if cmd.endswith("all") else None))
    if cmd == "!facts":
        facts = db.list_facts()
        return await send(message.channel, "\n".join(f"[{f['id']}] {f['fact']}" for f in facts) or "No facts yet.")
    if cmd == "!sync":
        if not gcal.enabled():
            return await send(message.channel, "Google Calendar isn't set up (see README).")
        try:
            n = await gcal.sync()
            return await send(message.channel, f"📆 Synced {n} events from Google Calendar.\n\n" + agenda(3))
        except Exception as e:
            return await send(message.channel, f"⚠️ Sync failed: {e}")
    if cmd == "!skip":
        skipped = checkins.skip_today()
        names = ", ".join(c.label for c in skipped)
        return await send(message.channel, f"👍 Skipped today's {names}." if skipped else "Nothing to skip today.")
    if cmd == "!worklog":
        rows = checkins.history("work", 7)
        return await send(message.channel, "\n".join(f"**{r['date']}** {r['response']}" for r in rows)
                          or "No work updates in the last 7 days.")
    if cmd == "!reset":
        db.clear_messages()
        return await send(message.channel, "Chat history cleared (items and facts kept).")
    if not text:
        return

    # Check-in answers: a Discord reply to a check-in message, or `!update ...`, is logged directly.
    answered = None
    if message.reference and message.reference.message_id:
        answered = checkins.match_reply(message.reference.message_id)
    if cmd.startswith("!update"):
        text = text[len("!update"):].strip()
        if not text:
            return await send(message.channel, "Usage: `!update what you got done today`")
        answered = checkins.BY_NAME["work"]
    if answered:
        checkins.complete(answered, text)
        text = f"[{answered.label} logged] {text}"

    async with llm_lock, message.channel.typing():
        try:
            reply = await agent.chat(text)
        except Exception as e:
            reply = f"⚠️ Couldn't reach the local model ({e.__class__.__name__}: {e}). Is Ollama running?"
            if answered:
                reply = f"✅ Logged your {answered.label}. (Model unavailable: {e.__class__.__name__})"
    await send(message.channel, reply)


if __name__ == "__main__":
    if not config.DISCORD_TOKEN or not config.OWNER_ID:
        raise SystemExit("Set DISCORD_TOKEN and OWNER_ID in .env (see .env.example).")
    bot.run(config.DISCORD_TOKEN)
