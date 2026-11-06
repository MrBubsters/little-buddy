"""Local LLM agent: Ollama chat + tool calls against the SQLite store."""
import asyncio
import json
import re
from datetime import timedelta

from ollama import AsyncClient

import checkins
import config
import db
import gcal

client = AsyncClient(host=config.OLLAMA_HOST)
MAX_TOOL_ROUNDS = 5


def _fn(name, desc, props, required=()):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": list(required)}}}


S, I = {"type": "string"}, {"type": "integer"}
TOOLS = [
    _fn("add_item", "Save an event, deadline, task or reminder. Dated events also go to Google Calendar.", {
        "title": S,
        "kind": {"type": "string", "enum": list(db.KINDS)},
        "due_at": {"type": "string",
                   "description": "Local time 'YYYY-MM-DD HH:MM', or 'YYYY-MM-DD' for an all-day event. Omit if no date."},
        "remind_at": {"type": "string", "description": "When to ping, 'YYYY-MM-DD HH:MM'. Omit for default."},
        "recurrence": {"type": "string", "enum": list(db.RECURRENCES)},
        "category": {"type": "string", "enum": ["work", "social", "personal"]},
        "notes": S,
    }, ["title", "kind", "category"]),
    _fn("list_items", "List open items: overdue, upcoming within days_ahead, and undated.", {
        "days_ahead": I,
        "category": {"type": "string", "enum": ["work", "social", "personal"]},
    }),
    _fn("update_item", "Change fields of an existing item by id.", {
        "id": I, "title": S, "due_at": S, "remind_at": S, "notes": S,
        "recurrence": {"type": "string", "enum": list(db.RECURRENCES)},
        "category": {"type": "string", "enum": ["work", "social", "personal"]},
    }, ["id"]),
    _fn("complete_item", "Mark an item done (recurring items move to next occurrence).", {"id": I}, ["id"]),
    _fn("delete_item", "Permanently delete an item.", {"id": I}, ["id"]),
    _fn("remember", "Store a lasting fact about the user (people, routines, preferences, goals).",
        {"fact": S}, ["fact"]),
    _fn("forget", "Delete a stored fact by id.", {"id": I}, ["id"]),
    _fn("log_checkin", "Record the user's answer to a check-in, e.g. their summary of work done today.", {
        "name": {"type": "string", "enum": list(checkins.BY_NAME)},
        "summary": {"type": "string", "description": "The user's update, in their own words."},
    }, ["name", "summary"]),
    _fn("checkin_history", "Get past check-in answers, e.g. what the user got done at work recently.", {
        "name": {"type": "string", "enum": list(checkins.BY_NAME)}, "days": I,
    }, ["name"]),
]


async def _add_item(a: dict) -> str:
    kind, due = a.get("kind", "task"), a.get("due_at")
    all_day = bool(due) and kind == "event" and len(str(due).strip()) == 10
    if kind == "event" and due and gcal.enabled():
        db.parse(due)  # validate before touching Google
        eid = await asyncio.to_thread(gcal.create_event, a["title"], str(due), a.get("recurrence", "none"),
                                      a.get("category", "personal"), a.get("notes", ""))
        await gcal.sync()
        item = db.find_gcal(eid)
        if item and a.get("remind_at"):
            item = db.update_item(item["id"], remind_at=a["remind_at"])
        if item:
            return "Saved to Google Calendar: " + db.describe(item)
        return f"Saved to Google Calendar: {a['title']} ({due}), beyond the {config.GCAL_SYNC_DAYS}-day sync window."
    saved = db.describe(db.add_item(**a, all_day=all_day))
    if gcal.enabled():
        return f"Saved in the bot only (NOT on Google Calendar; only events sync): {saved}"
    return "Saved: " + saved


WRITE_TOOLS = {"add_item", "update_item", "complete_item", "delete_item", "remember", "forget", "log_checkin"}


