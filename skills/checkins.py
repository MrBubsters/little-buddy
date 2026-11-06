"""Check-ins skill: the bot asks a question on a schedule and keeps nagging until it's answered.

Add more by appending to CHECKINS (e.g. an evening journal or a Friday weekly review).
For one-off or free-form follow-ups, the assistant can also schedule its own jobs (see skills/jobs.py).
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Literal

import config
from core import db
from core.app import app
from core.skills import Forward, command
from core.tools import ToolContext, tool

_days = db.parse_days


@dataclass(frozen=True)
class Checkin:
    name: str        # short id, used in tools and the DB
    label: str       # human name
    start: str       # "HH:MM" first ask
    until: str       # "HH:MM" stop nagging for the day
    nag_minutes: int
    days: tuple[int, ...]
    prompt: str
    nag: str


CHECKINS = [
    Checkin(
        name="work",
        label="work update",
        start=config.WORK_CHECKIN_TIME,
        until=config.WORK_CHECKIN_UNTIL,
        nag_minutes=config.WORK_CHECKIN_NAG_MINUTES,
        days=_days(config.WORK_CHECKIN_DAYS),
        prompt="📝 Time for your work update! What did you get done today?\n"
               "Reply to this message (or just tell me). `!skip` if you're off today.",
        nag="⏳ Still waiting on today's work update. What did you get done? (reply here, or `!skip`)",
    ),
]
BY_NAME = {c.name: c for c in CHECKINS}

db.ensure_schema(
    "CREATE TABLE IF NOT EXISTS checkin_logs("
    " id INTEGER PRIMARY KEY, name TEXT NOT NULL, date TEXT NOT NULL,"
    " response TEXT NOT NULL, created_at TEXT NOT NULL);"
)


def _key(c: Checkin, field: str) -> str:
    return f"checkin:{c.name}:{field}"


def _today() -> str:
    return db.now().strftime("%Y-%m-%d")


def is_done(c: Checkin) -> bool:
    return db.get_meta(_key(c, "done")) == _today()


def in_window(c: Checkin, n: datetime | None = None) -> bool:
    n = n or db.now()
    return n.weekday() in c.days and c.start <= n.strftime("%H:%M") < c.until


def asked_today(c: Checkin) -> bool:
    last = db.get_meta(_key(c, "last"))
    return bool(last) and last[:10] == _today()


def awaiting(c: Checkin) -> bool:
    """Asked today and not answered yet (used to steer the LLM)."""
    return asked_today(c) and not is_done(c)


def due(c: Checkin) -> bool:
    """Should we send the prompt/nag right now?"""
    n = db.now()
    if is_done(c) or not in_window(c, n):
        return False
    last = db.get_meta(_key(c, "last"))
    if not last or last[:10] != _today():
        return True
    return n - db.parse(last) >= timedelta(minutes=c.nag_minutes)


def message_for(c: Checkin) -> str:
    return c.nag if asked_today(c) else c.prompt


def record_sent(c: Checkin, message_id: int) -> None:
    today = _today()
    ids = db.get_meta(_key(c, "msgs")) or ""
    ids = ids if ids.startswith(today + ":") else today + ":"
    db.set_meta(_key(c, "msgs"), ids + f"{message_id},")
    db.set_meta(_key(c, "last"), db.fmt(db.now()))


def match_reply(message_id: int) -> Checkin | None:
    """Which check-in (if any) a Discord message id belongs to — for 'reply to this message'."""
    today = _today()
    for c in CHECKINS:
        ids = db.get_meta(_key(c, "msgs")) or ""
        if ids.startswith(today + ":") and str(message_id) in ids.split(":", 1)[1].split(","):
            return c
    return None


def complete(c: Checkin, response: str) -> None:
    db.conn.execute("INSERT INTO checkin_logs(name,date,response,created_at) VALUES(?,?,?,?)",
                    (c.name, _today(), response.strip(), db.fmt(db.now())))
    db.conn.commit()
    db.set_meta(_key(c, "done"), _today())


def skip_today() -> list[Checkin]:
    """Skip every check-in still outstanding today (asked, or scheduled later today)."""
    n = db.now()
    skipped = []
    for c in CHECKINS:
        if n.weekday() in c.days and not is_done(c) and n.strftime("%H:%M") < c.until:
            db.set_meta(_key(c, "done"), _today())
            skipped.append(c)
    return skipped


def history(name: str, days: int = 7) -> list[dict]:
    since = (db.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = db.conn.execute(
        "SELECT date, response FROM checkin_logs WHERE name=? AND date>=? ORDER BY date, id", (name, since))
    return [dict(r) for r in rows]


# ---------- skill hooks ----------
Name = Literal[tuple(BY_NAME)]

GUIDE = """Check-ins:
- A message starting with "[<something> logged]" is a check-in answer that's already saved. Compare it with \
the open items: complete_item each one it says is finished, update_item notes for progress, and add_item new \
tasks or deadlines it mentions. Then acknowledge it in one encouraging line.
- Use checkin_history to read back past check-in answers."""


@tool(writes=True, owner_only=True)
def log_checkin(name: Name, summary: Annotated[str, "The user's update, in their own words."]) -> str:
    """Record the user's answer to a check-in, e.g. their summary of work done today."""
    c = BY_NAME.get(name)
    if not c:
        return "Error: unknown check-in."
    complete(c, summary)
    return (f"Logged today's {c.label}.\nNow compare it with the open items: call complete_item for each one it "
            f"says is finished, update_item notes for progress, add_item for new tasks it mentions.")


@tool(owner_only=True)
def checkin_history(name: Name, days: int = 7) -> str:
    """Get past check-in answers, e.g. what the user got done at work recently."""
    rows = history(name, int(days or 7))
    return "\n".join(f"{r['date']}: {r['response']}" for r in rows) or "No entries."


def context(ctx: ToolContext, query: str) -> str | None:
    if not ctx.is_owner:
        return None  # someone else's words are never the owner's check-in answer
    waiting = [f"PENDING: you asked for today's {c.label} (check-in '{c.name}') and {config.owner()} hasn't "
               f"answered. If their message answers it, call log_checkin with name '{c.name}'."
               for c in CHECKINS if awaiting(c)]
    return "\n".join(waiting) or None


def preprocess(text: str, ctx: ToolContext, meta: dict) -> str:
    """A reply to a check-in message is logged directly."""
    c = match_reply(meta["reply_to"]) if ctx.is_owner and meta.get("reply_to") else None
    if not c:
        return text
    complete(c, text)
    return f"[{c.label} logged] {text}"


@command("!update", "log today's work update: `!update <what you did>`")
def _update_cmd(args: str, ctx):
    if not args:
        return "Usage: `!update what you got done today`"
    c = BY_NAME["work"]
    complete(c, args)
    return Forward(f"[{c.label} logged] {args}")


@command("!skip", "skip today's check-ins (vacation, sick day)")
def _skip_cmd(args: str, ctx) -> str:
    skipped = skip_today()
    return f"👍 Skipped today's {', '.join(c.label for c in skipped)}." if skipped else "Nothing to skip today."


@command("!worklog", "your work updates from the last 7 days")
def _worklog_cmd(args: str, ctx) -> str:
    rows = history("work", 7)
    return "\n".join(f"**{r['date']}** {r['response']}" for r in rows) or "No work updates in the last 7 days."


async def on_tick():
    for c in CHECKINS:
        if due(c):
            msg_id = await app.notify(message_for(c))
            record_sent(c, msg_id or 0)
