import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

BOT_NAME = os.getenv("BOT_NAME", "Buddy")  # what the assistant calls itself
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID") or 0)
# Optional server channel for chatting + reminders. 0 = DMs only.
CHANNEL_ID = int(os.getenv("CHANNEL_ID") or 0)

# --- Local LLM (Ollama). Sized for an 8GB GPU. ---
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")
# Context window. 6144 keeps qwen3:8b (Q4) + KV cache around ~6.5GB VRAM.
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "6144"))
# How long Ollama keeps the model loaded in VRAM after a request ("-1" = forever).
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "30m")
_think = os.getenv("OLLAMA_THINK", "").strip().lower()
# None = don't send the flag (use for models without a thinking mode).
OLLAMA_THINK = None if _think == "" else _think in ("1", "true", "yes")

# --- Behaviour ---
TZ = ZoneInfo(os.getenv("TIMEZONE", "UTC"))
DIGEST_TIME = os.getenv("DIGEST_TIME", "08:00")  # HH:MM local time
REMIND_LEAD_MINUTES = int(os.getenv("REMIND_LEAD_MINUTES", "30"))
# Days work items are shown. On other days work is hidden from digests/agenda and
# work reminders are postponed to the next work day at DIGEST_TIME.
WORK_DAYS = os.getenv("WORK_DAYS", "mon,tue,wed,thu,fri")
# Google events whose title contains any of these (comma-separated, case-insensitive) are tagged work.
GCAL_WORK_KEYWORDS = [k.strip().lower() for k in os.getenv("GCAL_WORK_KEYWORDS", "").split(",") if k.strip()]

# Work check-in: asks what you got done, nags until you answer.
WORK_CHECKIN_TIME = os.getenv("WORK_CHECKIN_TIME", "14:30")
WORK_CHECKIN_UNTIL = os.getenv("WORK_CHECKIN_UNTIL", "18:00")
WORK_CHECKIN_NAG_MINUTES = int(os.getenv("WORK_CHECKIN_NAG_MINUTES", "30"))
WORK_CHECKIN_DAYS = os.getenv("WORK_CHECKIN_DAYS", "mon,tue,wed,thu,fri")
HISTORY_MESSAGES =int(os.getenv("HISTORY_MESSAGES", "12"))
_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.getenv("DB_PATH", os.path.join(_DATA, "jarofass.db"))

# --- Google Calendar (optional) ---
GCAL_ENABLED = os.getenv("GCAL_ENABLED", "false").strip().lower() in ("1", "true", "yes")
GCAL_CREDENTIALS = os.getenv("GCAL_CREDENTIALS", os.path.join(_DATA, "credentials.json"))
GCAL_TOKEN = os.getenv("GCAL_TOKEN", os.path.join(_DATA, "token.json"))
GCAL_CALENDAR_ID = os.getenv("GCAL_CALENDAR_ID", "primary")
GCAL_SYNC_DAYS = int(os.getenv("GCAL_SYNC_DAYS", "30"))
GCAL_SYNC_MINUTES = int(os.getenv("GCAL_SYNC_MINUTES", "5"))
GCAL_EVENT_MINUTES = int(os.getenv("GCAL_EVENT_MINUTES", "60"))  # default length of new events