async def _run_tool(name: str, args: dict) -> str:
    try:
        args = {k: v for k, v in args.items() if v not in ("", None)}
        if "id" in args:
            args["id"] = int(args["id"])
        item = db.get_item(args["id"]) if "id" in args else None
        synced = bool(item and item.get("gcal_id") and gcal.enabled())
        if name == "add_item":
            return await _add_item(args)
        if name == "list_items":
            items = db.list_items(int(args.get("days_ahead") or 7), args.get("category") or None)
            return "\n".join(db.describe(i) for i in items) or "Nothing found."
        if name in ("update_item", "complete_item", "delete_item") and not item:
            return "No such item."
        if name == "update_item":
            args.pop("id")
            if synced:
                args.pop("recurrence", None)  # series rules are managed in Google Calendar
                await asyncio.to_thread(gcal.patch_event, item["gcal_id"], args.get("title"),
                                        args.get("due_at"), args.get("notes"), args.get("category"))
            return "Updated: " + db.describe(db.update_item(item["id"], **args))
        if name == "complete_item":
            return "Done: " + db.describe(db.complete_item(item["id"]))
        if name == "delete_item":
            if synced:
                await asyncio.to_thread(gcal.delete_event, item["gcal_id"])
            db.delete_item(item["id"])
            return "Deleted" + (" (also from Google Calendar)." if synced else ".")
        if name == "remember":
            return f"Remembered (fact #{db.add_fact(args['fact'])})."
        if name == "log_checkin":
            c = checkins.BY_NAME.get(args.get("name", ""))
            if not c:
                return "Unknown check-in."
            checkins.complete(c, args["summary"])
            return f"Logged today's {c.label}."
        if name == "checkin_history":
            rows = checkins.history(args.get("name", "work"), int(args.get("days") or 7))
            return "\n".join(f"{r['date']}: {r['response']}" for r in rows) or "No entries."
        if name == "forget":
            return "Forgotten." if db.delete_fact(args["id"]) else "No such fact."
        return f"Unknown tool {name}."
    except Exception as e:  # report back so the model can correct itself
        return f"Error: {e}"


def _context() -> tuple:
    """Shared prompt context: now, a calendar for relative dates, facts, open items, notes."""
    n = db.now()
    days = "\n".join(
        f"  {(n + timedelta(days=d)).strftime('%a %Y-%m-%d')}" + (" (today)" if d == 0 else "")
        for d in range(14)
    )
    facts = "\n".join(f"  [{f['id']}] {f['fact']}" for f in db.list_facts()) or "  (none yet)"
    work_day = db.is_work_day(n)
    items = db.list_items(3, exclude_category=None if work_day else "work")[:15]
    upcoming = "\n".join("  " + db.describe(i) for i in items) or "  (nothing)"
    day_note = "" if work_day else (
        "\n- It's the user's day off: work items are hidden above. Don't bring up work unless the user asks; "
        "if they ask, use list_items with category 'work'.")
    waiting = "".join(
        f"\n- PENDING: you asked for today's {c.label} (check-in '{c.name}') and the user hasn't answered. "
        f"If their message answers it, call log_checkin with name '{c.name}'."
        for c in checkins.CHECKINS if checkins.awaiting(c)
    )
    return n, days, facts, work_day, upcoming, day_note, waiting


# Item-handling rules shared by the text and voice prompts.
TOOL_RULES = """- When the user mentions an event, deadline, task or reminder they want kept, call add_item. Resolve relative dates using the calendar above. Use 24h 'YYYY-MM-DD HH:MM'.
- Choosing kind: something that HAPPENS at a date/time (party, dinner, appointment, meeting, trip, gathering) is an 'event'; events go to Google Calendar. A 'deadline' is when something is due. A 'task' is something to do. A 'reminder' is only a nudge to do something.
- Always set category: 'work' for anything job-related; 'social' for friends, family and events with others; 'personal' for errands, health, chores, bills and self-care.
- When the user shares a lasting personal detail (names, routines, preferences, goals), call remember. Never use remember for requests, questions or what someone said in passing.
- Use item ids when updating, completing or deleting. Call list_items to look further ahead.
- Never invent items, and never claim an action you did not take."""


