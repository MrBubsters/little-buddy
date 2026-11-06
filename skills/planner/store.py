"""Planner storage: items (events, deadlines, tasks, reminders) and the Google Calendar mirror."""
import re
from datetime import datetime, timedelta

import config
from core import db
from core.db import conn, fmt, now, parse, pretty

KINDS = ("event", "deadline", "task", "reminder")
CATEGORIES = ("work", "social", "personal")
RECURRENCES = tuple(r for r in db.RECURRENCES if r != "hourly")
CATEGORY_ICONS = {"work": "💼", "social": "🎉", "personal": "🏠"}

db.ensure_schema("""
CREATE TABLE IF NOT EXISTS items(
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'task',
    title TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'personal',
    due_at TEXT,
    remind_at TEXT,
    recurrence TEXT NOT NULL DEFAULT 'none',
    done INTEGER NOT NULL DEFAULT 0,
    reminded INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
""")
db.add_column("items", "gcal_id", "TEXT")
db.add_column("items", "all_day", "INTEGER NOT NULL DEFAULT 0")
db.add_column("items", "done_at", "TEXT")
conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_items_gcal ON items(gcal_id)")
conn.commit()

WORK_DAYS = set(db.parse_days(config.WORK_DAYS))


def is_work_day(dt: datetime | None = None) -> bool:
    return (dt or now()).weekday() in WORK_DAYS


def next_work_start(dt: datetime | None = None) -> datetime:
    """Next work day at DIGEST_TIME (where postponed work reminders land)."""
    dt = dt or now()
    h, m = map(int, config.DIGEST_TIME.split(":"))
    d = dt + timedelta(days=1)
    while d.weekday() not in WORK_DAYS:
        d += timedelta(days=1)
    return d.replace(hour=h, minute=m, second=0, microsecond=0)


def _default_remind(due: datetime | None, kind: str) -> datetime | None:
    if not due:
        return None
    lead = 0 if kind == "reminder" else config.REMIND_LEAD_MINUTES
    return due - timedelta(minutes=lead)


def add_item(title, kind="task", due_at=None, remind_at=None, recurrence="none",
             category="personal", notes="", all_day=False) -> dict:
    kind = kind if kind in KINDS else "task"
    recurrence = recurrence if recurrence in RECURRENCES else "none"
    due = parse(due_at)
    if all_day and due:
        due = due.replace(hour=23, minute=59)  # all-day: not overdue until the day ends
    remind = parse(remind_at) or (None if all_day else _default_remind(due, kind))
    cur = conn.execute(
        "INSERT INTO items(kind,title,notes,category,due_at,remind_at,recurrence,all_day,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (kind, title, notes or "", category or "personal", fmt(due), fmt(remind), recurrence,
         int(bool(all_day and due)), fmt(now())),
    )
    conn.commit()
    return get_item(cur.lastrowid)


def get_item(item_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    return dict(row) if row else None


def update_item(item_id: int, **fields) -> dict | None:
    item = get_item(item_id)
    if not item:
        return None
    allowed = {"title", "kind", "notes", "category", "due_at", "remind_at", "recurrence"}
    changes = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "due_at" in changes:
        raw = str(changes["due_at"]).strip()
        due = parse(raw)
        kind = changes.get("kind", item["kind"])
        if len(raw) == 10 and kind == "event":
            due, default_remind, changes["all_day"] = due.replace(hour=23, minute=59), None, 1
        else:
            default_remind, changes["all_day"] = _default_remind(due, kind), 0
        changes["due_at"] = fmt(due)
        if "remind_at" not in changes:
            changes["remind_at"] = fmt(default_remind)
    if "remind_at" in changes:
        changes["remind_at"] = fmt(parse(changes["remind_at"]))
        changes["reminded"] = 0
    if not changes:
        return item
    cols = ", ".join(f"{k}=?" for k in changes)
    conn.execute(f"UPDATE items SET {cols} WHERE id=?", (*changes.values(), item_id))
    conn.commit()
    return get_item(item_id)


def roll_forward(item: dict) -> dict:
    """Move a recurring item to its next occurrence in the future."""
    due, remind = parse(item["due_at"]), parse(item["remind_at"])
    rec, cur = item["recurrence"], now()
    while True:
        due = db.shift(due, rec) if due else None
        remind = db.shift(remind, rec) if remind else None
        anchor = remind or due
        if anchor is None or anchor > cur:
            break
    conn.execute("UPDATE items SET due_at=?, remind_at=?, reminded=0 WHERE id=?",
                 (fmt(due), fmt(remind), item["id"]))
    conn.commit()
    return get_item(item["id"])


def complete_item(item_id: int) -> dict | None:
    item = get_item(item_id)
    if not item:
        return None
    if item["recurrence"] != "none" and item["due_at"]:
        return roll_forward(item)
    conn.execute("UPDATE items SET done=1, done_at=? WHERE id=?", (fmt(now()), item_id))
    conn.commit()
    return get_item(item_id)


def done_since(since: datetime) -> list[dict]:
    """Items completed at or after `since`, oldest first."""
    rows = conn.execute("SELECT * FROM items WHERE done=1 AND done_at >= ? ORDER BY done_at", (fmt(since),))
    return [dict(r) for r in rows]


def events_between(start: datetime, end: datetime) -> list[dict]:
    rows = conn.execute("SELECT * FROM items WHERE kind='event' AND due_at >= ? AND due_at < ? ORDER BY due_at",
                        (fmt(start), fmt(end)))
    return [dict(r) for r in rows]


_FILLER = set("""the a an and or of to for on in at with my our is was it this that be done did finished
completed complete finish wrapped up off ticket task item""".split())


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _FILLER and (len(w) > 2 or w.isdigit())}


