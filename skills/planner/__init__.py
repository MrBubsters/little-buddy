"""Planner skill: events, deadlines, tasks and reminders, the morning digest, and Google Calendar sync."""
import asyncio
import time
from datetime import timedelta
from typing import Annotated, Literal

import config
from core import db, skills
from core.app import app
from core.skills import command
from core.tools import ToolContext, tool
from skills.planner import gcal, store

Kind = Literal["event", "deadline", "task", "reminder"]
Category = Literal["work", "social", "personal"]
Recurrence = Literal["none", "daily", "weekdays", "weekly", "monthly", "yearly"]
When = Annotated[str, f"Local time: {db.WHEN_HELP}. A date alone = all-day event. Omit if no date."]
RemindAt = Annotated[str, f"When to ping: {db.WHEN_HELP}. Omit for the default."]


def _norm(ts: str | None) -> str | None:
    return db.normalize(ts) if ts else None

GUIDE = """Planner (events, deadlines, tasks, reminders):
- When {owner} mentions something they want kept or reminded about, call add_item. Resolve relative dates \
with the calendar below. For times, prefer passing \
the user's own words ('friday 5pm', 'tomorrow 9am', 'in 2 hours'): the tool works out the exact date.
- kind: something that HAPPENS at a time (appointment, meeting, party, trip) is an 'event' (events go to Google \
Calendar). A 'deadline' is when something is due, a 'task' is something to do, a 'reminder' is just a nudge.
- category: 'work' for anything job-related; 'social' for friends, family and things with others; 'personal' \
for errands, health, chores, bills and self-care.
- If they describe an event AND something to prepare for it, add both: the event, and a task with remind_at.
- Ongoing work (projects, tickets, to-dos) goes here as one 'task' per piece of work, with no date unless \
there's a deadline and its current status in notes. Never keep a task list in memory: memories can't be checked off.
- When {owner} says something is finished (in passing, in a check-in, by name or description), call \
complete_item for EACH open item it covers. They don't need to give an id: pass words from the title. When \
a task progresses but isn't finished, update_item its notes.
- The open items are the only source of truth for what's outstanding. To answer "what's left to do?", call \
list_items (days_ahead 30) and answer from its result, not from memories or earlier chat.
- For what {owner} did, finished or accomplished (today, this week, ...), call recap and answer only from its \
result. Never present open tasks or upcoming events as accomplishments.
- Every dated item says how far it is from today ("tomorrow", "2 days ago"). Trust those labels instead of \
working out dates yourself.
- Call list_items to look further ahead.
- 📆 marks items synced with Google Calendar; changes to them update Google too. Only say something is on Google \
Calendar if a tool result said so or it's marked 📆.
- The schedule is one part of {owner}'s life, not the topic of every conversation. Bring it up when they ask, \
when it bears on what they're talking about, or when something is starting soon."""


# ---------- tools ----------
@tool(writes=True)
async def add_item(title: str, kind: Kind, category: Category, due_at: When = None, remind_at: RemindAt = None,
                   recurrence: Recurrence = "none", notes: str = "") -> str:
    """Save an event, deadline, task or reminder. Dated events also go to Google Calendar."""
    due_at, remind_at = _norm(due_at), _norm(remind_at)
    all_day = bool(due_at) and kind == "event" and len(str(due_at).strip()) == 10
    if kind == "event" and due_at and gcal.enabled():
        db.parse(due_at)  # validate before touching Google
        eid = await asyncio.to_thread(gcal.create_event, title, str(due_at), recurrence, category, notes)
        await gcal.sync()
        item = store.find_gcal(eid)
        if item and remind_at:
            item = store.update_item(item["id"], remind_at=remind_at)
        if item:
            return "Saved to Google Calendar: " + store.describe(item)
        return f"Saved to Google Calendar: {title} ({due_at}), beyond the {config.GCAL_SYNC_DAYS}-day sync window."
    saved = store.describe(store.add_item(title, kind, due_at, remind_at, recurrence, category, notes, all_day))
    if gcal.enabled():
        return f"Saved in the bot only (NOT on Google Calendar; only events sync): {saved}"
    return "Saved: " + saved


