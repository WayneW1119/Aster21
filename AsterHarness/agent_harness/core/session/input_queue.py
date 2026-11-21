"""Session-scoped input coordination, inspired by Codex's input_queue.rs.

HarnessEvent is an input to the scheduler, not a trace record or an API item.
"""

import asyncio
from collections import deque
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from uuid import uuid4
from typing import Callable

from ..client_common import Item


@dataclass
class HarnessEvent:
    kind: str
    item: Item
    event_id: str = field(default_factory=lambda: str(uuid4()))

    def snapshot(self) -> dict:
        return deepcopy(asdict(self))


class InputQueue:
    def __init__(self) -> None:
        self._pending: deque[HarnessEvent] = deque()
        self._condition = asyncio.Condition()
        self.closed = False

    def snapshot(self) -> list[dict]:
        return [event.snapshot() for event in self._pending]

    def empty(self) -> bool:
        return not self._pending

    def restore(self, snapshots: list[dict]) -> None:
        """Hydrate before a scheduler starts; event IDs and ordering stay unchanged."""
        if self._pending or self.closed:
            raise ValueError("Restore requires an empty open queue")
        self._pending.extend(HarnessEvent(**deepcopy(snapshot)) for snapshot in snapshots)

    async def prepend(self, events: list[HarnessEvent]) -> None:
        # Suspend can race with a waiting drain. Return its unprocessed batch to the front.
        async with self._condition:
            self._pending.extendleft(reversed(events))
            self._condition.notify_all()

    async def put(self, event: HarnessEvent, *, internal: bool = False) -> None:
        async with self._condition:
            if self.closed and not internal:
                raise RuntimeError("Session input is closed")
            self._pending.append(deepcopy(event))
            self._condition.notify()

    async def drain(self, *, wait: bool,
                    wake_if: Callable[[], bool] | None = None) -> list[HarnessEvent]:
        async with self._condition:
            if wait:
                await self._condition.wait_for(lambda: self._pending or self.closed
                                                or (wake_if is not None and wake_if()))
            batch = list(self._pending)
            self._pending.clear()
            return batch

    async def wake_waiters(self) -> None:
        # A generator can finish/cancel without an input item; batch drivers recheck liveness.
        async with self._condition:
            self._condition.notify_all()

    async def close(self) -> None:
        # Reject new external messages, but let in-flight tools enqueue their results.
        async with self._condition:
            self.closed = True
            self._condition.notify_all()
