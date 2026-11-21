"""Tool execution lifecycle; Session keeps scheduling and Step keeps sampling.

The invocation scope also crosses asyncio.to_thread, so synchronous history and
memory handlers observe the captured request rather than mutable global fields.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from ..context.world_state.memory import MemoryState
from ..session.input_queue import HarnessEvent
from .context import ToolInvocation
from .names import is_default_namespace

if TYPE_CHECKING:
    from ..session.session import Session
    from ..session.step_context import StepContext


class ToolRuntime:
    def __init__(self, session: "Session") -> None:
        self.session = session
        self._invocation: ContextVar[ToolInvocation | None] = ContextVar(
            "tool_invocation", default=None)

    @property
    def current_invocation(self) -> ToolInvocation | None:
        return self._invocation.get()

    @contextmanager
    def invocation_scope(self, invocation: ToolInvocation):
        token = self._invocation.set(invocation)
        try:
            yield
        finally:
            self._invocation.reset(token)

    async def execute_batch(self, context: "StepContext", calls: list[dict],
                            compact_boundary: set[str]) -> None:
        boundary = frozenset(compact_boundary) if len(calls) == 1 else None
        for call in calls:
            await self.execute(ToolInvocation(call, context, boundary))

    async def execute(self, invocation: ToolInvocation) -> None:
        session = self.session
        call = invocation.call

        def trace(event: str, data: dict) -> None:
            session.trace(event, {"session_id": session.session_id,
                                 "step": session.state.step_number, **data})

        trace("tool_started", {"call": call})
        is_memory_tool = (session.memory is not None and is_default_namespace(call.get("namespace"))
                          and call.get("name") in MemoryState.TOOL_NAMES)
        if is_memory_tool and session.agent_context.read_memory("index.md") != session.memory.current["text"]:
            raise RuntimeError("Memory invariant failed before memory tool: untracked disk change")
        with self.invocation_scope(invocation):
            outcome = await invocation.step.tool_router.invoke_async(call)
        result = outcome.to_item(call["call_id"])
        if is_memory_tool:
            result["output"], changed = session.memory.finish_tool(
                call, result["output"], session.agent_context)
            if changed:
                if session.rollout is not None:
                    session.rollout.append("memory_state", {"state": session.memory.to_dict(),
                                                            "legacy_hidden_ids": []})
                trace("memory_version_committed", {"memory": session.memory.to_dict(),
                                                    "call_id": call["call_id"]})
        raw_result = result
        result, limited = session.tool_outputs.limit(result, call,
            invocation.step.settings.tool_output_token_limit, memory_tool=is_memory_tool,
            protected_fields=outcome.protected_fields)
        if limited is not None or outcome.raw_output is not None:
            trace("tool_result_raw", {"result": (
                {"type": "function_call_output", "call_id": call["call_id"], "output": outcome.raw_output}
                if outcome.raw_output is not None else raw_result), "budget": limited})
        if limited is not None:
            trace("tool_output_limited", limited)
        trace("tool_result", {"result": result, "wake_model": outcome.wake_model})
        if not outcome.wake_model:
            session.record_items([result])
            trace("history_updated", {"reason": "non_waking_tool_receipt",
                                       "items": session.state.history.snapshot()})
        else:
            await session.enqueue_event(HarnessEvent("tool_result", result), internal=True)
        if limited is not None and limited["kind"] == "tool_definitions":
            import json
            await session.enqueue_event(HarnessEvent("tool_output_limited", {
                "type": "message", "role": "user", "text": "<harness_event>\n" + json.dumps({
                    "source": "runtime", "kind": "tool_output_limited", "data": limited},
                    ensure_ascii=False) + "\n</harness_event>"}), internal=True)
