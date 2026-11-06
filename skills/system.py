"""System skill: help, status and housekeeping commands. Built-in, but just a skill like the others."""
from core import agent, db, llm, skills
from core.skills import command
from core.tools import REGISTRY

import config


@command("!help", "this message")
def _help_cmd(args: str, ctx) -> str:
    cmds = "\n".join(f"`{c.name}` {c.help}" for c in sorted(skills.COMMANDS.values(), key=lambda c: c.name))
    return (f"**{config.BOT_NAME}**: just talk to me normally. Ask questions, think out loud, have me remember "
            f"things, plan your week or check back on something later.\n"
            f"e.g. \"dentist next tuesday at 3pm\", \"what's 18% of 64?\", \"every friday at 5 ask me how the week "
            f"went\", \"remember my sister Ana's birthday is March 4\"\n\n{cmds}\n📆 = synced with Google Calendar")


@command("!reset", "clear this chat's history (items, memories and jobs are kept)")
def _reset_cmd(args: str, ctx) -> str:
    db.clear_messages(ctx.conversation)
    return "Chat history cleared (items, memories and jobs kept)."


@command("!status", "model, skills and how the context window is used")
async def _status_cmd(args: str, ctx) -> str:
    lines = [await llm.status(),
             f"provider {config.LLM_PROVIDER} @ {config.LLM_BASE_URL}; fast model '{config.LLM_FAST_MODEL}'; "
             f"embeddings: {config.EMBED_MODEL or 'off (keyword search)'}; concurrency {config.LLM_CONCURRENCY}",
             agent.describe_budget(ctx)]
    if llm.STATS["last_prompt_tokens"]:
        lines.append(f"last prompt used {llm.STATS['last_prompt_tokens']} tokens")
    lines.append(_skills_text())
    return "\n".join(lines)


def _skills_text() -> str:
    by_skill: dict[str, list[str]] = {}
    for t in REGISTRY.tools.values():
        by_skill.setdefault(t.skill, []).append(t.name + ("" if t.is_enabled() else " (off)"))
    lines = ["**Skills**"]
    for name in skills.SKILLS:
        lines.append(f"• {name}: {', '.join(by_skill.get(name, [])) or 'no tools'}")
    lines += [f"• ✗ {name}: {err}" for name, err in skills.FAILED.items()]
    if config.SKILLS_DISABLED:
        lines.append(f"_disabled: {', '.join(config.SKILLS_DISABLED)}_")
    return "\n".join(lines)


@command("!skills", "loaded skills and their tools")
def _skills_cmd(args: str, ctx) -> str:
    return _skills_text()
