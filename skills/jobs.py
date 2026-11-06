"""Jobs skill: the assistant schedules its own future work.

"Every Friday at 5, ask me how the week went", "in an hour, check if I finished the report",
"every morning at 7, tell me something interesting about space". See core/scheduler.py.
"""
from typing import Annotated, Literal

from core import db, scheduler
from core.skills import command
from core.tools import tool

GUIDE = """Scheduled jobs (things YOU do later, unprompted):
- Use schedule_job when {owner} wants you to act later or repeatedly: check in about something, follow up, \
send a recurring message, look something up on a schedule. Write the instruction as a note to your future \
self with all the context needed, e.g. "Ask how the dentist appointment went; it was about the cracked molar."
- For `at`, pass the user's own words ('friday 17:00', 'in 2 hours'); the tool works out the exact date.
- For a simple "remind me to X at T", use add_item instead (it shows up on their agenda)."""


@tool(writes=True, owner_only=True)
def schedule_job(instruction: Annotated[str, "What to do when it runs, written as a note to your future self."],
                 at: Annotated[str, f"First run, local time: {db.WHEN_HELP}."],
                 repeat: Literal["none", "hourly", "daily", "weekdays", "weekly", "monthly", "yearly"] = "none",
                 title: Annotated[str, "Short label for listings."] = "",
                 send_as_is: Annotated[bool, "true = send the instruction text verbatim instead of acting on it."] = False) -> str:
    """Schedule something for you to do later, once or repeatedly (a check-in, follow-up or recurring message)."""
    when = db.parse(at)  # a date alone runs at 09:00
    if when < db.now():
        return f"Error: {at} is in the past (now is {db.fmt(db.now())})."
    job = scheduler.add(title or instruction[:60], instruction, when,
                        "message" if send_as_is else "agent", repeat)
    return "Scheduled: " + scheduler.describe(job)


@tool
def list_jobs() -> str:
    """List the jobs you've scheduled for yourself."""
    return "\n".join(scheduler.describe(j) for j in scheduler.pending()) or "No scheduled jobs."


@tool(writes=True, owner_only=True)
def cancel_job(id: int) -> str:
    """Cancel a scheduled job by id."""
    return f"Cancelled job #{id}." if scheduler.cancel(id) else f"Error: no active job #{id}."


@command("!jobs", "jobs I've scheduled for myself")
def _jobs_cmd(args: str, ctx) -> str:
    return list_jobs()
