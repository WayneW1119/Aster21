"""ResponseEvent consumption, independent of HTTP and Step scheduling.

Like Codex, deltas are observational; completed output items carry replayable
content (including opaque reasoning). No partial argument execution or guessing.
"""

from copy import deepcopy
from typing import Iterable

from .client_common import ResponseEvent, RetryableModelError, retry_after_seconds


EVENT_KINDS = {
    "response.created": "created",
    "response.output_item.added": "output_item_added",
    "response.output_item.done": "output_item_done",
    "response.output_text.delta": "output_text_delta",
    "response.function_call_arguments.delta": "tool_call_input_delta",
    "response.reasoning_summary_text.delta": "reasoning_summary_delta",
    "response.reasoning_text.delta": "reasoning_delta",
    "response.completed": "completed",
    "response.failed": "failed",
    "response.incomplete": "failed",
    "error": "failed",
}


def normalize_event(data: dict) -> ResponseEvent:
    if not isinstance(data, dict) or not isinstance(data.get("type"), str):
        raise RuntimeError("Invalid model stream event")
    return ResponseEvent(EVENT_KINDS.get(data["type"], "other"), data)


def collect_response(events: Iterable[ResponseEvent], *, allow_empty_output: bool = False) -> dict:
    """Commit one completed response or fail with no history/tool side effects.

    Some Responses servers omit output on Completed (as Codex's event type does).
    In that case use ordered OutputItemDone, never reconstruct from text deltas.
    """
    done = {}
    added = set()
    for event in events:
        data = event.data
        if event.kind == "failed":
            # Arbitrary upstream error bodies may contain sensitive material.
            response = data.get("response")
            error = response.get("error") if isinstance(response, dict) else data.get("error")
            if isinstance(error, dict):
                code = error.get("code")
                kind = error.get("type") if not code else None
                transient = {"server_is_overloaded", "server_error", "internal_server_error",
                             "service_unavailable", "service_unavailable_error", "rate_limit_exceeded"}
                if any(isinstance(value, str) and value in transient for value in (code, kind)):
                    raise RetryableModelError("Temporary model stream failure; no output committed",
                        retry_after_seconds=retry_after_seconds(error.get("headers")))
            raise RuntimeError("Model stream reported failure or incomplete response")
        if event.kind in ("output_item_added", "output_item_done"):
            index, item = data.get("output_index"), data.get("item")
            if type(index) is not int or index < 0 or not isinstance(item, dict):
                raise RuntimeError("Invalid model output item event")
            if event.kind == "output_item_added":
                added.add(index)
            else:
                if index in done:
                    raise RuntimeError("Duplicate completed model output index")
                done[index] = deepcopy(item)
        if event.kind == "completed":
            body = deepcopy(data.get("response"))
            if not isinstance(body, dict) or body.get("status", "completed") != "completed":
                raise RuntimeError("Model response was not completed")
            # The ChatGPT direct route can send an empty terminal output even
            # after full output_item.done events. This is an explicit adapter
            # capability, not permission to execute deltas or unfinished calls.
            if "output" not in body or (allow_empty_output and body.get("output") in (None, [])):
                if added - done.keys() or sorted(done) != list(range(len(done))):
                    raise RuntimeError("Model stream contains unfinished output items")
                body["output"] = [done[i] for i in range(len(done))]
            output = body["output"]
            if not isinstance(output, list) or any(not isinstance(i, dict) for i in output):
                raise RuntimeError("Model response had no valid output array")
            if any(i >= len(output) for i in added | done.keys()):
                raise RuntimeError("Model stream output index is absent from completed response")
            if any(output[i] != item for i, item in done.items()):
                raise RuntimeError("Model completed response disagrees with completed output item")
            body.setdefault("status", "completed")
            return body
    raise RetryableModelError("Model stream ended before response.completed; no tools executed")
