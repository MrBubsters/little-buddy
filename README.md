# little-buddy

**A private personal assistant that lives in your Discord DMs and runs on your own computer.**

Tell it about your life in plain language ("dentist next Tuesday at 3", "report due Friday 5pm",
"my sister's birthday is March 4") and it keeps track, reminds you, sends you a morning agenda,
checks in on your workday, and syncs with Google Calendar.

The AI model runs **locally** through [Ollama](https://ollama.com). No OpenAI/Anthropic account,
no API keys, no per-message costs, and your conversations never leave your machine (apart from
Discord itself delivering the messages, and Google Calendar if you enable it).

---

## Features
- 💬 **Natural-language chat:** add, change, complete and delete events, deadlines, tasks and reminders. Understands "next Friday", "every weekday at 7", etc.
- 🧠 **Remembers you:** stores lasting facts (people, routines, preferences) and uses them as context.
- ⏰ **Reminders:** @mentions you before things happen. Supports daily/weekday/weekly/monthly/yearly repeats.
- ☀️ **Morning digest:** today's agenda, overdue items and the next few days.
- 📝 **Daily work check-in:** asks what you got done and keeps asking until you answer. Ask "what did I do this week?" later.
- 💼 **Work/life separation:** items are tagged work 💼, social 🎉 or personal 🏠; work stays out of your weekends.
- 📆 **Google Calendar sync (optional):** two-way, with your own free Google API credentials.
- 🖥️ **Runs anywhere:** Windows, Linux (incl. headless servers as a service), macOS. GPU recommended, CPU works.

## How it works
```
 You (Discord DM) ──► bot.py ──► agent.py ──► Ollama (local AI model on your GPU)
                         │            │  the model calls "tools" to save/update/list things
                         │            ▼
                         ├──────► db.py ── SQLite database (data/jarofass.db)
                         └──────► gcal.py ◄──► Google Calendar (optional, every 5 min)

 Scheduler (every 30 s): reminders, morning digest, check-ins. Plain code, no AI needed.
```

---

## Quick start
You'll need about 30 minutes for the first setup.

| Step | Guide |
|---|---|
| 1. Create your Discord bot and get its token + your user ID | [docs/discord-setup.md](docs/discord-setup.md) |
| 2. Install Ollama, the code and its dependencies, then configure `.env` | [Windows](docs/install-windows.md) · [Linux / macOS](docs/install-linux.md) |
| 3. *(Optional)* Connect Google Calendar | [docs/google-calendar-setup.md](docs/google-calendar-setup.md) |
| 4. Start the bot and DM it `!help` | in the install guides |

Already running it and switching computers? See [docs/moving-machines.md](docs/moving-machines.md).

### Requirements
- Python **3.10+**
- [Ollama](https://ollama.com)
- A Discord account
- Disk: ~3–10 GB for the model
- Recommended: NVIDIA GPU with 6 GB+ VRAM, or an Apple Silicon Mac

## Choosing a model
The model must support **tool calling**. Pick by your GPU's memory (VRAM), set `OLLAMA_MODEL` in
`.env`, and `ollama pull` it.

| Your hardware | `OLLAMA_MODEL` | `OLLAMA_NUM_CTX` | Notes |
|---|---|---|---|
| No GPU (CPU only, 16 GB+ RAM) | `qwen3:4b` | `4096` | Works; replies take 10–60 s |
| 6 GB GPU | `qwen3:4b` | `6144` | Fast, decent accuracy |
| **8 GB GPU** | **`qwen3:8b`** (default) | `6144` | Good balance; uses ~6.5 GB |
| 12 GB+ GPU | `qwen3:8b` | `8192` | Or try `qwen3:14b` for better accuracy |
| 16 GB+ GPU / Apple Silicon 16 GB+ | `qwen3:14b` | `8192` | Best accuracy of these |

**Check it fits:** while the bot is replying, run `ollama ps`. The *PROCESSOR* column should say
**100% GPU**. A CPU/GPU split means it's too big: lower `OLLAMA_NUM_CTX` or pick a smaller model.

Reminders, the digest and check-ins don't use the model, so they're instant regardless.

---

## Using it
Just DM the bot like you'd text a person:

> dentist next tuesday at 3pm
> project report due friday 5pm, remind me thursday evening
> gym every weekday at 7am
> remember my manager's name is Priya
> move the dentist to wednesday
> what's on this week?
> what did I get done at work this week?

The bot confirms what it saved, e.g. `🏠 #12 [event/personal] Dentist — Tue Nov 10 15:00`. The
`#12` is the item's ID, so you can say "delete #12" or "mark #12 done".

### Commands
These answer instantly without using the AI:

| Command | What it does |
|---|---|
| `!agenda` | Overdue items + the next 3 days (`!agenda all` includes work on days off) |
| `!week` | Same for 7 days (`!week all`) |
| `!facts` | What the bot remembers about you |
| `!update <text>` | Log today's work update |
| `!skip` | Skip today's check-ins (vacation, sick day) |
| `!worklog` | Your work updates from the last 7 days |
| `!sync` | Sync Google Calendar right now |
| `!reset` | Clear chat history (your items and facts are kept) |
| `!help` | Show help |

### Scheduled messages
All of these @mention you so Discord sends a notification.

| Message | When | Settings |
|---|---|---|
| ☀️ Morning digest | Daily | `DIGEST_TIME` |
| ⏰ Reminder | Before each item (default 30 min) | `REMIND_LEAD_MINUTES`, or say "remind me at …" |
| 📝 Work check-in | Work days, re-asks until answered | `WORK_CHECKIN_TIME`, `_UNTIL`, `_NAG_MINUTES`, `_DAYS` |

Answer a check-in by **replying** to it in Discord, with `!update …`, or just by telling the bot.

**Days off:** outside `WORK_DAYS`, work items are hidden from the digest and agenda and work
reminders wait until the next work morning.

### Categories
| Icon | Category | Examples |
|---|---|---|
| 💼 | work | meetings, projects, deadlines, coworkers |
| 🎉 | social | friends, family, parties, dinners |
| 🏠 | personal | errands, health, bills, chores |

📆 marks items synced with Google Calendar.

### Voice channels
Join a voice channel in a server the bot is in, then DM it `!join`, and just talk. It replies to
everything said in the channel, like a friend on a call. It posts each transcript and reply in the DM
and speaks the reply. It remembers what was said earlier in the call, so follow-ups like "and tomorrow?" work.

It can still save and look up items ("remind me to stretch at five"). Friends in the call can talk to
it too: the model is told who's speaking, and friends can't change your saved facts or check-ins.
`!leave` disconnects, and the bot also leaves on its own once every human has left.

Prefer it to only answer when addressed? Set `WAKE_WORD_REQUIRED=true` and start sentences with its
name ("Buddy, what's on today?").

- Speech-to-text is [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (GPU, ~1 GB VRAM next to the LLM; CPU fallback).
  Text-to-speech is [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M) on the CPU (about 4× faster than real time).
  Both run locally, and their models download on the first `!join`. Pick a voice with `TTS_VOICE` (default `af_heart`).
- In voice, the bot talks instead of writing. It gives the gist instead of reading out lists, says times
  like "tomorrow at 3", says "one sec" when the model is slow, and stops talking if you say its name again.
- With wake words on, Whisper may spell an unusual name differently. Add the spellings it uses (shown in the
  console as `[voice] Name: ...`) to `WAKE_WORDS`, e.g. `WAKE_WORDS=buddy,body,bud`.
- Bots can only join **server** voice channels, not DM or group calls. The bot needs *Connect* and *Speak* permissions there.
- **Let people in the call know the bot is listening.** Audio is never saved. Everything said is
  transcribed, printed to the bot's console, sent to the local model, and kept in its chat history.
- Use headphones. If your mic picks up the bot's voice from speakers, it can end up replying to itself.
- Discord encrypts all voice end-to-end (DAVE). `voice.py` adds DAVE decryption to the
  `discord-ext-voice-recv` alpha library, so keep that package at the pinned version.

---

## Configuration
All settings live in `.env`. Copy `.env.example`; every option is explained there. The essentials:

| Setting | Required | Example |
|---|---|---|
| `DISCORD_TOKEN` | ✅ | from [Discord setup](docs/discord-setup.md) |
| `OWNER_ID` | ✅ | your Discord user ID |
| `TIMEZONE` | recommended | `America/Chicago` ([list](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)) |
| `BOT_NAME` | | `Buddy` |
| `OLLAMA_MODEL` | | see [Choosing a model](#choosing-a-model) |
| `GCAL_ENABLED` | | `true` after [Google setup](docs/google-calendar-setup.md) |

Restart the bot after editing `.env`.

### Customizing
- **More check-ins** (evening journal, Friday review…): add an entry to `CHECKINS` in `checkins.py`.
- **Personality / rules:** edit the system prompt in `_system_prompt()` in `agent.py`.
- **Ollama on another machine:** set `OLLAMA_HOST=http://that-machine:11434` (and on that machine,
  set the environment variable `OLLAMA_HOST=0.0.0.0` so Ollama accepts network connections).

## Project layout
| File | Purpose |
|---|---|
| `bot.py` | Discord connection, commands, scheduler (reminders/digest/check-ins) |
| `agent.py` | Talks to Ollama; defines the tools the AI can use; builds the prompt |
| `db.py` | SQLite storage, dates, repeats |
| `gcal.py` | Google Calendar sync; run directly once to authorize |
| `checkins.py` | Scheduled check-in questions |
| `config.py` | Reads `.env` |
| `start.bat` / `start.sh` | Launchers (create the venv on first run) |
| `deploy/little-buddy.service` | systemd service for Linux |
| `data/` | Your database and Google credentials. **Private, git-ignored** |

---

## Troubleshooting
| Symptom | Fix |
|---|---|
| `No time zone found with key …` | You ran the system Python. Use `.venv\Scripts\python` (Windows) or `.venv/bin/python` (Linux/macOS) |
| `ModuleNotFoundError` | Same: use the venv's Python, or rerun `pip install -r requirements.txt` in it |
| Bot is online but never replies | Enable **Message Content Intent**; check `OWNER_ID` is *your* ID ([Discord setup](docs/discord-setup.md#test-it)) |
| "Couldn't reach the local model" | Ollama isn't running. Windows: start the Ollama app. Linux: `sudo systemctl start ollama` |
| `model "…" not found` | `ollama pull <OLLAMA_MODEL>` |
| Replies are very slow | Model doesn't fit your GPU. See [Choosing a model](#choosing-a-model) |
| Wrong dates or times | Set `TIMEZONE` correctly; for repeated mistakes try a larger model |
| Bot saved something wrong | Tell it ("that should be 4pm, not 3") or "delete #ID" |
| Double reminders | Two copies of the bot are running (e.g. old + new computer). Stop one |
| `database is locked` | Database is in a cloud-synced folder. Set `DB_PATH` to a local path |
| Google problems | See [Google troubleshooting](docs/google-calendar-setup.md#troubleshooting) |
| New setting has no effect | Restart the bot; check the line exists in `.env` (compare with `.env.example` after updating) |

## Privacy
- The AI runs on your machine; conversations aren't sent to any AI company.
- Messages pass through **Discord** (it's the chat app), and calendar events through **Google** if enabled.
- Everything the bot knows is in `data/jarofass.db` on your computer. Delete it to start fresh.
- Keep `.env` and `data/` private. They're git-ignored by default.

## Limitations
- One reminder time per item.
- One Google calendar synced; editing a recurring Google event through the bot changes only that occurrence.
- Small local models occasionally misread dates. The bot always confirms what it saved, so glance at its reply.