def _spoken_when(due: str | None, all_day: bool) -> str:
    """'Saturday (in 2 days) at 4 PM' style, so the model doesn't have to do date math."""
    if not due:
        return "no date"
    d, today = db.parse(due), db.now().date()
    delta = (d.date() - today).days
    day = ("today" if delta == 0 else "tomorrow" if delta == 1 else f"{d.strftime('%A')} (in {delta} days)"
           if 1 < delta < 7 else f"{d.strftime('%A %B')} {d.day}" + (" (overdue)" if delta < 0 else ""))
    if all_day:
        return day + ", all day"
    t = d.strftime("%I:%M %p").lstrip("0").replace(":00", "")
    return f"{day} at {t}"


def _voice_items(work_day: bool) -> str:
    items = db.list_items(7, exclude_category=None if work_day else "work")[:12]
    return "\n".join(f"  - {i['title']} ({i['kind']}, {i['category']}): {_spoken_when(i['due_at'], i.get('all_day'))}"
                     for i in items) or "  (nothing planned)"


def _voice_system_prompt(speaker: str, guest: bool) -> str:
    """Prompt for voice: a conversation partner first, an organizer second."""
    n, days, facts, work_day, upcoming, day_note, waiting = _context()
    upcoming = _voice_items(work_day)
    tomorrow = (n + timedelta(days=1)).strftime("%A")
    if guest:
        waiting = ""  # a friend's words are never the user's check-in answer
    return f"""You are {config.BOT_NAME}, the user's personal assistant and friendly sidekick. Right now you're \
hanging out in a Discord voice call and talking out loud. Everything you write is spoken by a text-to-speech voice.

Who's talking: {speaker}. Their words were transcribed automatically, so expect small mistakes (misheard names or \
words); go with the most sensible meaning, or ask a quick question if it's really unclear. You hear everything \
they say, so it's an ongoing conversation: respond to each thing naturally, the way a friend on a call would.

How to talk:
- Respond to what they actually said, like a person would. A greeting or "are you there?" gets a natural, friendly \
reply such as "Hey! Yep, I'm here. What's up?", not a status report. Small talk, jokes and questions get a real, \
conversational answer.
- Don't bring up their schedule unless they ask about it, or something is starting within the hour.
- The facts, items and chat history below are about THE USER, not you. You don't have a job, a day or plans of \
your own. If they ask how you are, answer briefly and playfully, then turn it back to them.
- Keep every reply under about 35 words unless they explicitly ask for detail. Talk in flowing sentences, the way \
you'd say it out loud. Use contractions and a warm, relaxed tone, and feel free to show a little personality.
- Don't end with stock offers like "Let me know if you need anything else". Just stop when you've said it. \
Only ask a question back when you're actually curious or need an answer, not after every reply.
- Only use tools when they ask you to save, change or look something up. Questions and chit-chat need no tools.
- NEVER use lists, bullet points, numbering, line breaks, headings, markdown, emoji or item ids. Bullet points \
sound robotic when read aloud.
- When they ask about plans, tell them in a sentence or two, with the most important thing first, woven into \
normal speech rather than read out item by item. If there's a lot, say how many and mention the one or two that \
matter most, then offer to go through the rest.
- Their schedule is ONLY the open items listed below plus list_items results. Never mention an event, time or task \
that isn't literally there. Past chat messages are not their schedule. If nothing's planned for the day they asked \
about, just say it's free, and you can mention the next real item if it's soon.
- Say dates and times like people do: "tomorrow at 3", "Friday evening", "in about an hour". Never "2026-11-06 15:00".
- You can still save, change and look things up with your tools. Afterwards, confirm it casually in a few words.

Now: {n.strftime('%A %Y-%m-%d %H:%M')} ({config.TZ.key}). Today is {n.strftime('%A')}, tomorrow is {tomorrow}.
Calendar for resolving relative dates:
{days}

Background facts about the user. These are NOT their schedule, so don't bring them up unless the conversation is \
about them:
{facts}

The user's schedule: their open items for the next 7 days{"" if work_day else " (work hidden)"}. This is the \
complete list. Use it only when relevant, and don't recite it.
{upcoming}

Rules for items:
{TOOL_RULES}{day_note}{waiting}"""


