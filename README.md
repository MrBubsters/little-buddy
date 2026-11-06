# little-buddy

**A private, general-purpose AI assistant that lives in your Discord DMs and runs on your own computer.**

Talk to it like a capable friend: ask questions, think a problem through, get help writing, do quick
math, save notes and ideas, and let it keep track of your life. "dentist next Tuesday at 3", "my
sister's birthday is March 4", "every Friday at 5 ask me how the week went", "what's 18% of 64?".
It reminds you, sends a morning agenda, checks in on your workday, can follow up on things by
itself, and syncs with Google Calendar.

The AI model runs **locally** through [Ollama](https://ollama.com) (or any OpenAI-compatible
server). No account, no API keys, no per-message costs, and your conversations never leave your
machine (apart from Discord delivering the messages, and Google Calendar or web search if you turn them on).

It's built to grow: abilities are **skills** you can add as plain Python files, and the
assistant automatically uses more of whatever hardware you give it.

---

## Features
- 💬 **General conversation:** questions, explanations, brainstorming, writing and editing, honest opinions.
- 🧠 **Memory:** remembers lasting facts about you and keeps longer notes (recipes, ideas, meeting notes), with search. Only what's relevant to the current message goes into the prompt, so it scales.
- 🔁 **Follows up by itself:** "in an hour, check whether I finished the report", "every Monday at 9 ask what my top priority is". The assistant schedules its own jobs and carries them out later.
- 🧮 **Exact math:** a calculator tool for bills, budgets, conversions and percentages.
- 📅 **Planner:** events, deadlines, tasks and reminders in plain language ("next Friday", "every weekday at 7"), repeats, a morning digest, and work/life separation (work 💼 stays out of your weekends).
- 📝 **Daily work check-in:** asks what you got done and keeps asking until you answer.
- 📆 **Google Calendar sync (optional):** two-way, with your own free Google API credentials.
- 🌐 **Web search (optional, off by default):** looks things up and reads pages.
- 🧩 **Skills:** drop a Python file in `skills/` to add tools, commands, live context or scheduled behaviour. See [docs/writing-skills.md](docs/writing-skills.md).
- 🖥️ **Runs anywhere:** Windows, Linux (incl. headless servers as a service), macOS. GPU recommended, CPU works.

