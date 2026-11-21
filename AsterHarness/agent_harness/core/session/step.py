"""One snapshot/request/tool batch; scheduling is owned by Session, not this module."""

import asyncio
import time
from dataclasses import asdict
from typing import TYPE_CHECKING

from ..model_protocol import invoke
from ..client_common import Prompt, RetryableModelError
from .input_queue import HarnessEvent
from .step_context import StepContext

if TYPE_CHECKING:
    from .session import Session

MAX_MODEL_RETRIES = 5
MODEL_RETRY_BUDGET_SECONDS = 600


class ModelRequestPaused(Exception):
    """An uncommitted model request was checkpointed for an explicit resume."""


async def request_model(session, context, prompt, trace):
    attempt = 0
    loop = asyncio.get_running_loop()
    started = loop.time()
    while True:
        if attempt and loop.time() - started >= MODEL_RETRY_BUDGET_SECONDS:
            raise RuntimeError('Model retry time budget exhausted; explicit resume required')
        try:
            response = await asyncio.to_thread(invoke, context.client, prompt)
            session.state.status = "running"
            return response
        except RetryableModelError as error:
            if attempt >= MAX_MODEL_RETRIES:
                raise RuntimeError(f'Model retry limit reached ({MAX_MODEL_RETRIES} retries); explicit resume required') from None
            attempt += 1
            delay = min(30, 2 ** min(attempt - 1, 5))
            hint = getattr(error, 'retry_after_seconds', None)
            if type(hint) in (int, float) and 0 <= hint <= 3600:
                delay = max(delay, hint)
            if loop.time() - started + delay >= MODEL_RETRY_BUDGET_SECONDS:
                raise RuntimeError('Model retry time budget exhausted; explicit resume required') from None
            session.state.status = "retrying"
            cause = error.__cause__
            status = getattr(cause, "code", None)
            failure_context = error.__context__
            trace("model_retrying", {"attempt": attempt, "max_retries": MAX_MODEL_RETRIES, "delay_seconds": delay,
                  "observed_at": time.time(), "elapsed_seconds": loop.time() - started,
                  "error_type": type(error).__name__,
                  "cause_type": type(cause).__name__ if cause else None,
                  "context_type": type(failure_context).__name__ if failure_context else None,
                  "http_status": status if type(status) is int and 100 <= status <= 599 else None,
                  "network_reason_type": type(cause.reason).__name__ if cause and hasattr(cause, "reason") else None,
                  "reason": "Transient model/auth transport failure; no output or tools committed"})
            deadline = loop.time() + delay
            while loop.time() < deadline:
                if session._suspending or session.input_queue.closed:
                    # Original inputs are already archived. Add only a wake-up;
                    # never record them again or re-execute a previous tool batch.
                    await session.enqueue_event(HarnessEvent("model_retry_pending", {
                        "type": "message", "role": "user",
                        "text": '<harness_event>{"kind":"model_retry_pending","data":{"note":"Continue the interrupted model request from existing history; do not repeat completed actions."}}</harness_event>'}), internal=True)
                    raise ModelRequestPaused()
                await asyncio.sleep(min(.1, max(0, deadline - loop.time())))


def build_prompt(session: "Session") -> Prompt:
    """Inspection helper; execution captures one StepContext and keeps it throughout."""
    return StepContext.capture(session).prompt


async def run_step(session: "Session", batch: list[HarnessEvent]) -> None:
    state = session.state
    state.step_number += 1
    step = state.step_number
    history = state.history

    def trace(event: str, data: dict) -> None:
        session.trace(event, {"session_id": session.session_id, "step": step, **data})

    if session.rollout is not None:
        session.rollout.append("step_boundary", {"session_id": session.session_id, "step_number": step})
    trace("step_started", {"events": [event.snapshot() for event in batch]})
    await session.apply_step_filesystem_policy(batch)
    await session.apply_step_mcp_configuration(batch)
    await session.apply_step_custom_tool_changes(batch)
    await session.apply_step_skill_changes(batch)
    session.record_items([event.item for event in batch])
    session.record_step_world_state_if_changed()
    trace("history_updated", {"reason": "input_events", "items": history.snapshot()})
    context = StepContext.capture(session)
    prompt = context.prompt
    session.checkpoint_boundary("model_request")
    trace("model_request", {"prompt": asdict(prompt),
                            "world_state": context.world_state_snapshot.to_dict()})
    # Network I/O is synchronous in the adapter, but must not block event ingress.
    response = await request_model(session, context, prompt, trace)
    output = [item.to_record() for item in response.items]
    if any("_generator_inventory" in item or "_environment_state" in item or "_filesystem_policy" in item or "_skills_state" in item or "_mcp_sources" in item or "_mcp_searches" in item or "_mcp_configuration" in item
           or "_custom_tool_change" in item or "_skill_change" in item
           or item.get("type") in {"tool_search_output", "additional_tools"}
           for item in output):
        raise RuntimeError("Model output cannot introduce runtime MCP context evidence")
    calls = [record for item, record in zip(response.items, output) if item.kind in {"tool_call", "tool_discovery"}]
    trace("model_response", {"output": output, "usage": response.usage})
    ids = [call.get("call_id") for call in calls]
    previous_ids = {item.get("call_id") for item in
                    (history.item(identifier) for identifier in history.ids("archive"))
                    if item.get("type") in {"function_call", "tool_search_call"}}
    if (any(not isinstance(i, str) or not i for i in ids)
            or len(set(ids)) != len(ids) or any(i in previous_ids for i in ids)):
        raise RuntimeError("Missing or duplicate tool call_id")
    compact_boundary = set(history.ids())
    session.record_items(output)
    trace("history_updated", {"reason": "model_output", "items": history.snapshot()})

    # No calls is not an end marker. With no new events, the scheduler will simply wait.
    if not calls:
        trace("protocol_warning", {"reason": "No tool calls; assistant text is not sent "
                                  "to the user. No synthetic wake-up event is generated."})
    async with session.services.mcp.retain_batch_clients():
        await session.tool_runtime.execute_batch(context, calls, compact_boundary)
    # Queue after the whole tool batch: the reminder never separates calls from
    # their receipts, and usage from before a successful compaction is obsolete.
    await session.warn_context_budget(response.usage,
        context.settings.context_budget_warning_tokens, context.history_view.generation)
    session.checkpoint_boundary("step_complete")
    trace("step_completed", {"pending_events": session.input_queue.snapshot()})