def _system_prompt() -> str:
    n, days, facts, work_day, upcoming, day_note, waiting = _context()
    return f"""You are {config.BOT_NAME}, the user's personal assistant for organizing work and social life.
Be brief, friendly and practical. Reply in plain text suitable for Discord.

Now: {n.strftime('%A %Y-%m-%d %H:%M')} ({config.TZ.key})
Calendar for resolving relative dates:
{days}

What you know about the user:
{facts}

Open items (overdue + next 3 days{"" if work_day else ", work hidden"}):
{upcoming}

Rules:
- When the user mentions an event, deadline, task or reminder, call add_item. Resolve relative dates using the calendar above. Use 24h 'YYYY-MM-DD HH:MM'.
- Choosing kind: something that HAPPENS at a date/time (party, dinner, appointment, meeting, trip, gathering) is an 'event'; events go to Google Calendar. A 'deadline' is when something is due. A 'task' is something to do. A 'reminder' is only a nudge to do something.
- If the user describes an event AND something to prepare for it, call add_item TWICE: one 'event' for the occasion, and one 'task' (with remind_at) for the preparation.
- Only say something is on Google Calendar if a tool result said "Saved to Google Calendar" or the item is marked 📆. If unsure, check with list_items. Never claim an action you did not take.
- Always set category: 'work' for anything job-related (meetings, projects, deadlines, coworkers, on-call); 'social' for friends, family and events with others; 'personal' for errands, health, chores, bills and self-care.
- When the user shares a lasting personal detail (names, routines, preferences, goals), call remember.
- Use item ids from the lists when updating, completing or deleting. Call list_items to look further ahead.
- Items marked 📆 are synced with Google Calendar; changes to them update Google too.
- A message starting with "[<something> logged]" is a check-in answer that is already saved: acknowledge it in one encouraging line and add any future tasks or deadlines it mentions.
- Never invent items. Confirm what you saved in one short line.{day_note}{waiting}"""


async def warm_up() -> str:
    """Check Ollama is reachable and the model exists, and load it into memory. Returns a status line."""
    try:
        models = {m.model for m in (await client.list()).models}
    except Exception as e:
        return f"✗ Ollama not reachable at {config.OLLAMA_HOST} ({e.__class__.__name__}). Is it running?"
    if config.OLLAMA_MODEL not in models and f"{config.OLLAMA_MODEL}:latest" not in models:
        return f"✗ Model '{config.OLLAMA_MODEL}' not downloaded. Run: ollama pull {config.OLLAMA_MODEL}"
    # An empty prompt makes Ollama load the model without generating anything.
    await client.generate(model=config.OLLAMA_MODEL, prompt="", keep_alive=config.OLLAMA_KEEP_ALIVE,
                          options={"num_ctx": config.OLLAMA_NUM_CTX})
    loaded = {m.model: m for m in (await client.ps()).models}
    m = loaded.get(config.OLLAMA_MODEL) or loaded.get(f"{config.OLLAMA_MODEL}:latest")
    if not m:
        return f"✓ Model '{config.OLLAMA_MODEL}' ready"
    vram = (m.size_vram or 0) / 1e9
    where = "100% GPU" if m.size_vram and m.size_vram >= m.size else f"{vram:.1f} GB on GPU, rest on CPU (slow!)"
    return f"✓ Model '{config.OLLAMA_MODEL}' loaded: {m.size / 1e9:.1f} GB, {where}"


GUEST_BLOCKED_TOOLS = {"log_checkin", "remember", "forget"}  # a friend's words aren't facts/check-ins about the user

# Stock closing lines small models love to add; they sound canned when spoken.
# Only whole trailing sentences are removed, never part of one.
_SIGN_OFF = re.compile(r"(?:^|(?<=[.!?]))\s*(?:Just\s+)?(?:let me know|feel free|is there anything else|"
                       r"anything else (?:I can|you need)|hope (?:that|this) helps)[^.!?]*[.!?]*\s*$", re.I)