## How it works
```
 You (Discord DM) ──► bot.py ─┐        (or cli.py in a terminal)
                              ▼
          core/agent.py  builds a prompt that fits the context window:
                         persona + skill context + relevant memories + recent chat,
                         then lets the model think and call tools, step by step
                              │
          core/llm.py   ──► Ollama / any OpenAI-compatible server
          core/tools.py ──► tools from skills/  (planner, memory, jobs, check-ins, web, ...)
          core/memory.py    facts + notes, full-text (and optional embedding) search
          core/scheduler.py every 30 s: skill hooks (reminders, digest, check-ins, calendar sync)
                            + jobs the assistant scheduled for itself
          core/db.py        SQLite database (data/jarofass.db)
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
`.env`, and `ollama pull` it. The context size (`OLLAMA_NUM_CTX`) matters as much as the model:
tool definitions, memories and chat history are all budgeted to fit it, so a bigger context means
more memory and longer conversations. `!status` shows where it goes.

| Your hardware | `OLLAMA_MODEL` | `OLLAMA_NUM_CTX` | Notes |
|---|---|---|---|
| No GPU (CPU only, 16 GB+ RAM) | `qwen3:4b` | `4096` | Works; replies take 10–60 s |
| 6 GB GPU | `qwen3:4b` | `6144` | Fast, decent accuracy |
| **8 GB GPU** | **`qwen3:8b`** (default) | `6144` | Good balance; uses ~6.5 GB. See [the 8 GB profile](#8-gb-gpu-eg-rtx-2070-super-3070-4060) |
| 12 GB GPU | `qwen3:14b` | `8192` | Noticeably better at multi-step requests than 8b. Or `qwen3:8b` with a 16k context |
| 16 GB+ GPU / Apple Silicon 16 GB+ | `qwen3:14b` | `8192` | Best accuracy of these |

**Check it fits:** while the bot is replying, run `ollama ps`. The *PROCESSOR* column should say
**100% GPU**. A CPU/GPU split means it's too big: lower `OLLAMA_NUM_CTX` or pick a smaller model.

Reminders, the digest and check-ins don't use the model, so they're instant regardless.

### Hardware profiles
On a local GPU, how many words per second the model writes depends mostly on the card's memory
speed, and what fits depends on its memory size. Before each reply the model also reads the whole
prompt (about 3–7k tokens here), which is slower on older cards. The prompt is laid out so the
unchanged part (instructions, tool definitions, earlier chat) is reused from Ollama's cache; only
the newest message and its context block are read fresh.

#### 8 GB GPU (e.g. RTX 2070 Super, 3070, 4060)
```
OLLAMA_MODEL=qwen3:8b
OLLAMA_NUM_CTX=6144
SKILLS_DISABLED=web          # unless you use web search; every skill's tools take up context
LLM_CONCURRENCY=1
```
Also turn on two **Ollama server** settings. They're environment variables for Ollama itself, not
lines in `.env`. Together they roughly halve the memory the context uses:

| | Windows (PowerShell, then quit and restart the Ollama app from the tray) | Linux (`sudo systemctl edit ollama`, then restart it) |
|---|---|---|
| Flash attention | `setx OLLAMA_FLASH_ATTENTION 1` | `Environment="OLLAMA_FLASH_ATTENTION=1"` |
| Smaller context cache | `setx OLLAMA_KV_CACHE_TYPE q8_0` | `Environment="OLLAMA_KV_CACHE_TYPE=q8_0"` |

After that, try `OLLAMA_NUM_CTX=10240` and check `ollama ps` still says **100% GPU** while replying.
Don't count on 14b models: they don't fit in 8 GB and slow down a lot when they spill to the CPU.

#### 12 GB GPU (e.g. RTX 4070, 3060 12 GB)
`OLLAMA_MODEL=qwen3:14b` with `OLLAMA_NUM_CTX=8192`, plus the same two Ollama settings. If you
develop here but deploy to an 8 GB machine, test with the 8 GB settings so what you test is what you ship.

#### Measuring
`tools/bench.py` replays a fixed conversation (on a throwaway copy of your database) and reports the
prompt-reading, writing and total time per message. Run it on the machine you deploy to whenever
you change the model, the context size or the skills:
```
.venv\Scripts\python tools\bench.py --runs 3
```

### Scaling up
Nothing is tied to one model or one machine:

| To get... | Set |
|---|---|
| A smarter model | `OLLAMA_MODEL` (bigger model) and `OLLAMA_NUM_CTX` (more memory and history per message) |
| A model on another computer or server | `OLLAMA_HOST=http://that-machine:11434` |
| llama.cpp, LM Studio, vLLM or a cloud API instead of Ollama | `LLM_PROVIDER=openai`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` |
| Faster side jobs | `LLM_FAST_MODEL` (a small model for rewrites and summaries) |
| Search memory by meaning, not just keywords | `EMBED_MODEL=nomic-embed-text` (then `ollama pull nomic-embed-text`) |
| Parallel requests on a server that can batch | `LLM_CONCURRENCY` |
| Longer multi-step tasks | `LLM_MAX_STEPS` |

---

## Using it
Just DM the bot like you'd text a person:

> how do I explain recursion to a 10 year old?
> we're splitting a $187.40 bill 3 ways plus 20% tip, what do we each owe?
> every friday at 5pm ask me how my week went
> in 2 hours check whether I finished the demo slides
> save a note: pasta sauce recipe, ...
> dentist next tuesday at 3pm
> project report due friday 5pm, remind me thursday evening
> gym every weekday at 7am
> remember my manager's name is Priya
> move the dentist to wednesday
> what's on this week?
> what did I get done at work this week?

The bot confirms what it saved, e.g. `🏠 #12 [event/personal] Dentist — Tue Nov 10 15:00 (in 4 days)`. The
`#12` is the item's ID, so you can say "delete #12" or "mark #12 done", but you don't have to: "I finished
the dentist forms" or "the demo went well" finds the matching item. Ongoing work (projects, tickets) is kept
as tasks with their status, so "what's left?" and "what did I get done this week?" stay accurate.

### Commands
These answer instantly without using the AI (`!help` lists them all, including ones skills add):

| Command | What it does |
|---|---|
| `!agenda` | Overdue items + the next 3 days (`!agenda all` includes work on days off) |
| `!week` | Same for 7 days (`!week all`) |
| `!facts` | What the bot remembers about you (`!facts notes` for saved notes) |
| `!jobs` | Follow-ups and recurring jobs the assistant scheduled for itself |
| `!update <text>` | Log today's work update |
| `!skip` | Skip today's check-ins (vacation, sick day) |
| `!worklog` | Your work updates from the last 7 days |
| `!sync` | Sync Google Calendar right now |
| `!status` | Model, backend, loaded skills, and how the context window is used |
| `!skills` | Loaded skills and their tools |
| `!reset` | Clear chat history (your items, memories and jobs are kept) |
| `!help` | Show help |

### Scheduled messages
All of these @mention you so Discord sends a notification.

| Message | When | Settings |
|---|---|---|
| ☀️ Morning digest | Daily | `DIGEST_TIME` |
| ⏰ Reminder | Before each item (default 30 min) | `REMIND_LEAD_MINUTES`, or say "remind me at …" |
| ⚙️ Jobs | Whenever you asked ("every Friday at 5…", "in an hour…") | `!jobs` to list; ask it to cancel one |
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
| `OWNER_NAME` | | your first name, so it doesn't call you "the user" |
| `WEB_ENABLED` | | `true` to allow web search |
| `GCAL_ENABLED` | | `true` after [Google setup](docs/google-calendar-setup.md) |

