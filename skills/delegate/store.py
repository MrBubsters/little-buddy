"""Tables for delegated work: a *task* is the durable objective, a *run* is one Claude Code attempt at it.

    task: queued -> running -> awaiting_review (a PR exists) | completed | failed | cancelled -> closed
    run:  queued -> running -> completed | failed | cancelled
"""
import re
from datetime import datetime

import config
from core import db

TASK_ACTIVE = ("queued", "running")
TASK_OPEN = ("queued", "running", "awaiting_review", "completed", "failed")  # anything not closed/cancelled

db.ensure_schema("""
CREATE TABLE IF NOT EXISTS agent_tasks(
    id INTEGER PRIMARY KEY,
    objective TEXT NOT NULL,
    repository TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    origin TEXT NOT NULL DEFAULT 'discord',
    branch TEXT,
    worktree TEXT,
    session_id TEXT,
    pr_url TEXT,
    summary TEXT,
    cost_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_runs(
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    session_id TEXT,
    cost_usd REAL,
    turns INTEGER,
    tools INTEGER,
    exit_code INTEGER,
    result TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_task ON agent_runs(task_id, id);
""")

_PR = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")


def ts() -> str:
    return datetime.now(config.TZ).replace(tzinfo=None).isoformat(timespec="seconds")


def _row(cur) -> dict | None:
    r = cur.fetchone()
    return dict(r) if r else None


# ---------- tasks ----------
def create_task(objective: str, repository: str, model: str, origin: str = "discord") -> dict:
    now = ts()
    cur = db.conn.execute(
        "INSERT INTO agent_tasks(objective,repository,model,origin,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        (objective.strip(), repository, model, origin, now, now))
    task_id = cur.lastrowid
    db.conn.execute("INSERT INTO agent_runs(task_id,prompt,created_at) VALUES(?,?,?)", (task_id, objective.strip(), now))
    db.conn.commit()
    return get_task(task_id)


def get_task(task_id: int) -> dict | None:
    return _row(db.conn.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)))


def update_task(task_id: int, **fields) -> None:
    fields["updated_at"] = ts()
    sets = ", ".join(f"{k}=?" for k in fields)
    db.conn.execute(f"UPDATE agent_tasks SET {sets} WHERE id=?", (*fields.values(), task_id))
    db.conn.commit()


def list_tasks(statuses: tuple[str, ...] | None = None, limit: int = 10) -> list[dict]:
    if statuses:
        marks = ",".join("?" * len(statuses))
        rows = db.conn.execute(f"SELECT * FROM agent_tasks WHERE status IN ({marks}) ORDER BY id DESC LIMIT ?",
                               (*statuses, limit))
    else:
        rows = db.conn.execute("SELECT * FROM agent_tasks ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


# ---------- runs ----------
def add_run(task_id: int, prompt: str) -> dict:
    cur = db.conn.execute("INSERT INTO agent_runs(task_id,prompt,created_at) VALUES(?,?,?)",
                          (task_id, prompt.strip(), ts()))
    db.conn.commit()
    return get_run(cur.lastrowid)


def get_run(run_id: int) -> dict | None:
    return _row(db.conn.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)))


def update_run(run_id: int, **fields) -> None:
    sets = ", ".join(f"{k}=?" for k in fields)
    db.conn.execute(f"UPDATE agent_runs SET {sets} WHERE id=?", (*fields.values(), run_id))
    db.conn.commit()


def task_runs(task_id: int) -> list[dict]:
    return [dict(r) for r in db.conn.execute("SELECT * FROM agent_runs WHERE task_id=? ORDER BY id", (task_id,))]


def next_queued_run() -> dict | None:
    """Oldest queued run whose task is still live."""
    return _row(db.conn.execute(
        "SELECT r.* FROM agent_runs r JOIN agent_tasks t ON t.id=r.task_id "
        "WHERE r.status='queued' AND t.status IN ('queued','running') ORDER BY r.id LIMIT 1"))


def running_runs() -> list[dict]:
    return [dict(r) for r in db.conn.execute("SELECT * FROM agent_runs WHERE status='running' ORDER BY id")]


def month_spend() -> float:
    month = ts()[:7]
    row = db.conn.execute("SELECT COALESCE(SUM(cost_usd),0) AS c FROM agent_runs WHERE finished_at LIKE ?",
                          (month + "%",)).fetchone()
    return float(row["c"] or 0)


def pr_url(text: str | None) -> str | None:
    m = _PR.search(text or "")
    return m.group(0) if m else None


# ---------- display ----------
ICONS = {"queued": "⏳", "running": "🔧", "awaiting_review": "👀", "completed": "✅", "failed": "❌",
         "cancelled": "🛑", "closed": "📦"}


def _age(ts_str: str | None) -> str:
    if not ts_str:
        return ""
    secs = (datetime.fromisoformat(ts()) - datetime.fromisoformat(ts_str)).total_seconds()
    return f"{int(secs // 60)}m" if secs < 36000 else f"{int(secs // 3600)}h"


def describe(t: dict, live: dict | None = None) -> str:
    """One line: 🔧 task #3 minecraft-ai (sonnet) running 4m · 12 tools — Fix pathfinding..."""
    icon = ICONS.get(t["status"], "•")
    bits = [t["status"].replace("_", " ")]
    if t["status"] == "running" and live:
        bits.append(f"{_age(live.get('started_at'))} · {live.get('tools', 0)} tools")
    if t["pr_url"]:
        bits.append(t["pr_url"])
    if t["cost_usd"]:
        bits.append(f"${t['cost_usd']:.2f}")
    return f"{icon} task #{t['id']} {t['repository']} ({t['model']}) {' · '.join(bits)} — {t['objective'][:70]}"