def _strip_sign_off(text: str) -> str:
    stripped = _SIGN_OFF.sub("", text.strip()).strip()
    return stripped or text.strip()


def _looks_written(text: str) -> bool:
    """True if a reply reads like writing (lists, several lines, markdown) or is too long to say comfortably."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return (len(lines) > 2 or bool(re.search(r"^\s*(?:[-*•]|\d+[.)])\s", text, re.M))
            or "**" in text or "#" in re.sub(r"#\d", "", text) or len(text.split()) > 55)


async def _speechify(reply: str) -> str:
    """Second, tool-free pass: rewrite a written-style reply as one to three spoken sentences."""
    resp = await client.chat(
        model=config.OLLAMA_MODEL, keep_alive=config.OLLAMA_KEEP_ALIVE,
        options={"num_ctx": 2048, "temperature": 0.5},
        **({"think": False} if config.OLLAMA_THINK is not None else {}),
        messages=[
            {"role": "system", "content": "Rewrite the assistant's message as what a friendly person would say "
                                          "out loud on a voice call: at most two flowing, natural sentences and "
                                          "under 35 words. Summarize, don't enumerate: if there are several "
                                          "things, mention how many and the one or two that matter most, and "
                                          "offer the rest. No lists, line breaks, markdown, emoji or ids. Say "
                                          "times naturally (\"tomorrow at 3\"). No sign-off like \"let me know if "
                                          "you need anything\". Output only the rewritten words."},
            {"role": "user", "content": reply},
        ])
    return (resp.message.content or "").strip() or reply


async def chat(user_text: str, voice: str | None = None, guest: bool = False,
               history: list[dict] | None = None) -> str:
    """voice: who's speaking (e.g. "the user"), when the message came from a voice call.
    Voice replies are conversational and meant to be spoken. guest: a friend in the call, not the user.
    history: the conversation so far in this call. Voice mode uses it instead of the DM history,
    which is full of formatted lists and old topics the model would otherwise drag in."""
    tools = [t for t in TOOLS if t["function"]["name"] not in GUEST_BLOCKED_TOOLS] if guest else TOOLS
    if voice:
        messages = [{"role": "system", "content": _voice_system_prompt(voice, guest)}]
        messages += history or []
    else:
        messages = [{"role": "system", "content": _system_prompt()}]
        messages += db.recent_messages(config.HISTORY_MESSAGES)
    messages.append({"role": "user", "content": user_text})

    kwargs = {"model": config.OLLAMA_MODEL, "tools": tools, "keep_alive": config.OLLAMA_KEEP_ALIVE,
              "options": {"num_ctx": config.OLLAMA_NUM_CTX, "temperature": 0.7 if voice else 0.3}}
    if config.OLLAMA_THINK is not None:
        kwargs["think"] = config.OLLAMA_THINK

    reply, receipts = "", []
    for _ in range(MAX_TOOL_ROUNDS):
        resp = await client.chat(messages=messages, **kwargs)
        msg = resp.message
        messages.append(msg)
        if not msg.tool_calls:
            reply = (msg.content or "").strip()
            break
        for call in msg.tool_calls:
            result = await _run_tool(call.function.name, dict(call.function.arguments or {}))
            print(f"[tool] {call.function.name}({json.dumps(call.function.arguments)}) -> {result}")
            messages.append({"role": "tool", "content": result, "tool_name": call.function.name})
            if call.function.name in WRITE_TOOLS:
                receipts.append(result)
    reply = reply or "Done."
    if voice and _looks_written(reply):
        print("[voice] reply was list-formatted or long; rewriting for speech")
        reply = await _speechify(reply)
    if voice:
        reply = _strip_sign_off(reply)
    # Ground truth from the code, not the model: what was actually changed this turn.
    # "-# " renders as small grey subtext in Discord.
    if receipts:
        reply += "\n" + "\n".join(f"-# ✔ {r}" for r in receipts)

    db.add_message("user", user_text)
    db.add_message("assistant", reply)
    return reply
