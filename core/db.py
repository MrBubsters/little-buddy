"""Shared SQLite connection, time helpers, chat history and key/value meta.

Skills own their own tables: call `ensure_schema()` with CREATE statements and
`add_column()` for migrations, at import time.
"""
import calendar
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta

import config

FMT = "%Y-%m-%dT%H:%M"
RECURRENCES = ("none", "hourly", "daily", "weekdays", "weekly", "monthly", "yearly")

os.makedirs(os.path.dirname(os.path.abspath(config.DB_PATH)), exist_ok=True)
conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row


def ensure_schema(sql: str) -> None:
    conn.executescript(sql)
    conn.commit()


def columns(table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def add_column(table: str, name: str, decl: str) -> None:
    if name not in columns(table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        conn.commit()


ensure_schema("""
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, role TEXT NOT NULL, content TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
""")
add_column("messages", "conversation", "TEXT NOT NULL DEFAULT 'main'")
add_column("messages", "created_at", "TEXT")
conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation, id)")
conn.commit()


# ---------- time helpers ----------
def now() -> datetime:
    return datetime.now(config.TZ).replace(tzinfo=None, second=0, microsecond=0)


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_UNITS = {"min": "minutes", "minute": "minutes", "hr": "hours", "hour": "hours", "day": "days", "week": "weeks"}
_TIME = re.compile(r"(?:\bat\s+)?(?:\b(noon|midnight)|\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m?\.?)\s*$"
                   r"|(?:\bat\s+|^|\s)(\d{1,2}):(\d{2})\s*$|\bat\s+(\d{1,2})\s*$")
WHEN_HELP = "'YYYY-MM-DD HH:MM', 'YYYY-MM-DD', or words like 'friday 17:00', 'tomorrow 9am', 'in 2 hours'"


def normalize(ts: str) -> str:
    """Turn a time the model or user wrote into 'YYYY-MM-DD HH:MM' (or 'YYYY-MM-DD' if no time was given).

    Small models are unreliable at calendar arithmetic, so tools accept phrases like "friday 5pm",
    "next monday", "tomorrow at 9" or "in 2 hours" and the date math happens here instead.
    """
    s = str(ts).strip()
    try:
        datetime.fromisoformat(s.replace(" ", "T"))
        return s
    except ValueError:
        pass
    low = re.sub(r"[,]+", " ", s.lower()).strip()
    low = re.sub(r"^(?:every|each|starting)\s+", "", low)  # the repeat is a separate argument
    n = now()
    m = re.fullmatch(r"in\s+(\d+|an?|one)\s+(min|minute|hr|hour|day|week)s?", low)
    if m:
        qty = 1 if m[1] in ("a", "an", "one") else int(m[1])
        return (n + timedelta(**{_UNITS[m[2]]: qty})).strftime("%Y-%m-%d %H:%M")

    hour = minute = None
    t = _TIME.search(low)
    if t:
        if t[1]:
            hour, minute = (12, 0) if t[1] == "noon" else (0, 0)
        elif t[2]:
            hour, minute = int(t[2]) % 12 + (12 if t[4] == "p" else 0), int(t[3] or 0)
        elif t[5]:
            hour, minute = int(t[5]), int(t[6])
        else:  # "at 5": people mean 5 pm for 1-7
            hour, minute = int(t[7]) + (12 if 1 <= int(t[7]) <= 7 else 0), 0
        low = low[:t.start()].strip()
    if hour is not None and not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"'{ts}' isn't a valid time of day.")

    day = None
    if low in ("", "today"):
        day = n.date()
        if low == "" and hour is not None and (hour, minute) <= (n.hour, n.minute):
            day += timedelta(days=1)  # "5pm" when it's already 6pm means tomorrow
    elif low == "tonight":
        day, hour, minute = n.date(), 20 if hour is None else hour, minute or 0
    elif low == "tomorrow":
        day = n.date() + timedelta(days=1)
    elif low == "day after tomorrow":
        day = n.date() + timedelta(days=2)
    else:
        m = re.fullmatch(r"(?:(this|next|on)\s+)?(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*", low)
        if m:
            target = next(i for i, w in enumerate(_WEEKDAYS) if w.startswith(m[2][:3]))
            ahead = (target - n.weekday()) % 7
            if ahead == 0 and (m[1] == "next" or (hour is not None and (hour, minute) <= (n.hour, n.minute))):
                ahead = 7
            day = n.date() + timedelta(days=ahead)
    if day is None:
        raise ValueError(f"Couldn't understand the time '{ts}'. Use {WHEN_HELP}.")
    return day.isoformat() + ("" if hour is None else f" {hour:02d}:{minute:02d}")


def parse(ts: str | None) -> datetime | None:
    """Accept 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM', ISO with tz, or a phrase (see normalize). Date-only -> 09:00."""
    if not ts:
        return None
    ts = normalize(ts).replace(" ", "T")
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo:
        dt = dt.astimezone(config.TZ).replace(tzinfo=None)
    if len(ts) == 10:
        dt = dt.replace(hour=9)
    return dt.replace(second=0, microsecond=0)


def fmt(dt: datetime | None) -> str | None:
    return dt.strftime(FMT) if dt else None


def pretty(ts: str | None) -> str:
    if not ts:
        return "no date"
    return datetime.strptime(ts, FMT).strftime("%a %b %d %H:%M")


DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def parse_days(spec: str) -> tuple[int, ...]:
    return tuple(DAY_NAMES.index(d.strip()[:3].lower()) for d in spec.split(",") if d.strip())


def shift(dt: datetime, rec: str) -> datetime | None:
    """Next occurrence of `dt` for a recurrence rule (see RECURRENCES)."""
    if rec == "hourly":
        return dt + timedelta(hours=1)
    if rec == "daily":
        return dt + timedelta(days=1)
    if rec == "weekly":
        return dt + timedelta(weeks=1)
    if rec == "weekdays":
        dt += timedelta(days=1)
        while dt.weekday() >= 5:
            dt += timedelta(days=1)
        return dt
    if rec == "monthly":
        y, m = dt.year + dt.month // 12, dt.month % 12 + 1
        return dt.replace(year=y, month=m, day=min(dt.day, calendar.monthrange(y, m)[1]))
    if rec == "yearly":
        try:
            return dt.replace(year=dt.year + 1)
        except ValueError:  # Feb 29
            return dt.replace(year=dt.year + 1, day=28)
    return None


# ---------- chat history (one thread per conversation id: "main", or any id a front-end picks) ----------
def add_message(role: str, content: str, conversation: str = "main") -> None:
    conn.execute("INSERT INTO messages(role,content,conversation,created_at) VALUES(?,?,?,?)",
                 (role, content, conversation, fmt(now())))
    conn.commit()


def recent_messages(n: int, conversation: str = "main") -> list[dict]:
    rows = conn.execute("SELECT role, content FROM messages WHERE conversation=? ORDER BY id DESC LIMIT ?",
                        (conversation, n)).fetchall()
    return [dict(r) for r in reversed(rows)]


def clear_messages(conversation: str | None = "main") -> None:
    if conversation is None:
        conn.execute("DELETE FROM messages")
    else:
        conn.execute("DELETE FROM messages WHERE conversation=?", (conversation,))
    conn.commit()


# ---------- meta ----------
def get_meta(key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))
    conn.commit()


def get_json(key: str, default=None):
    raw = get_meta(key)
    return default if raw is None else json.loads(raw)


def set_json(key: str, value) -> None:
    set_meta(key, json.dumps(value))
