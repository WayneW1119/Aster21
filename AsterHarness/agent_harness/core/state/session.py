from dataclasses import dataclass, field

from ..context_manager import ContextManager
from ..context.world_state import WorldStateSnapshot


@dataclass
class SessionState:
    history: ContextManager = field(default_factory=ContextManager)
    status: str = "waiting"
    step_number: int = 0
    sent_messages: list[str] = field(default_factory=list)
    in_flight_events: list[dict] = field(default_factory=list)
    world_state_baseline: WorldStateSnapshot | None = None
