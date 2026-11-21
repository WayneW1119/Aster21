"""Request-scoped context, inspired by Codex's session/step_context.rs.

Capture without awaiting: prompt contents and advertised/executed tool bindings
belong to the same request. Runtime services (processes, generators, memory tools)
remain shared; this is not a deep copy of a running Session.
"""

import json
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from ..model_protocol import ModelAdapter
from ..client_common import ModelClient, Prompt
from ..context.world_state import WorldStateSnapshot
from ..context_manager.navigation import HistoryView
from ..tools.router import ToolRouter
from ..mcp.binding import McpBinding

if TYPE_CHECKING:
    from .session import Session, SessionConfiguration


@dataclass(frozen=True)
class StepContext:
    settings: "SessionConfiguration"
    client: ModelClient | ModelAdapter
    tool_router: ToolRouter
    _prompt: Prompt
    world_state_snapshot: WorldStateSnapshot
    history_view: HistoryView
    mcp: McpBinding

    @property
    def prompt(self) -> Prompt:
        # Consumers cannot mutate the captured request through returned lists/dicts.
        return deepcopy(self._prompt)

    @classmethod
    def capture(cls, session: "Session") -> "StepContext":
        from .session import CHANNEL_INSTRUCTIONS, EVENT_INSTRUCTIONS, PERSISTENCE_INSTRUCTIONS

        settings = session.configuration
        router = ToolRouter(session.router.registry.snapshot(),
            mcp_binding=session.deferred_binding(),
            loaded_mcp=(session.mcp_projection.loaded_definitions()
                        if session.mcp_projection is not None else {}))
        client = session.client
        # Stateful test/custom clients need not be cloned. Transport adapters may
        # capture their configuration while sharing only intentional services.
        capture_client = getattr(client, "for_step", None)
        if capture_client is not None:
            client = capture_client()
        instructions = settings.base_instructions + "\n\n" + EVENT_INSTRUCTIONS
        session.validate_world_state_sections()
        world = session.build_world_state()
        rendered = world.render_context(session.state.history.snapshot())
        items = list(rendered.history)
        history = session.state.history
        view = HistoryView(tuple(history.ids()), history.generation, history.revision)
        for item in items:
            if (item.get("type") == "message" and item.get("role") == "user"
                    and item.get("source") == "terminal"):
                item.pop("source")  # Internal metadata is not a Responses API field.
                attachments = item.pop("_attachments", None)
                item.pop("_message_fingerprint", None)
                item["text"] = ("<terminal_message>\n" + json.dumps(
                    {"source": "terminal", "text": item["text"],
                     **({"attachments": attachments} if attachments else {})}, ensure_ascii=False)
                    + "\n</terminal_message>")
        if session.rollout is not None:
            instructions += "\n\n" + PERSISTENCE_INSTRUCTIONS
        # Keep persistence under the runtime heading before another H1.
        instructions += "\n\n" + CHANNEL_INSTRUCTIONS
        if session.agent_context is not None:
            from ..context_manager.agent_context import MEMORY_INSTRUCTIONS
            context = session.agent_context
            instructions += ("\n\n" + MEMORY_INSTRUCTIONS + "\n\n# Agent-specific instructions\n"
                             + f"Source: {context.instructions_path}\n\n" + context.instructions())
        prompt = replace(rendered, history=tuple(items)).to_prompt(instructions, router.registry.specs())
        prompt = replace(prompt, cache_session_id=session.thread_id or session.session_id)
        return cls(settings, client, router, prompt,
                   deepcopy(session.state.world_state_baseline or WorldStateSnapshot()), view,
                   session.services.mcp.binding)
