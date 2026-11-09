"""All settings, read from `.env`. Every option is documented in `.env.example`."""
import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _list(name: str, default: str = "") -> list[str]:
    return [x.strip().lower() for x in os.getenv(name, default).split(",") if x.strip()]


_ROOT = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.join(_ROOT, "data")

BOT_NAME = os.getenv("BOT_NAME", "Buddy")  # what the assistant calls itself
OWNER_NAME = os.getenv("OWNER_NAME", "")   # how it refers to you; empty = "the user"
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID") or 0)
# Optional server channel for chatting + reminders. 0 = DMs only.
CHANNEL_ID = int(os.getenv("CHANNEL_ID") or 0)

# --- Language model -------------------------------------------------------------------------
# Provider: "ollama" (default), or "openai" for any OpenAI-compatible server
# (llama.cpp server, LM Studio, vLLM, a cloud API, ...). The OLLAMA_* names keep working.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", OLLAMA_HOST if LLM_PROVIDER == "ollama" else "http://localhost:8080/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL") or os.getenv("OLLAMA_MODEL", "qwen3:8b")
# Smaller/faster model for side jobs (rewriting replies for speech, summaries). Empty = LLM_MODEL.
LLM_FAST_MODEL = os.getenv("LLM_FAST_MODEL", "") or LLM_MODEL
# Context window in tokens. Everything the agent sees (tools, memories, history) is budgeted to fit,
# so raising this on bigger hardware automatically gives it more memory and history.
LLM_CONTEXT = int(os.getenv("LLM_CONTEXT") or os.getenv("OLLAMA_NUM_CTX", "6144"))
# Tokens kept free for the model's answer.
LLM_REPLY_TOKENS = int(os.getenv("LLM_REPLY_TOKENS", "1024"))
# How many model requests may run at once. 1 keeps VRAM flat; raise it if your server can batch.
LLM_CONCURRENCY = int(os.getenv("LLM_CONCURRENCY", "1"))
# How many tool-use rounds one message may take (multi-step tasks need more).
LLM_MAX_STEPS = int(os.getenv("LLM_MAX_STEPS", "8"))
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.4"))
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "300"))
# How long Ollama keeps the model loaded in VRAM after a request ("-1" = forever).
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "30m")
_think = os.getenv("LLM_THINK", os.getenv("OLLAMA_THINK", "")).strip().lower()
# None = don't send the flag (use for models without a thinking mode).
LLM_THINK = None if _think == "" else _think in ("1", "true", "yes")
# Optional embedding model (e.g. "nomic-embed-text") for meaning-based memory search.
# Empty = keyword search only.
EMBED_MODEL = os.getenv("EMBED_MODEL", "")

# --- Agent ----------------------------------------------------------------------------------
# Personality/instructions file. Edit it to change who the assistant is, no code needed.
PERSONA_FILE = os.getenv("PERSONA_FILE", os.path.join(_DATA, "persona.md"))
# Skills (folders/modules in skills/) to turn off, comma-separated, e.g. "web,checkins".
SKILLS_DISABLED = _list("SKILLS_DISABLED")
# Extra folders to load skills from (your own plug-ins), separated by ";" on Windows or ":" elsewhere.
SKILL_PATHS = [p for p in os.getenv("SKILL_PATHS", "").split(os.pathsep) if p.strip()]
# Upper bound on chat messages the model sees; the context budget usually trims earlier.
HISTORY_MESSAGES = int(os.getenv("HISTORY_MESSAGES", "40"))
DB_PATH = os.getenv("DB_PATH", os.path.join(_DATA, "jarofass.db"))

# --- Schedule / planner ---------------------------------------------------------------------
TZ = ZoneInfo(os.getenv("TIMEZONE", "UTC"))
DIGEST_TIME = os.getenv("DIGEST_TIME", "08:00")  # HH:MM local time
REMIND_LEAD_MINUTES = int(os.getenv("REMIND_LEAD_MINUTES", "30"))
# Days work items are shown. On other days work is hidden from digests/agenda and
# work reminders are postponed to the next work day at DIGEST_TIME.
WORK_DAYS = os.getenv("WORK_DAYS", "mon,tue,wed,thu,fri")
# Google events whose title contains any of these (comma-separated, case-insensitive) are tagged work.
GCAL_WORK_KEYWORDS = _list("GCAL_WORK_KEYWORDS")

# Work check-in: asks what you got done, nags until you answer.
WORK_CHECKIN_TIME = os.getenv("WORK_CHECKIN_TIME", "14:30")
WORK_CHECKIN_UNTIL = os.getenv("WORK_CHECKIN_UNTIL", "18:00")
WORK_CHECKIN_NAG_MINUTES = int(os.getenv("WORK_CHECKIN_NAG_MINUTES", "30"))
WORK_CHECKIN_DAYS = os.getenv("WORK_CHECKIN_DAYS", "mon,tue,wed,thu,fri")

