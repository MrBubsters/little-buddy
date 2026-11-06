"""The agent: build a prompt that fits the context window, then let the model think and use tools.

    reply = await agent.chat("what's the capital of Peru, and remind me to call mom at 6", ctx)

Nothing here knows about calendars or check-ins. Abilities come from skills (tools, guides and
live context), the personality from data/persona.md, and everything is sized to LLM_CONTEXT, so a
bigger model or context window is used automatically.
"""
import inspect
import json
import os
import re
from datetime import timedelta

import config
from core import db, llm, memory, skills, style
from core.tools import REGISTRY, ToolContext

DEFAULT_PERSONA = """You are {name}, {owner}'s personal AI assistant. You run privately on their own computer.

You're a capable generalist, not just an organizer. Answer questions, explain things, help think problems \
through, brainstorm, give honest opinions, write and edit, and keep track of their life: plans, reminders, \
notes, and what you've learned about them.

Think for yourself. Work out what they actually need, connect it with what you know about them, point out \
things they'd want to know (a clash, a forgotten deadline, a better idea) and say when you're unsure. Don't \
just repeat stored information back; use it. Be warm, direct and concise."""

RULES = """How to work:
- Use tools when they help: to save or change something, to look something up, or to do work you can't do \
reliably in your head. Answer directly when no tool is needed.
- Multi-step requests are fine: call several tools, check the results, then answer.
- Tool results are the truth. Never claim you did something unless a tool result says it happened. If a tool \
returns an error, fix the arguments and retry, or explain what went wrong.
- If something needs an ability you don't have, say so plainly instead of pretending.
- Never invent facts about {owner}, their plans or their past."""

CONTEXT_NOTE = """The newest message starts with a <context> block that the app adds: the current time, live \
information (like the schedule) and what you know about {owner}. Use it, but it's background, not something \
anyone said, so don't mention the block itself.""".replace("{owner}", config.owner())

TOOL_RESULT_SHARE = 0.25   # max share of the remaining budget one tool result may use
MEMORY_SHARE = 0.35        # share of the remaining budget for memories (the rest is chat history)


def persona() -> str:
    text = DEFAULT_PERSONA
    if config.PERSONA_FILE and os.path.exists(config.PERSONA_FILE):
        with open(config.PERSONA_FILE, encoding="utf-8") as f:
            text = re.sub(r"<!--.*?-->", "", f.read(), flags=re.S).strip() or DEFAULT_PERSONA
    return text.replace("{name}", config.BOT_NAME).replace("{owner}", config.owner())


def _guides(ctx: ToolContext, tool_skills: set[str]) -> list[str]:
    """How-to text from skills whose tools are available. Static, so it lives in the cached system prompt."""
    out = []
    for name, mod in skills.SKILLS.items():
        guide = getattr(mod, "GUIDE", None)
        has_tools = any(t.skill == name for t in REGISTRY.tools.values())
        if guide and (name in tool_skills or not has_tools):
            text = guide(ctx) if callable(guide) else guide
            if text:
                out.append(text.replace("{owner}", config.owner()).strip())
    return out


async def _live_context(ctx: ToolContext, query: str) -> list[str]:
    """Each skill's `context` hook: information that changes from message to message."""
    out = []
    for name, fn in skills.hooks("context"):
        try:
            text = fn(ctx, query)
            text = await text if inspect.isawaitable(text) else text
        except Exception as e:
            print(f"[agent] {name}.context failed: {e!r}")
            text = None
        if text:
            out.append(text.strip())
    return out


def _memory_section(facts: list[dict], notes: list[dict], complete: bool) -> str:
    owner = config.owner()
    lines = [f"What you know about {owner} (background: use it when it's relevant, don't recite it):"]
    lines += [f"  [#{f['id']}] {f['content']}" for f in facts] or ["  (nothing yet)"]
    if not complete:
        lines.append("  (Only the most relevant are shown. Use recall to search for more.)")
    if notes:
        lines.append("Saved notes that may be relevant to this message:")
        lines += [f"  [note #{n['id']}] {n['content']}" for n in notes]
    return "\n".join(lines)


def _speaker_section(ctx: ToolContext) -> str:
    if ctx.is_owner:
        return ""
    return (f"Who's talking: {ctx.speaker or 'someone'}, NOT {config.owner()}. What you know is about "
            f"{config.owner()}, not about them. Be friendly, but don't share {config.owner()}'s private "
            f"details unless {config.owner()} is clearly happy for you to.")


def system_prompt(ctx: ToolContext, tool_skills: set[str]) -> str:
    """Everything that stays the same from message to message.

    Keep anything that changes (the time, the agenda, memories) OUT of here: the model server reuses
    the unchanged start of a prompt from its cache, and on slower GPUs re-reading a few thousand
    tokens on every message costs seconds. Changing parts go in the <context> block instead.
    """
    parts = [persona(), style.for_mode(ctx.mode), *_guides(ctx, tool_skills),
             RULES.replace("{owner}", config.owner()), CONTEXT_NOTE]
    return "\n\n".join(p for p in parts if p)


