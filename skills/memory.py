"""Memory skill: lets the assistant remember facts, keep notes, and search them later."""
import re
from typing import Annotated

from core import memory
from core.skills import command
from core.tools import tool

GUIDE = """Memory:
- When {owner} shares a lasting personal detail (names, relationships, routines, preferences, goals), call \
remember with one short self-contained sentence. Not for requests, questions or passing remarks, and never \
for tasks, to-dos or project status: those go in the planner (add_item) so they can be checked off.
- When they ask you to keep something longer (an idea, a recipe, meeting notes, a list), call save_note.
- Before saying you don't know something about them, call recall. Correct outdated facts with update_memory.
- A memory ending in "(more: recall '#N')" is only a preview. If the question is about it, call recall with \
'#N' and answer from the full text."""


@tool(writes=True, owner_only=True)
async def remember(fact: Annotated[str, "One self-contained sentence, e.g. 'Sister Ana's birthday is March 4.'"]) -> str:
    """Store a lasting fact about the user (people, routines, preferences, goals)."""
    return f"Remembered (#{await memory.add(fact, 'fact')}): {fact}"


@tool(writes=True, owner_only=True)
async def save_note(title: str, content: Annotated[str, "The full text to keep."]) -> str:
    """Save a longer note the user wants kept (ideas, recipes, lists, meeting notes)."""
    mid = await memory.add(f"{title.strip()}: {content.strip()}", "note", tags=title)
    return f"Saved note #{mid}: {title}"


@tool
async def recall(query: Annotated[str, "What to look for, in a few words."]) -> str:
    """Search saved facts and notes (or fetch one in full by id, e.g. '#12')."""
    m = re.fullmatch(r"\s*#?(\d+)\s*", query)
    hits = [x for x in [memory.get(int(m[1]))] if x] if m else await memory.search(query, 6)
    return "\n".join(f"[#{m['id']} {m['kind']}] {m['content']}" for m in hits) or "Nothing saved about that."


@tool(writes=True, owner_only=True)
async def update_memory(id: int, content: str) -> str:
    """Replace the text of a saved fact or note by id."""
    return f"Updated #{id}: {content}" if await memory.update(id, content) else f"Error: no memory #{id}."


@tool(writes=True, owner_only=True)
def forget(id: int) -> str:
    """Delete a saved fact or note by id."""
    m = memory.get(id)
    return f"Forgot #{id}: {m['content'][:80]}" if m and memory.delete(id) else f"Error: no memory #{id}."


@command("!facts", "what I remember about you (`!facts notes` for saved notes)")
def _facts_cmd(args: str, ctx) -> str:
    kind = "note" if args.lower().startswith("note") else "fact"
    rows = memory.entries(kind)
    return "\n".join(f"[#{m['id']}] {m['content'][:300]}" for m in rows) or f"No {kind}s yet."
