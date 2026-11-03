"""Recurring check-ins: the bot asks a question on a schedule and keeps nagging until answered.

Add more by appending to CHECKINS (e.g. an evening journal or a Friday weekly review).
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

import config
import db

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _days(spec: str) -> tuple[int, ...]:
    return tuple(DAY_NAMES.index(d.strip()[:3].lower()) for d in spec.split(",") if d.strip())


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

db.conn.execute(
    "CREATE TABLE IF NOT EXISTS checkin_logs("
    " id INTEGER PRIMARY KEY, name TEXT NOT NULL, date TEXT NOT NULL,"
    " response TEXT NOT NULL, created_at TEXT NOT NULL)"
)
db.conn.commit()


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