@tool
def list_items(days_ahead: int = 7, category: Category = None) -> str:
    """List open items: overdue, upcoming within days_ahead, and undated."""
    items = store.list_items(int(days_ahead or 7), category or None)
    return "\n".join(store.describe(i) for i in items) or "Nothing found."


ItemRef = Annotated[str, "The item's id ('#12'), or words from its title ('the demo', 'go links', 'PRX-558')."]


def _item(ref: str) -> dict:
    found = store.find_open(ref)
    if len(found) == 1:
        return found[0]
    if found:
        raise LookupError(f"'{ref}' matches several items. Call again with the id of the right one:\n"
                          + "\n".join(store.describe(i) for i in found[:6]))
    raise LookupError(f"no open item matches '{ref}'. Call list_items with days_ahead 60 to see them; "
                      f"if it was never saved, say so instead of claiming it's updated.")


def _synced(item: dict) -> bool:
    return bool(item.get("gcal_id") and gcal.enabled())


@tool(writes=True)
async def update_item(item: ItemRef, title: str = None, due_at: When = None, remind_at: RemindAt = None,
                      notes: str = None, recurrence: Recurrence = None, category: Category = None) -> str:
    """Change fields of an existing item (e.g. a new time, or its latest status in notes)."""
    item = _item(item)
    id = item["id"]
    due_at, remind_at = _norm(due_at), _norm(remind_at)
    fields = {k: v for k, v in dict(title=title, due_at=due_at, remind_at=remind_at, notes=notes,
                                    recurrence=recurrence, category=category).items() if v is not None}
    if _synced(item):
        fields.pop("recurrence", None)  # series rules are managed in Google Calendar
        await asyncio.to_thread(gcal.patch_event, item["gcal_id"], fields.get("title"), fields.get("due_at"),
                                fields.get("notes"), fields.get("category"))
    return "Updated: " + store.describe(store.update_item(id, **fields))


@tool(writes=True)
def complete_item(item: ItemRef) -> str:
    """Mark an item done when the user says they finished it (recurring items move to their next occurrence)."""
    return "Done: " + store.describe(store.complete_item(_item(item)["id"]))


@tool(writes=True)
async def delete_item(item: ItemRef) -> str:
    """Permanently delete an item (for mistakes or cancelled plans; use complete_item for finished work)."""
    item = _item(item)
    id = item["id"]
    if _synced(item):
        await asyncio.to_thread(gcal.delete_event, item["gcal_id"])
    store.delete_item(id)
    return f"Deleted #{id} {item['title']}" + (" (also from Google Calendar)." if _synced(item) else ".")