def find_open(ref) -> list[dict]:
    """Items matching `ref`: an id ('#12', '12', 12) or words from an open item's title or notes
    ('the demo', 'go links', 'PRX-558'). One entry = a match; several = ambiguous, best first."""
    ref = str(ref).strip()
    m = re.fullmatch(r"#?(\d+)", ref)
    if m:
        item = get_item(int(m[1]))
        return [item] if item else []
    want = _words(ref)
    if not want:
        return []
    scored = []
    for r in conn.execute("SELECT * FROM items WHERE done=0"):
        title, notes = _words(r["title"]), _words(r["notes"] or "")
        score = len(want & title) + 0.5 * len(want & (notes - title))
        if ref.lower() in r["title"].lower():
            score += len(want)  # the whole phrase appears in the title
        if score >= max(1.0, len(want) / 2):
            scored.append((score, dict(r)))
    scored.sort(key=lambda s: -s[0])
    if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return [scored[0][1]]  # a clear winner
    return [item for score, item in scored if score == scored[0][0]]


def relative(ts: str | None) -> str:
    """'today', 'tomorrow', 'in 3 days', '2 days ago': so the model never has to work out past vs future."""
    if not ts:
        return ""
    days = (parse(ts).date() - now().date()).days
    return {0: "today", 1: "tomorrow", -1: "yesterday"}.get(
        days, f"in {days} days" if days > 0 else f"{-days} days ago")


def delete_item(item_id: int) -> bool:
    cur = conn.execute("DELETE FROM items WHERE id=?", (item_id,))
    conn.commit()
    return cur.rowcount > 0


def list_items(days_ahead: int = 7, category: str | None = None, include_done=False,
               exclude_category: str | None = None) -> list[dict]:
    """Open items that are overdue, due within `days_ahead`, or have no date."""
    end = fmt(now() + timedelta(days=days_ahead))
    # Past events are simply over; only tasks/deadlines/reminders can be overdue.
    sql = "SELECT * FROM items WHERE (due_at IS NULL OR due_at <= ?) AND NOT (kind='event' AND due_at < ?)"
    args: list = [end, fmt(now())]
    if not include_done:
        sql += " AND done=0"
    if category:
        sql += " AND category=?"
        args.append(category)
    if exclude_category:
        sql += " AND category<>?"
        args.append(exclude_category)
    sql += " ORDER BY due_at IS NULL, due_at"
    return [dict(r) for r in conn.execute(sql, args)]


def due_reminders() -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM items WHERE done=0 AND reminded=0 AND remind_at IS NOT NULL AND remind_at <= ?",
        (fmt(now()),),
    )
    return [dict(r) for r in rows]


def postpone_reminder(item: dict, until: datetime) -> None:
    conn.execute("UPDATE items SET remind_at=? WHERE id=?", (fmt(until), item["id"]))
    conn.commit()


