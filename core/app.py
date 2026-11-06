"""The bridge between skills and whichever front-end is running (Discord, the terminal, ...).

Skills call `await app.notify("text")` to message the owner unprompted; the front-end decides
how (a Discord DM with an @mention, a printed line, ...).
"""
from typing import Awaitable, Callable


class App:
    def __init__(self):
        self.frontend = "none"
        self._notify: Callable[[str], Awaitable[object]] | None = None

    def attach(self, frontend: str, notify: Callable[[str], Awaitable[object]]) -> None:
        self.frontend, self._notify = frontend, notify

    async def notify(self, text: str):
        """Send the owner a message. Returns a front-end message id when there is one."""
        if self._notify is None:
            print(f"[notify] {text}")
            return None
        return await self._notify(text)


app = App()
