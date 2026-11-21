import math
import time
from dataclasses import dataclass, field
from typing import ClassVar

from .types import Emit
from .timer_wait import wait_until


@dataclass
class TimerGenerator:
    delay_ms: int
    message: str
    interval_ms: int | None = None
    repeat_count: int = 1  # 0 explicitly means repeat until cancelled.
    kind: ClassVar[str] = "timer"
    events_emitted: int = field(default=0, init=False)
    next_fire_at: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if type(self.delay_ms) is not int or self.delay_ms < 1:
            raise ValueError("delay_ms must be a positive integer")
        if not isinstance(self.message, str) or not self.message.strip() or len(self.message) > 4000:
            raise ValueError("message must contain 1-4000 characters")
        if type(self.repeat_count) is not int or not 0 <= self.repeat_count <= 10000:
            raise ValueError("repeat_count must be 0-10000 (0 repeats until cancelled)")
        if self.interval_ms is not None and (type(self.interval_ms) is not int or self.interval_ms < 1000):
            raise ValueError("interval_ms must be an integer >= 1000")
        if self.repeat_count != 1 and self.interval_ms is None:
            raise ValueError("Repeating timers require interval_ms >= 1000")
        self.next_fire_at = time.time() + self.delay_ms / 1000

    def describe(self) -> dict:
        return {"delay_ms": self.delay_ms, "message": self.message,
                "interval_ms": self.interval_ms, "repeat_count": self.repeat_count}

    def checkpoint(self) -> dict:
        return {"events_emitted": self.events_emitted, "next_fire_at": self.next_fire_at}

    @classmethod
    def restore(cls, configuration: dict, state: dict) -> "TimerGenerator":
        timer = cls(**configuration)
        if not isinstance(state, dict) or set(state) != {"events_emitted", "next_fire_at"}:
            raise ValueError("Invalid timer checkpoint")
        count, deadline = state["events_emitted"], state["next_fire_at"]
        if (type(count) is not int or count < 0
                or (timer.repeat_count and count > timer.repeat_count)
                or (deadline is not None and (type(deadline) not in (int, float)
                                              or not math.isfinite(deadline)))
                or ((timer.repeat_count != 0 and count == timer.repeat_count) != (deadline is None))):
            raise ValueError("Invalid timer progress or deadline")
        timer.events_emitted = count
        timer.next_fire_at = deadline
        return timer

    async def run(self, emit: Emit) -> None:
        while self.next_fire_at is not None:
            await wait_until(self.next_fire_at)
            count = self.events_emitted + 1
            await emit("timer_fired", {"message": self.message, "occurrence": count})
            self.events_emitted = count
            self.next_fire_at = (None if self.repeat_count and count >= self.repeat_count
                                 else time.time() + self.interval_ms / 1000)