def mark_reminded(item: dict) -> None:
    if item["recurrence"] != "none":
        roll_forward(item)
    else:
        conn.execute("UPDATE items SET reminded=1 WHERE id=?", (item["id"],))
        conn.commit()


def describe(item: dict) -> str:
    when = pretty(item["due_at"])
    if item.get("all_day") and item["due_at"]:
        when = when[:10] + " (all day)"
    if item["due_at"]:
        when += f" ({relative(item['due_at'])})"
    icon = CATEGORY_ICONS.get(item["category"], "")
    s = f"{icon} #{item['id']} [{item['kind']}/{item['category']}] {item['title']} — {when}"
    if item.get("gcal_id"):
        s += " 📆"
    # Show the ping time when it isn't simply the default lead before the due time.
    due = parse(item["due_at"])
    if item.get("remind_at") and not item.get("reminded") and fmt(_default_remind(due, item["kind"])) != item["remind_at"]:
        s += f" ⏰ {pretty(item['remind_at'])}"
    if item["recurrence"] != "none":
        s += f" (repeats {item['recurrence']})"
    if item["done"]:
        s += " ✅" + (f" done {pretty(item['done_at'])[:10]} ({relative(item['done_at'])})" if item.get("done_at") else "")
    if item["notes"]:
        s += f" — {item['notes']}"
    return s


# ---------- Google Calendar mirror ----------
def upsert_gcal(ev: dict) -> None:
    """Insert/update a synced event. ev: gcal_id, title, due_at, all_day, notes, category|None.
    Keeps local-only state (done, custom remind_at) unless the event time changed. Caller commits."""
    row = conn.execute("SELECT * FROM items WHERE gcal_id=?", (ev["gcal_id"],)).fetchone()
    due = parse(ev["due_at"])
    remind = None if ev["all_day"] else _default_remind(due, "event")
    past = int(due < now())
    if row is None:
        conn.execute(
            "INSERT INTO items(kind,title,notes,category,due_at,remind_at,all_day,reminded,gcal_id,created_at)"
            " VALUES('event',?,?,?,?,?,?,?,?,?)",
            (ev["title"], ev["notes"], ev["category"] or "personal", fmt(due), fmt(remind),
             int(ev["all_day"]), past, ev["gcal_id"], fmt(now())),
        )
        return
    # Google owns title/notes/recurrence for synced events (each occurrence is its own row).
    conn.execute("UPDATE items SET title=?, notes=?, category=COALESCE(?,category), recurrence='none'"
                 " WHERE id=?", (ev["title"], ev["notes"], ev["category"], row["id"]))
    if row["due_at"] != fmt(due) or row["all_day"] != int(ev["all_day"]):
        conn.execute("UPDATE items SET due_at=?, remind_at=?, all_day=?, reminded=? WHERE id=?",
                     (fmt(due), fmt(remind), int(ev["all_day"]), past, row["id"]))


def prune_gcal(seen: set[str], start: str, end: str) -> int:
    """Delete synced items in [start, end] that no longer exist in Google Calendar."""
    rows = conn.execute("SELECT id, gcal_id FROM items WHERE gcal_id IS NOT NULL AND due_at BETWEEN ? AND ?",
                        (start, end)).fetchall()
    gone = [(r["id"],) for r in rows if r["gcal_id"] not in seen]
    conn.executemany("DELETE FROM items WHERE id=?", gone)
    conn.commit()
    return len(gone)


def unsynced_events() -> list[dict]:
    """Open, dated, upcoming local events that never made it to Google Calendar."""
    rows = conn.execute(
        "SELECT * FROM items WHERE kind='event' AND gcal_id IS NULL AND done=0 AND due_at >= ?",
        (fmt(now()),),
    )
    return [dict(r) for r in rows]


def set_gcal_id(item_id: int, event_id: str) -> None:
    conn.execute("UPDATE items SET gcal_id=? WHERE id=?", (event_id, item_id))
    conn.commit()


def find_gcal(event_id: str) -> dict | None:
    """Find a synced item by event id, or the first instance of a recurring event."""
    row = conn.execute(
        "SELECT * FROM items WHERE gcal_id=? OR substr(gcal_id,1,?)=? ORDER BY due_at LIMIT 1",
        (event_id, len(event_id) + 1, event_id + "_"),
    ).fetchone()
    return dict(row) if row else None
