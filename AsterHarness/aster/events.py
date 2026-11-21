"""Event-source helpers, sharing the existing event protocol and state isolation."""

from agent_harness.events import emit_event, state_directory

__all__ = ["emit_event", "state_directory"]