# --- Web access (optional, off by default: it sends search queries to the internet) ----------
WEB_ENABLED = _bool("WEB_ENABLED")
WEB_MAX_CHARS = int(os.getenv("WEB_MAX_CHARS", "4000"))  # page text returned to the model

# --- Google Calendar (optional) ---
GCAL_ENABLED = _bool("GCAL_ENABLED")
GCAL_CREDENTIALS = os.getenv("GCAL_CREDENTIALS", os.path.join(_DATA, "credentials.json"))
GCAL_TOKEN = os.getenv("GCAL_TOKEN", os.path.join(_DATA, "token.json"))
GCAL_CALENDAR_ID = os.getenv("GCAL_CALENDAR_ID", "primary")
GCAL_SYNC_DAYS = int(os.getenv("GCAL_SYNC_DAYS", "30"))
GCAL_SYNC_MINUTES = int(os.getenv("GCAL_SYNC_MINUTES", "5"))
GCAL_EVENT_MINUTES = int(os.getenv("GCAL_EVENT_MINUTES", "60"))  # default length of new events


def owner() -> str:
    return OWNER_NAME or "the user"


# --- Delegation to coding agents (skills/delegate) ------------------------------------------
def _repos(spec: str) -> dict[str, str]:
    """"name=/path,other=/path2" -> {name: absolute path}. Only these repositories can be worked on."""
    out = {}
    for part in spec.split(","):
        name, _, path = part.strip().partition("=")
        if name and path:
            out[name.strip()] = os.path.abspath(os.path.expanduser(path.strip()))
    return out


def _find(name: str) -> str:
    """A CLI the service can't find through PATH (systemd services don't load your shell profile)."""
    import glob
    home = os.path.expanduser("~")
    candidates = [os.path.join(home, ".local", "bin", name), os.path.join("/usr/local/bin", name),
                  os.path.join(home, ".opencode", "bin", name),
                  *sorted(glob.glob(os.path.join(home, ".nvm", "versions", "node", "*", "bin", name)), reverse=True)]
    return next((c for c in candidates if os.path.exists(c)), name)


def _tiers(spec: str) -> dict[str, tuple[str, str]]:
    """"cheap=opencode:openai/gpt-6-luna,standard=claude:sonnet" -> {tier: (backend, model)}."""
    out = {}
    for part in spec.split(","):
        tier, _, rest = part.strip().partition("=")
        backend, _, model = rest.strip().partition(":")
        if tier and backend in ("claude", "opencode") and model:
            out[tier.strip()] = (backend, model.strip())
    return out


# Repositories a Claude Code agent may work in, as name=path pairs. Empty = delegation off.
AGENT_REPOS = _repos(os.getenv("AGENT_REPOS", ""))
# Folder where tickets without a repository start a NEW local git project (empty = such tickets are refused).
AGENT_NEW_PROJECTS = os.path.expanduser(os.getenv("AGENT_NEW_PROJECTS", ""))
# Path to the Claude Code CLI (services don't get your shell's PATH).
CLAUDE_PATH = os.getenv("CLAUDE_PATH", "") or _find("claude")
# Path to the OpenCode CLI (used by tiers with the "opencode" backend).
OPENCODE_PATH = os.getenv("OPENCODE_PATH", "") or _find("opencode")
# Worker tiers the model may pick, as tier=backend:model. Backends: "claude" (Claude Code; model sonnet|opus)
# or "opencode" (OpenCode; model as provider/model, e.g. openai/gpt-6-luna).
AGENT_TIERS = _tiers(os.getenv("AGENT_TIERS", "standard=claude:sonnet,strong=claude:opus")) \
    or {"standard": ("claude", "sonnet")}
AGENT_DEFAULT_TIER = os.getenv("AGENT_DEFAULT_TIER", "") or ("standard" if "standard" in AGENT_TIERS else next(iter(AGENT_TIERS)))
AGENT_MAX_BUDGET_USD = float(os.getenv("AGENT_MAX_BUDGET_USD", "5"))        # per run (--max-budget-usd)
AGENT_MONTHLY_BUDGET_USD = float(os.getenv("AGENT_MONTHLY_BUDGET_USD", "50"))  # sum of recorded run costs
AGENT_MAX_CONCURRENT = int(os.getenv("AGENT_MAX_CONCURRENT", "1"))
AGENT_PERMISSION_MODE = os.getenv("AGENT_PERMISSION_MODE", "auto")
AGENT_TIMEOUT_MINUTES = int(os.getenv("AGENT_TIMEOUT_MINUTES", "45"))
# Where per-task git worktrees are created (outside the repositories themselves).
AGENT_WORKTREES = os.path.expanduser(os.getenv("AGENT_WORKTREES", "~/.cache/little-buddy/worktrees"))

# --- HTTP API for a remote CLI (buddyctl.py). Off unless API_TOKEN is set. -------------------
API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("API_PORT", "8765"))
API_TOKEN = os.getenv("API_TOKEN", "")