@tool
def recap(days: Annotated[int, "How far back. 'This week' = days since Monday, plus 1."] = 7,
          category: Category = None) -> str:
    """What the user got done and what happened in the last N days: finished items, check-in answers and
    past events. Use for 'what did I do / accomplish this week?'."""
    n = db.now()
    days = max(1, int(days or 7))
    since = (n - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    keep = (lambda i: i["category"] == category) if category else (lambda i: True)
    done = [i for i in store.done_since(since) if keep(i)]
    events = [i for i in store.events_between(since, n) if keep(i)]
    parts = [f"Recap from {since.strftime('%a %b %d')} to now ({n.strftime('%a %b %d %H:%M')}). "
             f"Only what's listed here happened; upcoming and open items are NOT accomplishments.",
             "Finished items:\n" + ("\n".join("  " + store.describe(i) for i in done) or "  (none recorded)"),
             "Events that already happened:\n" + ("\n".join("  " + store.describe(i) for i in events) or "  (none)")]
    checkins = skills.SKILLS.get("checkins")
    if checkins and category in (None, "work"):
        logs = checkins.history("work", days - 1)
        parts.append("Work check-in answers:\n"
                     + ("\n".join(f"  {r['date']}: {r['response']}" for r in logs) or "  (none)"))
    return "\n\n".join(parts)


# ---------- prompt context ----------
def context(ctx: ToolContext, query: str) -> str:
    n = db.now()
    # One date per line: small models misread weekdays from a run-on list.
    days = "\n".join(f"  {(n + timedelta(days=d)).strftime('%A %Y-%m-%d')}" + (" (today)" if d == 0 else "")
                     for d in range(14))
    work_day = store.is_work_day(n)
    items = store.list_items(3, exclude_category=None if work_day else "work")
    shown = items[:10]
    lines = [f"Calendar for resolving dates (\"next Friday\", \"every Monday\"):\n{days}",
             f"{config.owner().capitalize()}'s open items (overdue + next 3 days{'' if work_day else ', work hidden'})."
             f" None of these are done, and anything 'tomorrow' or 'in N days' hasn't happened yet:"]
    lines += ["  " + store.describe(i) for i in shown] or ["  (nothing)"]
    if len(items) > len(shown):
        lines.append(f"  (+{len(items) - len(shown)} more: use list_items)")
    if not work_day:
        lines.append("It's their day off: work items are hidden. Don't bring up work unless they ask; "
                     "if they do, use list_items with category 'work'.")
    return "\n".join(lines)


# ---------- agenda / digest ----------
def agenda(days: int, include_work: bool | None = None) -> str:
    """Agenda text. Work items are hidden on non-work days unless include_work=True."""
    n = db.now()
    if include_work is None:
        include_work = store.is_work_day(n)
    today_end = db.fmt(n.replace(hour=23, minute=59))
    overdue, today, later, undated = [], [], [], []
    hidden = 0 if include_work else len(store.list_items(days, category="work"))
    for it in store.list_items(days, exclude_category=None if include_work else "work"):
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
            parts.append(f"**{label}**\n" + "\n".join("• " + store.describe(i) for i in group))
    if len(parts) == 1:
        parts.append("Nothing scheduled. 🎉")
    if hidden:
        parts.append(f"_💼 {hidden} work item(s) hidden for the weekend. `!agenda all` to see them._")
    return "\n\n".join(parts)


@command("!agenda", "overdue + the next 3 days (`all` includes work on days off)")
def _agenda_cmd(args: str, ctx) -> str:
    return agenda(3, include_work=True if args.lower() == "all" else None)


@command("!digest", "the morning digest, now")
def _digest_cmd(args: str, ctx) -> str:
    return agenda(3)


@command("!week", "the next 7 days (`all` includes work on days off)")
def _week_cmd(args: str, ctx) -> str:
    return agenda(7, include_work=True if args.lower() == "all" else None)


@command("!sync", "sync Google Calendar now")
async def _sync_cmd(args: str, ctx) -> str:
    if not gcal.enabled():
        return "Google Calendar isn't set up (see README)."
    try:
        n = await gcal.sync()
        return f"📆 Synced {n} events from Google Calendar.\n\n" + agenda(3)
    except Exception as e:
        return f"⚠️ Sync failed: {e}"


# ---------- background ----------
_last_sync = 0.0


async def on_start():
    if config.GCAL_ENABLED and not gcal.enabled():
        print(f"[gcal] enabled but {config.GCAL_TOKEN} missing — run: .venv\\Scripts\\python -m skills.planner.gcal")


async def on_tick():
    global _last_sync
    # Sync first, so reminders and the digest see fresh events.
    if gcal.enabled() and time.monotonic() - _last_sync >= config.GCAL_SYNC_MINUTES * 60:
        _last_sync = time.monotonic()
        try:
            print(f"[gcal] synced {await gcal.sync()} events")
        except Exception as e:
            print(f"[gcal] sync failed: {e!r}")

    for it in store.due_reminders():
        if it["category"] == "work" and not store.is_work_day():
            store.postpone_reminder(it, store.next_work_start())  # no work pings on days off
            continue
        when = f" at {db.pretty(it['due_at'])}" if it["due_at"] else ""
        await app.notify(f"⏰ **Reminder:** {it['title']}{when}" + (f"\n{it['notes']}" if it["notes"] else ""))
        store.mark_reminded(it)

    n = db.now()
    today = n.strftime("%Y-%m-%d")
    if n.strftime("%H:%M") >= config.DIGEST_TIME and db.get_meta("last_digest") != today:
        db.set_meta("last_digest", today)
        await app.notify("☀️ Good morning!\n" + agenda(3))
