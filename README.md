# Jarofass — local personal assistant on Discord

A private Discord bot backed by a **local LLM (Ollama)** that keeps context about your life
(facts, events, deadlines, tasks) in SQLite and pings you with reminders and a morning digest.
No API keys or token costs.

```
Discord DM ──► bot.py ──► agent.py ──► Ollama (qwen3:8b, local GPU)
                 │            │ tool calls
                 │            ▼
                 └──────► db.py (SQLite: items, facts, chat history)
   scheduler (30s): due reminders + daily digest — plain code, no LLM
```

## Hardware sizing (target: 8GB GPU)
| Model | VRAM (Q4, 6k ctx) | Notes |
|---|---|---|
| `qwen3:8b` (default) | ~6.5 GB | Best date/tool-call accuracy that fits 8GB |
| `qwen3:4b` | ~3.5 GB | Fallback if the 8B spills to CPU (check `ollama ps`) |

`ollama ps` should show **100% GPU**. If it shows a CPU/GPU split, lower `OLLAMA_NUM_CTX` to 4096
or switch to `qwen3:4b`. Reminders and the digest never touch the LLM, so they're instant either way.

## Setup
1. **Ollama**: install from https://ollama.com, then `ollama pull qwen3:8b`.
2. **Discord bot**: https://discord.com/developers/applications → New Application → *Bot*:
   - Reset Token → copy it.
   - Enable **Message Content Intent**.
   - *OAuth2 → URL Generator*: scope `bot`, permissions `Send Messages`, `Read Message History`.
     Open the URL and add it to a server you're in (needed so it can DM you).
3. **Config**: copy `.env.example` to `.env`, fill in `DISCORD_TOKEN`, `OWNER_ID`, `TIMEZONE`.
4. **Run**:
   ```
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   .venv\Scripts\python bot.py
   ```
   or just `start.bat`.

## Google Calendar sync (optional)
Two-way sync for **events**: Google → bot every 5 min (next 30 days, shown with 📆), and events you
create/edit/delete through the bot are written to Google. Tasks, deadlines and reminders stay local.

1. https://console.cloud.google.com → create a project (e.g. "Jarofass").
2. *APIs & Services → Library* → enable **Google Calendar API**.
3. *Google Auth Platform* (OAuth consent screen) → External → fill app name + your email →
   under *Audience* add yourself as a test user, then click **Publish app** (→ In production).
   Unpublished "Testing" apps get tokens that expire every 7 days. You'll see an "unverified app"
   warning when authorizing; that's expected for a personal app (Advanced → Go to Jarofass).
4. *Clients* → Create client → **Desktop app** → download JSON → save as `data/credentials.json`.
5. Run once: `.venv\Scripts\python gcal.py`, which opens the browser, writes `data/token.json` and lists upcoming events.
6. Set `GCAL_ENABLED=true` in `.env` and restart the bot. `!sync` forces a sync.

For the server PC, copy `data/credentials.json` and `data/token.json` along with the DB.
Limitations: one calendar (`GCAL_CALENDAR_ID`); editing/deleting a recurring event through the bot
affects only that occurrence. Change the whole series in Google Calendar.

## Usage
DM the bot naturally: "dentist next tue 3pm", "report due friday 5pm, remind me thursday 6pm",
"gym every weekday 7am", "remember my manager is Priya".

Commands (instant, no LLM): `!agenda` `!week` `!facts` `!digest` `!sync` `!reset` `!help`

### Scheduled messages (all @mention you)
- **Morning digest** at `DIGEST_TIME`.
- **Reminders** for items with a reminder time.
- **Work check-in** Mon–Fri at `WORK_CHECKIN_TIME` (14:30): "what did you get done?" Re-asks every
  `WORK_CHECKIN_NAG_MINUTES` until you answer, up to `WORK_CHECKIN_UNTIL`. Answer by replying to the
  message, `!update <text>`, or just telling the bot. `!skip` skips today (PTO/sick), `!worklog` shows
  the last 7 days, or ask "what did I get done this week?". More check-ins: add to `CHECKINS` in `checkins.py`.

## Deploying to the other PC
Copy the folder (without `.venv`), recreate the venv, copy `.env` and `data/jarofass.db`.
To auto-start, add a Task Scheduler task "At log on" running `start.bat`.
Note: keep `data/` out of OneDrive sync on the server, since sync can lock SQLite files (set `DB_PATH`).
