"""Long-term memory: facts about the user and saved notes, searchable.

- kind "fact": short lasting details (people, preferences, routines). Shown to the model when they
  fit the context budget, otherwise the most relevant ones.
- kind "note": anything longer the user wants kept (a recipe, an idea, meeting notes).
  Retrieved by search when relevant.

Search is SQLite full-text (always on). With EMBED_MODEL set, it also ranks by meaning.
"""
import json
import math
import re

import config
from core import db, llm

db.ensure_schema("""
CREATE TABLE IF NOT EXISTS memories(
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'fact',
    content TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT,
    embedding TEXT
);
""")
try:
    db.ensure_schema("CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(content, tags);")
    FTS = True
except Exception:  # SQLite built without FTS5: fall back to LIKE matching
    FTS = False

# One-time move of the old `facts` table into memories, keeping ids so "#3" still means the same fact.
if db.get_meta("memories_migrated") is None:
    if "id" in db.columns("facts"):
        for r in db.conn.execute("SELECT id, fact, created_at FROM facts"):
            db.conn.execute("INSERT OR IGNORE INTO memories(id,kind,content,created_at) VALUES(?,?,?,?)",
                            (r["id"], "fact", r["fact"], r["created_at"]))
    db.conn.commit()
    db.set_meta("memories_migrated", "1")


def _reindex(mid: int) -> None:
    if not FTS:
        return
    db.conn.execute("DELETE FROM memories_fts WHERE rowid=?", (mid,))
    row = db.conn.execute("SELECT content, tags FROM memories WHERE id=?", (mid,)).fetchone()
    if row:
        db.conn.execute("INSERT INTO memories_fts(rowid, content, tags) VALUES(?,?,?)",
                        (mid, row["content"], row["tags"]))


if FTS and db.get_meta("memories_fts_built") is None:
    for r in db.conn.execute("SELECT id FROM memories").fetchall():
        _reindex(r["id"])
    db.conn.commit()
    db.set_meta("memories_fts_built", "1")


def get(mid: int) -> dict | None:
    row = db.conn.execute("SELECT id, kind, content, tags, created_at FROM memories WHERE id=?", (mid,)).fetchone()
    return dict(row) if row else None


def entries(kind: str | None = None) -> list[dict]:
    sql, args = "SELECT id, kind, content, tags, created_at FROM memories", []
    if kind:
        sql, args = sql + " WHERE kind=?", [kind]
    return [dict(r) for r in db.conn.execute(sql + " ORDER BY id", args)]


async def add(content: str, kind: str = "fact", tags: str = "") -> int:
    content = content.strip()
    # Don't store the same thing twice.
    dup = db.conn.execute("SELECT id FROM memories WHERE kind=? AND lower(content)=lower(?)",
                          (kind, content)).fetchone()
    if dup:
        return dup["id"]
    cur = db.conn.execute("INSERT INTO memories(kind,content,tags,created_at) VALUES(?,?,?,?)",
                          (kind, content, tags.strip(), db.fmt(db.now())))
    _reindex(cur.lastrowid)
    db.conn.commit()
    await _embed_missing()
    return cur.lastrowid


async def update(mid: int, content: str) -> bool:
    cur = db.conn.execute("UPDATE memories SET content=?, updated_at=?, embedding=NULL WHERE id=?",
                          (content.strip(), db.fmt(db.now()), mid))
    _reindex(mid)
    db.conn.commit()
    await _embed_missing()
    return cur.rowcount > 0


def delete(mid: int) -> bool:
    cur = db.conn.execute("DELETE FROM memories WHERE id=?", (mid,))
    _reindex(mid)
    db.conn.commit()
    return cur.rowcount > 0


# ---------- search ----------
_STOP = set("""a an and are as at be but by can do does for from had has have how i if in into is it its
just me my no not of on or our so that the their them then there these they this to too was we were what
when where which who why will with you your about would could should please tell know remember""".split())


def _terms(text: str) -> list[str]:
    return [t for t in re.findall(r"\w+", text.lower()) if len(t) > 2 and t not in _STOP][:12]