Restart the bot after editing `.env`.

### Customizing
- **Personality / standing instructions:** copy [docs/persona.example.md](docs/persona.example.md) to `data/persona.md` and edit it. No code or restart needed.
- **New abilities:** add a skill. See [docs/writing-skills.md](docs/writing-skills.md). Turn skills off with `SKILLS_DISABLED`.
- **More check-ins** (evening journal, Friday review…): add an entry to `CHECKINS` in `skills/checkins.py`, or just ask the assistant ("every evening at 9 ask me what I'm grateful for").
- **Ollama on another machine:** set `OLLAMA_HOST=http://that-machine:11434` (and on that machine,
  set the environment variable `OLLAMA_HOST=0.0.0.0` so Ollama accepts network connections).

## Project layout
| Path | Purpose |
|---|---|
| `bot.py` | Discord front-end: routes messages to commands or the agent, delivers notifications |
| `cli.py` | Terminal front-end for trying things without Discord (`python cli.py "hello"`) |
| `core/agent.py` | The agent loop and prompt builder (persona, skill context, memory, history, budgets) |
| `core/llm.py` | Model backends (Ollama, OpenAI-compatible) |
| `core/tools.py` | Tool registry: turns Python functions into tools |
| `core/skills.py` | Skill loader and hooks |
| `core/memory.py` | Facts and notes with search |
| `core/scheduler.py` | 30-second heartbeat and self-scheduled jobs |
| `core/style.py` | Reply style per mode (text, scheduled, or ones a front-end registers) |
| `core/db.py` | SQLite connection, time parsing, chat history |
| `skills/` | Abilities: `planner/` (items, digest, Google Calendar), `checkins`, `memory`, `jobs`, `utilities`, `web`, `system` |
| `tests/` | Engine tests with a fake model: `python -m unittest discover tests` |
| `tools/bench.py` | Reply-speed benchmark for the machine it runs on |
| `config.py` | Reads `.env` |
| `start.bat` / `start.sh` | Launchers (create the venv on first run) |
| `deploy/little-buddy.service` | systemd service for Linux |
| `data/` | Your database, persona and Google credentials. **Private, git-ignored** |

---

## Troubleshooting
| Symptom | Fix |
|---|---|
| `No time zone found with key …` | You ran the system Python. Use `.venv\Scripts\python` (Windows) or `.venv/bin/python` (Linux/macOS) |
| `ModuleNotFoundError` | Same: use the venv's Python, or rerun `pip install -r requirements.txt` in it |
| Bot is online but never replies | Enable **Message Content Intent**; check `OWNER_ID` is *your* ID ([Discord setup](docs/discord-setup.md#test-it)) |
| "Couldn't reach the local model" | Ollama isn't running. Windows: start the Ollama app. Linux: `sudo systemctl start ollama` |
| `model "…" not found` | `ollama pull <OLLAMA_MODEL>` |
| Replies are very slow | Model doesn't fit your GPU. See [Choosing a model](#choosing-a-model) and [Hardware profiles](#hardware-profiles); measure with `tools/bench.py` |
| "context is tight" in the console | Too many tools for the context size: raise `OLLAMA_NUM_CTX`, or turn skills off with `SKILLS_DISABLED` |
| A skill doesn't show up | `!skills` lists load errors; set `SKILLS_DEBUG=1` for the full traceback |
| Wrong dates or times | Set `TIMEZONE` correctly; for repeated mistakes try a larger model |
| Bot saved something wrong | Tell it ("that should be 4pm, not 3") or "delete #ID" |
| Double reminders | Two copies of the bot are running (e.g. old + new computer). Stop one |
| `database is locked` | Database is in a cloud-synced folder. Set `DB_PATH` to a local path |
| Google problems | See [Google troubleshooting](docs/google-calendar-setup.md#troubleshooting) |
| New setting has no effect | Restart the bot; check the line exists in `.env` (compare with `.env.example` after updating) |

## Privacy
- The AI runs on your machine; conversations aren't sent to any AI company (unless you point `LLM_PROVIDER` at a cloud API).
- Web search is off by default. With `WEB_ENABLED=true`, search queries go to DuckDuckGo and pages are fetched from their sites.
- Messages pass through **Discord** (it's the chat app), and calendar events through **Google** if enabled.
- Everything the bot knows is in `data/jarofass.db` on your computer. Delete it to start fresh.
- Keep `.env` and `data/` private. They're git-ignored by default.

## Limitations
- One reminder time per item.
- Small local models sometimes misjudge when to use a tool. Bigger models (see [Scaling up](#scaling-up)) are noticeably better at multi-step requests.
- One Google calendar synced; editing a recurring Google event through the bot changes only that occurrence.
- Small local models occasionally misread dates. The bot always confirms what it saved, so glance at its reply.
