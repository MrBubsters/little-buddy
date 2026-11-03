"""Google Calendar two-way sync.

One-time setup: put your OAuth client file at data/credentials.json, then run
    .venv\\Scripts\\python gcal.py
to authorize in the browser. That writes data/token.json (copy it to the server PC too).
"""
import asyncio
import os
import threading
from datetime import datetime, timedelta, timezone

import config
import db

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
RRULES = {
    "daily": "RRULE:FREQ=DAILY",
    "weekdays": "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
    "weekly": "RRULE:FREQ=WEEKLY",
    "monthly": "RRULE:FREQ=MONTHLY",
    "yearly": "RRULE:FREQ=YEARLY",
}
CAT_KEY = "jarofass_category"

_api_lock = threading.Lock()  # httplib2 isn't thread-safe
_service = None
sync_lock = asyncio.Lock()


def enabled() -> bool:
    return config.GCAL_ENABLED and os.path.exists(config.GCAL_TOKEN)


def _creds(interactive: bool = False):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = None
    if os.path.exists(config.GCAL_TOKEN):
        creds = Credentials.from_authorized_user_file(config.GCAL_TOKEN, SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif interactive:
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow = InstalledAppFlow.from_client_secrets_file(config.GCAL_CREDENTIALS, SCOPES)
        creds = flow.run_local_server(port=0)
    else:
        raise RuntimeError("Google Calendar not authorized. Run: .venv\\Scripts\\python gcal.py")
    with open(config.GCAL_TOKEN, "w") as f:
        f.write(creds.to_json())
    return creds


def _svc():
    global _service
    if _service is None:
        from googleapiclient.discovery import build
        _service = build("calendar", "v3", credentials=_creds(), cache_discovery=False)
    return _service


def _events():
    return _svc().events()


# ---------- conversions ----------
def _normalize(e: dict) -> dict:
    start = e["start"]
    if "dateTime" in start:
        due, all_day = db.fmt(db.parse(start["dateTime"])), False
    else:
        due, all_day = start["date"] + "T23:59", True
    desc = (e.get("description") or "").replace("\n", " ").strip()[:200]
    notes = " | ".join(x for x in (e.get("location", ""), desc) if x)
    title = e.get("summary") or "(no title)"
    category = e.get("extendedProperties", {}).get("private", {}).get(CAT_KEY)
    if not category and any(k in title.lower() for k in config.GCAL_WORK_KEYWORDS):
        category = "work"
    return {
        "gcal_id": e["id"],
        "title": title,
        "due_at": due,
        "all_day": all_day,
        "notes": notes,
        "category": category,
    }


def _times(raw_due: str, minutes: int) -> dict:
    raw = raw_due.strip()
    if len(raw) == 10:  # date only -> all-day event
        d = datetime.fromisoformat(raw).date()
        return {"start": {"date": d.isoformat(), "dateTime": None, "timeZone": None},
                "end": {"date": (d + timedelta(days=1)).isoformat(), "dateTime": None, "timeZone": None}}
    s = db.parse(raw)
    e = s + timedelta(minutes=minutes)
    tz = config.TZ.key
    return {"start": {"dateTime": s.strftime("%Y-%m-%dT%H:%M:00"), "timeZone": tz, "date": None},
            "end": {"dateTime": e.strftime("%Y-%m-%dT%H:%M:00"), "timeZone": tz, "date": None}}


def _duration(ev: dict) -> int:
    try:
        s = datetime.fromisoformat(ev["start"]["dateTime"])
        e = datetime.fromisoformat(ev["end"]["dateTime"])
        return max(int((e - s).total_seconds() // 60), 1)
    except (KeyError, ValueError):
        return config.GCAL_EVENT_MINUTES


# ---------- blocking API calls (run via asyncio.to_thread) ----------
def fetch_events() -> list[dict]:
    now = datetime.now(timezone.utc)
    t_min = (now - timedelta(days=1)).isoformat()
    t_max = (now + timedelta(days=config.GCAL_SYNC_DAYS)).isoformat()
    out, page = [], None
    with _api_lock:
        while True:
            resp = _events().list(calendarId=config.GCAL_CALENDAR_ID, timeMin=t_min, timeMax=t_max,
                                  singleEvents=True, orderBy="startTime", maxResults=250,
                                  pageToken=page).execute()
            out += [_normalize(e) for e in resp.get("items", []) if e.get("status") != "cancelled"]
            page = resp.get("nextPageToken")
            if not page:
                return out


def create_event(title: str, due_at: str, recurrence: str = "none", category: str = "personal",
                 notes: str = "") -> str:
    body = {"summary": title, "description": notes or "",
            "extendedProperties": {"private": {CAT_KEY: category or "personal"}}}
    body.update(_times(due_at, config.GCAL_EVENT_MINUTES))
    for k in ("start", "end"):  # drop the null placeholders used for patching
        body[k] = {kk: v for kk, v in body[k].items() if v is not None}
    if recurrence in RRULES:
        body["recurrence"] = [RRULES[recurrence]]
    with _api_lock:
        ev = _events().insert(calendarId=config.GCAL_CALENDAR_ID, body=body).execute()
    return ev["id"]


def patch_event(event_id: str, title=None, due_at=None, notes=None, category=None) -> None:
    body = {}
    if title:
        body["summary"] = title
    if notes is not None:
        body["description"] = notes
    if category:
        body["extendedProperties"] = {"private": {CAT_KEY: category}}
    with _api_lock:
        if due_at:
            ev = _events().get(calendarId=config.GCAL_CALENDAR_ID, eventId=event_id).execute()
            body.update(_times(str(due_at), _duration(ev)))
        if body:
            _events().patch(calendarId=config.GCAL_CALENDAR_ID, eventId=event_id, body=body).execute()


def delete_event(event_id: str) -> None:
    from googleapiclient.errors import HttpError
    with _api_lock:
        try:
            _events().delete(calendarId=config.GCAL_CALENDAR_ID, eventId=event_id).execute()
        except HttpError as e:
            if e.resp.status not in (404, 410):  # already gone is fine
                raise


# ---------- async sync ----------
async def push_unsynced() -> int:
    """Upload local events created while sync was off (or when a push failed)."""
    items = db.unsynced_events()
    for it in items:
        twin = db.conn.execute(
            "SELECT id FROM items WHERE gcal_id IS NOT NULL AND lower(title)=lower(?) AND due_at=?",
            (it["title"], it["due_at"])).fetchone()
        if twin:  # already in Google (e.g. re-added while sync was off) — drop the duplicate
            db.delete_item(it["id"])
            print(f"[gcal] dropped duplicate local event #{it['id']} {it['title']}")
            continue
        due = it["due_at"][:10] if it["all_day"] else it["due_at"]
        eid = await asyncio.to_thread(create_event, it["title"], due, it["recurrence"],
                                      it["category"], it["notes"])
        if it["recurrence"] == "none":
            db.set_gcal_id(it["id"], eid)  # keeps local reminder/notes state
        else:
            db.delete_item(it["id"])  # Google expands the series; sync re-imports each occurrence
        print(f"[gcal] pushed local event #{it['id']} {it['title']}")
    return len(items)


async def sync() -> int:
    """Push stranded local events, then pull events from Google. Returns number of events seen."""
    async with sync_lock:
        await push_unsynced()
        events = await asyncio.to_thread(fetch_events)
        for ev in events:
            db.upsert_gcal(ev)
        db.conn.commit()
        n = db.now()
        db.prune_gcal({e["gcal_id"] for e in events},
                      db.fmt(n - timedelta(days=1)), db.fmt(n + timedelta(days=config.GCAL_SYNC_DAYS)))
        return len(events)


if __name__ == "__main__":
    if not os.path.exists(config.GCAL_CREDENTIALS):
        raise SystemExit(f"Missing {config.GCAL_CREDENTIALS} — download your OAuth client JSON there first.")
    _creds(interactive=True)
    print(f"Authorized. Token saved to {config.GCAL_TOKEN}")
    for ev in fetch_events()[:10]:
        print(" ", ev["due_at"], "-", ev["title"])