def _keyword(query: str, limit: int, kind: str | None) -> list[int]:
    terms = _terms(query)
    if not terms:
        return []
    k = " AND m.kind=?" if kind else ""
    args = [kind] if kind else []
    if FTS:
        q = " OR ".join(f'"{t}"*' for t in terms)
        sql = (f"SELECT m.id FROM memories_fts f JOIN memories m ON m.id=f.rowid "
               f"WHERE memories_fts MATCH ?{k} ORDER BY bm25(memories_fts) LIMIT ?")
        return [r["id"] for r in db.conn.execute(sql, [q, *args, limit])]
    like = " OR ".join("lower(m.content) LIKE ?" for _ in terms)
    sql = f"SELECT m.id FROM memories m WHERE ({like}){k} LIMIT ?"
    return [r["id"] for r in db.conn.execute(sql, [*(f"%{t}%" for t in terms), *args, limit])]


async def _embed_missing() -> None:
    if not config.EMBED_MODEL:
        return
    rows = db.conn.execute("SELECT id, content FROM memories WHERE embedding IS NULL LIMIT 64").fetchall()
    if not rows:
        return
    try:
        vecs = await llm.embed([r["content"] for r in rows])
    except Exception as e:
        print(f"[memory] embedding failed: {e!r}")
        return
    for r, v in zip(rows, vecs or []):
        db.conn.execute("UPDATE memories SET embedding=? WHERE id=?", (json.dumps(v), r["id"]))
    db.conn.commit()


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


async def _semantic(query: str, limit: int, kind: str | None) -> list[int]:
    if not config.EMBED_MODEL or not query.strip():
        return []
    await _embed_missing()
    try:
        qv = (await llm.embed([query]))[0]
    except Exception as e:
        print(f"[memory] embedding failed: {e!r}")
        return []
    sql, args = "SELECT id, embedding FROM memories WHERE embedding IS NOT NULL", []
    if kind:
        sql, args = sql + " AND kind=?", [kind]
    scored = [(_cos(qv, json.loads(r["embedding"])), r["id"]) for r in db.conn.execute(sql, args)]
    scored.sort(reverse=True)
    return [mid for s, mid in scored[:limit] if s > 0.35]


async def search(query: str, limit: int = 5, kind: str | None = None) -> list[dict]:
    """Best matches for `query`, merging keyword and (if enabled) meaning-based ranking."""
    scores: dict[int, float] = {}
    for ranking in (_keyword(query, limit * 2, kind), await _semantic(query, limit * 2, kind)):
        for rank, mid in enumerate(ranking):
            scores[mid] = scores.get(mid, 0) + 1 / (rank + 2)  # reciprocal-rank fusion
    best = sorted(scores, key=scores.get, reverse=True)[:limit]
    return [m for m in (get(i) for i in best) if m]


async def for_prompt(query: str, budget_tokens: int) -> tuple[list[dict], list[dict], bool]:
    """(facts, notes, complete) to show the model for this message, within a token budget.
    complete=False means some facts were left out (the model should search for more)."""
    notes, used = [], 0
    notes_budget = budget_tokens // 3
    for n in await search(query, 3, "note"):
        room = notes_budget - used
        if room < 40:
            break
        if llm.tokens(n["content"]) + 4 > room:
            n = dict(n, content=n["content"][: room * 3 - 40] + "… (cut off; recall shows all of it)")
        notes.append(n)
        used += llm.tokens(n["content"]) + 4

    facts = entries("fact")
    room = budget_tokens - used  # facts get whatever the notes didn't need
    complete = sum(llm.tokens(f["content"]) + 4 for f in facts) <= room
    if not complete:  # too many to show: the relevant ones, then the newest
        relevant = await search(query, 8, "fact")
        ids = {f["id"] for f in relevant}
        facts, used = [], 0
        for f in relevant + [f for f in reversed(entries("fact")) if f["id"] not in ids]:
            if used + llm.tokens(f["content"]) + 4 > room:
                # Too long to show whole: a preview still tells the model it exists.
                f = dict(f, content=f["content"][:150].rstrip() + f"… (more: recall '#{f['id']}')")
            c = llm.tokens(f["content"]) + 4
            if used + c <= room:
                facts.append(f)
                used += c
        facts.sort(key=lambda f: f["id"])
    return facts, notes, complete
