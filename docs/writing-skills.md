# Writing skills

Everything the assistant can do comes from a **skill**: a Python file or package in `skills/`
(or in a folder listed in `SKILL_PATHS`). Drop a file in, restart, and the model can use it.
`!skills` lists what's loaded, and `!status` shows how much of the context window the tools take.

## A minimal skill

```python
# skills/plants.py
from typing import Annotated, Literal

from core import db
from core.tools import tool

GUIDE = "Plants: when {owner} says they watered a plant, call log_watering."

db.ensure_schema("CREATE TABLE IF NOT EXISTS waterings(plant TEXT, at TEXT);")


@tool(writes=True)
def log_watering(plant: Annotated[str, "which plant"], amount: Literal["sip", "soak"] = "sip") -> str:
    """Record that a plant was watered."""
    db.conn.execute("INSERT INTO waterings VALUES(?,?)", (plant, db.fmt(db.now())))
    db.conn.commit()
    return f"Logged: {plant} ({amount})"
```

The tool's JSON schema is generated from the signature: type hints give types, `Literal` gives
choices, `Annotated[..., "text"]` adds a description, and parameters without defaults are required.
The first paragraph of the docstring is the description the model sees.

## What a skill can define

| Name | Purpose |
|---|---|
| `@tool` functions | Things the model can call. Options: `writes=True` (the result is shown to you as a ✔ receipt), `owner_only=True` (hidden from guests), `enabled=callable` (switch on or off at runtime). Add a `ctx` parameter to receive the `ToolContext`. |
| `GUIDE` | Prompt text on *when* to use the tools. Included only while the skill's tools are available. `{owner}` is filled in. |
| `context(ctx, query)` | Live information for the system prompt, such as today's agenda. Keep it short: it's sent with every message. |
| `@command("!name", "help")` | A chat command that answers instantly without the model. Return a string, or `Forward(text)` to pass text on to the model. |
| `preprocess(text, ctx, meta)` | Rewrite an incoming message first. `meta["reply_to"]` is the Discord message it replies to. |
| `on_start()` | Runs once after the front-end connects. |
| `on_tick()` | Runs every ~30 s, for reminders, polling and nags. |

Hooks can be plain functions or `async`. If a skill fails to import, it's skipped with a log line
and `!skills` shows the error, so a broken experiment doesn't take the bot down.

## Useful building blocks

- `core.db`: the shared SQLite connection, `ensure_schema`/`add_column` for your tables,
  `now`/`parse`/`normalize` for times (`normalize("next friday 5pm")` gives a `"YYYY-MM-DD 17:00"` string), and `get_meta`/`set_meta`.
- `core.app.app.notify(text)`: message the owner unprompted.
- `core.memory`: `add`, `search`, `entries` over saved facts and notes.
- `core.scheduler.add(...)`: schedule a job, either a fixed message or an instruction the agent carries out later.
- `core.llm.chat(messages, ...)`: call the model yourself (summaries, classification). Pass
  `model=config.LLM_FAST_MODEL` for cheap side jobs.
- `core.style.register(mode, instructions, finish=...)`: a new reply style for a new front-end
  (e.g. spoken replies), with an optional async post-processor.

## Keep it light on small models

Every enabled tool's schema is sent with every message. On an 8 GB GPU with a 6k context, aim for
short descriptions and a handful of parameters. Prefer one flexible tool over three narrow ones.
Use `enabled=` so tools only appear when they can work, or turn whole skills off with
`SKILLS_DISABLED`. On bigger hardware, raise `LLM_CONTEXT` and the budget grows to fit.

## Testing

`tests/test_core.py` runs the engine with a scripted fake model; copy its `FakeLLM` pattern for
your skill. To try a skill against the real model without Discord:

```
set DB_PATH=%TEMP%\scratch.db
.venv\Scripts\python cli.py "I watered the fern"
```
