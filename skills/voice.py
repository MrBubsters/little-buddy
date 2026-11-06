"""Voice skill: how the assistant talks when its words are spoken aloud (mode "voice").

Front-end-agnostic: anything that turns replies into speech (the Discord voice call in voice.py,
or a future phone/speaker front-end) passes ToolContext(mode="voice") and gets this style plus
the spoken-reply clean-up below. The audio pipeline itself lives in voice.py.
"""
import re

import config
from core import llm, style

VOICE_STYLE = """You're in a voice call, talking out loud: everything you write is spoken by a text-to-speech \
voice. What you hear was transcribed automatically, so expect small mistakes (misheard names or words); go with \
the most sensible meaning, or ask a quick question if it's really unclear. You hear everything said in the call, \
so it's an ongoing conversation: respond to each thing the way a friend on a call would.
- Respond to what they actually said. A greeting or "are you there?" gets a natural reply like "Hey! Yep, I'm \
here. What's up?", not a status report. Small talk, jokes and questions get a real, conversational answer.
- Facts, plans and chat history are about {owner}, not you. You don't have a job, a day or plans of your own. If \
they ask how you are, answer briefly and playfully, then turn it back to them.
- Don't bring up the schedule unless they ask, or something starts within the hour. When they ask about plans, \
say it in a sentence or two, most important first. If there's a lot, say how many and mention the one or two that \
matter most, then offer the rest. Only mention items that are actually in the schedule or a tool result; past \
chat isn't the schedule. If a day is free, just say so.
- Keep replies under about 35 words unless they ask for detail. Flowing spoken sentences, contractions, a warm \
relaxed tone, a little personality.
- NEVER use lists, bullet points, numbering, line breaks, headings, markdown, emoji or item ids: they sound \
robotic read aloud. Say dates and times the way people do ("tomorrow at 3", "Friday evening", "in about an hour"), \
never "2026-11-06 15:00".
- Only use tools when they ask you to save, change or look something up; afterwards confirm it casually in a few \
words. Don't end with stock offers like "let me know if you need anything". Only ask something back when you \
actually want the answer."""

# Stock closing lines small models love to add; they sound canned when spoken.
# Only whole trailing sentences are removed, never part of one.
_SIGN_OFF = re.compile(r"(?:^|(?<=[.!?]))\s*(?:Just\s+)?(?:let me know|feel free|is there anything else|"
                       r"anything else (?:I can|you need)|hope (?:that|this) helps)[^.!?]*[.!?]*\s*$", re.I)


def strip_sign_off(text: str) -> str:
    stripped = _SIGN_OFF.sub("", text.strip()).strip()
    return stripped or text.strip()


def looks_written(text: str) -> bool:
    """True if a reply reads like writing (lists, several lines, markdown) or is too long to say comfortably."""
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("-#")]
    body = "\n".join(lines)
    return (len(lines) > 2 or bool(re.search(r"^\s*(?:[-*•]|\d+[.)])\s", body, re.M))
            or "**" in body or "#" in re.sub(r"#\d", "", body) or len(body.split()) > 55)


async def speechify(reply: str) -> str:
    """Second, tool-free pass: rewrite a written-style reply as one to three spoken sentences."""
    resp = await llm.chat([
        {"role": "system", "content": "Rewrite the assistant's message as what a friendly person would say out "
                                      "loud on a voice call: at most two flowing, natural sentences and under 35 "
                                      "words. Summarize, don't enumerate: if there are several things, mention "
                                      "how many and the one or two that matter most, and offer the rest. No "
                                      "lists, line breaks, markdown, emoji or ids. Say times naturally (\"tomorrow "
                                      "at 3\"). No sign-off like \"let me know if you need anything\". Output only "
                                      "the rewritten words."},
        {"role": "user", "content": reply},
    ], model=config.LLM_FAST_MODEL, temperature=0.5, max_tokens=200, think=False)
    return resp.content or reply


async def finish(reply: str) -> str:
    """Make a reply sound natural spoken. Runs before receipts ("-# ✔ ...") are added."""
    if looks_written(reply):
        print("[voice] reply was list-formatted or long; rewriting for speech")
        reply = await speechify(reply)
    return strip_sign_off(reply)


style.register("voice", VOICE_STYLE, finish=finish)
