"""Event sources do not own history, model requests, or Session scheduling."""

from collections.abc import Awaitable, Callable
from typing import Protocol

Emit = Callable[[str, dict], Awaitable[object]]


class EventGenerator(Protocol):
    kind: str

    def describe(self) -> dict: ...

    async def run(self, emit: Emit) -> None: ...
