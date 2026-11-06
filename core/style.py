"""How replies should read in each mode ("text", "scheduled", or any mode a front-end adds).

A front-end or skill with its own medium registers a style, and optionally a post-processor
that cleans up the final reply:

    from core import style
    style.register("sms", "Keep replies under 160 characters.", finish=my_async_shortener)
"""
from typing import Awaitable, Callable

import config

TEXT_STYLE = """You're chatting by text (Discord). Plain text with light markdown is fine. Match the length \
of your answer to the question: a quick question gets a quick answer, a request for depth gets depth."""

SCHEDULED_STYLE = TEXT_STYLE + """
This turn was started by a scheduled job, not by a message. Whatever you write is sent to {owner} as a \
notification, so make it worth reading: short, specific and useful."""

STYLES: dict[str, str] = {"text": TEXT_STYLE, "scheduled": SCHEDULED_STYLE}
FINISHERS: dict[str, Callable[[str], Awaitable[str]]] = {}


def register(mode: str, instructions: str, finish: Callable[[str], Awaitable[str]] | None = None) -> None:
    STYLES[mode] = instructions
    if finish:
        FINISHERS[mode] = finish


def for_mode(mode: str) -> str:
    return STYLES.get(mode, TEXT_STYLE).replace("{owner}", config.owner())


async def finish(mode: str, reply: str) -> str:
    fn = FINISHERS.get(mode)
    return await fn(reply) if fn else reply
