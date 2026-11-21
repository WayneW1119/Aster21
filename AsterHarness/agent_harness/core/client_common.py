"""Corresponds conceptually to codex-rs/core/src/client_common.rs."""

from dataclasses import dataclass
import math
from typing import Any, Protocol

Item = dict[str, Any]


class RetryableModelError(RuntimeError):
    """Transient failure before any model output has been committed or executed."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def retry_after_seconds(headers) -> float | None:
    """Read bounded numeric retry hints without exposing upstream error bodies."""
    if not hasattr(headers, 'get'):
        return None
    for names, scale in ((('Retry-After', 'retry-after'), 1),
                         (('Retry-After-Ms', 'retry-after-ms'), .001)):
        value = headers.get(names[0], headers.get(names[1]))
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            continue
        try:
            seconds = float(value) * scale
        except (ValueError, OverflowError):
            continue
        if math.isfinite(seconds) and seconds >= 0:
            return min(seconds, 3600)
    return None


@dataclass
class ResponseEvent:
    """Model transport event, NOT a scheduler HarnessEvent (Codex client_common)."""

    kind: str
    data: Item


@dataclass
class Prompt:
    base_instructions: str
    input: list[Item]
    tools: list[Item]
    # Deprecated compatibility slot; transports no longer render inline labels.
    history_labels: list[str] | None = None
    # Pinned memory is context state, not a numbered/compactable history item.
    memory: Item | None = None
    # Transport hint only: stable Thread identity, never rendered as instructions.
    cache_session_id: str | None = None


@dataclass
class ModelResponse:
    output: list[Item]
    usage: Item | None = None


class ModelClient(Protocol):
    """A client may expose for_step() to capture mutable transport settings.

    Without it, the selected instance is shared and its configuration must remain
    stable during a request. Stateful clients need not copy response iterators.
    """

    def respond(self, prompt: Prompt) -> ModelResponse: ...