async def build_messages(text: str, ctx: ToolContext) -> tuple[list[dict], list[dict]]:
    """(messages, tool schemas) for one turn, sized to fit the context window.

    Layout, from most to least stable: [system: persona, style, guides, rules] [tool schemas]
    [earlier chat] [newest message: <context>time, live skill context, memories</context> + text].
    Everything before the newest message is usually identical to the previous request, so it's
    served from the model server's cache.
    """
    tools = REGISTRY.available(ctx)
    schemas = [t.schema() for t in tools]
    system = system_prompt(ctx, {t.skill for t in tools})
    n = db.now()
    live = [f"Now: {n.strftime('%A %Y-%m-%d %H:%M')} ({config.TZ.key}). "
            f"Tomorrow is {(n + timedelta(days=1)).strftime('%A %Y-%m-%d')}.",
            _speaker_section(ctx), *await _live_context(ctx, text)]

    budget = config.LLM_CONTEXT - config.LLM_REPLY_TOKENS
    budget -= (llm.tokens(json.dumps(schemas)) + llm.tokens(system) + llm.tokens("\n\n".join(live))
               + llm.tokens(text) + 60)
    if budget < 200:
        print(f"[agent] context is tight ({budget} tokens left for memory/history). "
              f"Raise LLM_CONTEXT or turn off skills with SKILLS_DISABLED.")
    mem_budget = max(100, int(budget * MEMORY_SHARE))
    facts, notes, complete = await memory.for_prompt(text, mem_budget)
    live.append(_memory_section(facts, notes, complete))
    budget -= llm.tokens(live[-1])

    # Recent conversation, newest first, until the budget runs out.
    history, used = [], 0
    for m in reversed(db.recent_messages(config.HISTORY_MESSAGES, ctx.conversation)):
        cost = llm.tokens(m["content"]) + 4
        if used + cost > budget:
            break
        history.insert(0, m)
        used += cost
    while history and history[0]["role"] != "user":
        history.pop(0)  # don't start mid-exchange

    said = text if ctx.is_owner else f"({ctx.speaker or 'someone'} says) {text}"
    block = "\n\n".join(x for x in live if x)
    user = f"<context>\n{block}\n</context>\n\n{said}"
    return [{"role": "system", "content": system}, *history, {"role": "user", "content": user}], schemas


_PROMISE = re.compile(r"\bI(?:'ll| will)\s+(?:\w+\s+)?(?:check|remind|ping|follow[- ]up|ask|message|text|"
                      r"get back|let you know|nudge|circle back)\b", re.I)
_NUDGE = ("[System check: your reply promises to do something later, but nothing was scheduled this turn, so it "
          "won't happen. If they asked for it, call the right tool now (schedule_job or add_item). Otherwise, "
          "rewrite your reply without that promise.]")


def _can_schedule(schemas: list[dict]) -> bool:
    return any(s["function"]["name"] in ("schedule_job", "add_item") for s in schemas)


def _cap(result: str, messages: list[dict], schemas: list[dict]) -> str:
    used = sum(llm.tokens(m.get("content") or "") for m in messages) + llm.tokens(json.dumps(schemas))
    room = int((config.LLM_CONTEXT - config.LLM_REPLY_TOKENS - used) * TOOL_RESULT_SHARE * 3)  # chars
    room = max(room, 600)
    return result if len(result) <= room else result[:room] + f"\n… (cut off: {len(result) - room} more characters)"


async def chat(text: str, ctx: ToolContext | None = None) -> str:
    """Answer one message. Saves the exchange to the conversation's history."""
    ctx = ctx or ToolContext()
    messages, schemas = await build_messages(text, ctx)
    reply, receipts, nudged = "", [], False
    for step in range(config.LLM_MAX_STEPS):
        last = step == config.LLM_MAX_STEPS - 1
        if last:
            messages.append({"role": "user", "content": "[You've used many steps. Answer now with what you have.]"})
        resp = await llm.chat(messages, None if last else schemas)
        if not resp.tool_calls:
            reply = resp.content
            if not last and not nudged and not receipts and _PROMISE.search(reply) and _can_schedule(schemas):
                # Small models often promise to do something later without scheduling it.
                nudged = True
                messages += [{"role": "assistant", "content": reply}, {"role": "user", "content": _NUDGE}]
                continue
            break
        messages.append({"role": "assistant", "content": resp.content, "tool_calls": resp.tool_calls})
        for call in resp.tool_calls:
            result = await REGISTRY.run(call["name"], call["arguments"], ctx)
            print(f"[tool] {call['name']}({json.dumps(call['arguments'], default=str)}) -> {result[:300]}")
            messages.append({"role": "tool", "content": _cap(result, messages, schemas),
                             "tool_call_id": call["id"], "name": call["name"]})
            t = REGISTRY.get(call["name"])
            if t and t.writes and not result.startswith("Error"):
                receipts.append(result.splitlines()[0])
    reply = await style.finish(ctx.mode, reply or ("Done." if receipts else "Hmm, I didn't get an answer together. Could you try again?"))
    # Ground truth from the code, not the model: what was actually changed this turn.
    # "-# " renders as small grey subtext in Discord.
    if receipts:
        reply += "\n" + "\n".join(f"-# ✔ {r}" for r in receipts)
    db.add_message("user", text if ctx.is_owner else f"({ctx.speaker or 'someone'} says) {text}", ctx.conversation)
    db.add_message("assistant", reply, ctx.conversation)
    return reply


async def warm_up() -> str:
    """Check the model backend and load the model. Returns a status line."""
    return await llm.status()


def describe_budget(ctx: ToolContext | None = None) -> str:
    """Where the context window goes (for !status)."""
    ctx = ctx or ToolContext()
    tools = REGISTRY.available(ctx)
    t = llm.tokens(json.dumps([x.schema() for x in tools]))
    return (f"context {config.LLM_CONTEXT} tokens: ~{t} for {len(tools)} tools, "
            f"{config.LLM_REPLY_TOKENS} reserved for the reply, the rest for instructions, memory and history")
