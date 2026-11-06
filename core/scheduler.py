"""The heartbeat: every TICK seconds it runs due jobs and every skill's `on_tick` hook.

Jobs are stored in the database, so they survive restarts. A job's action is either:
- "message": send the payload text as is, or
- "agent":   give the payload to the assistant as an instruction and send what it writes.
             This is how the assistant schedules its own future work ("check on me at 5",
             "every Friday ask how the week went", "in an hour see if the report is done").
             If it decides there's nothing worth saying it answers SKIP and nothing is sent.
"""
import asyncio
from datetime import datetime

import config
from core import db
from core.app import app

TICK = 30  # seconds
ACTIONS = ("message", "agent")

db.ensure_schema("""
CREATE TABLE IF NOT EXISTS jobs(
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'agent',
    payload TEXT NOT NULL,
    next_run TEXT NOT NULL,
    recurrence TEXT NOT NULL DEFAULT 'none',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_run TEXT,
    last_result TEXT
);
""")


def add(title: str, payload: str, at: datetime, action: str = "agent", recurrence: str = "none") -> dict:
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {ACTIONS}")
    if recurrence not in db.RECURRENCES:
        raise ValueError(f"recurrence must be one of {db.RECURRENCES}")
    cur = db.conn.execute(
        "INSERT INTO jobs(title,action,payload,next_run,recurrence,created_at) VALUES(?,?,?,?,?,?)",
        (title.strip(), action, payload.strip(), db.fmt(at), recurrence, db.fmt(db.now())))
    db.conn.commit()
    return get(cur.lastrowid)


def get(job_id: int) -> dict | None:
    row = db.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def pending() -> list[dict]:
    return [dict(r) for r in db.conn.execute("SELECT * FROM jobs WHERE enabled=1 ORDER BY next_run")]


def cancel(job_id: int) -> bool:
    cur = db.conn.execute("UPDATE jobs SET enabled=0 WHERE id=? AND enabled=1", (job_id,))
    db.conn.commit()
    return cur.rowcount > 0


def describe(j: dict) -> str:
    rep = "" if j["recurrence"] == "none" else f" (repeats {j['recurrence']})"
    return f"⚙️ job #{j['id']} {j['title']} — next {db.pretty(j['next_run'])}{rep} [{j['action']}]"


def _reschedule(j: dict) -> None:
    nxt, cur = db.parse(j["next_run"]), db.now()
    if j["recurrence"] == "none":
        db.conn.execute("UPDATE jobs SET enabled=0 WHERE id=?", (j["id"],))
    else:
        while nxt <= cur:  # skip runs missed while offline instead of firing them all at once
            nxt = db.shift(nxt, j["recurrence"])
        db.conn.execute("UPDATE jobs SET next_run=? WHERE id=?", (db.fmt(nxt), j["id"]))
    db.conn.commit()


async def run_job(j: dict) -> str:
    if j["action"] == "message":
        await app.notify(j["payload"])
        return "sent"
    from core import agent
    from core.tools import ToolContext
    late = db.now() - db.parse(j["next_run"])
    note = f" It was due {int(late.total_seconds() // 60)} minutes ago (the assistant was offline)." \
        if late.total_seconds() > 600 else ""
    instruction = (f"[Scheduled job #{j['id']} you set up earlier is due now: \"{j['payload']}\".{note} "
                   f"Carry it out, using tools if needed, then write the message to send {config.owner()}. "
                   f"If it turns out nothing is worth telling them, reply with exactly SKIP.]")
    reply = await agent.chat(instruction, ToolContext(mode="scheduled", conversation="main"))
    if reply.strip().upper().startswith("SKIP"):
        return "skipped"
    await app.notify(reply)
    return "sent"


async def tick() -> None:
    due = [dict(r) for r in db.conn.execute(
        "SELECT * FROM jobs WHERE enabled=1 AND next_run<=? ORDER BY next_run", (db.fmt(db.now()),))]
    for j in due:
        _reschedule(j)  # first, so a crash mid-job can't make it fire every tick
        try:
            result = await run_job(j)
        except Exception as e:
            result = f"error: {e!r}"
            print(f"[jobs] #{j['id']} failed: {e!r}")
        db.conn.execute("UPDATE jobs SET last_run=?, last_result=? WHERE id=?",
                        (db.fmt(db.now()), result[:500], j["id"]))
        db.conn.commit()


async def run_forever() -> None:
    from core import skills
    while True:
        try:
            await skills.run_hooks("on_tick")
            await tick()
        except Exception as e:
            print(f"[scheduler] {e!r}")
        await asyncio.sleep(TICK)
