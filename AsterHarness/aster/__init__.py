"""Aster public Python SDK. Legacy agent_harness imports remain supported."""

import agent_harness as _runtime
from typing import TYPE_CHECKING

__all__ = _runtime.__all__


def __getattr__(name):
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(_runtime, name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))


if TYPE_CHECKING:
    from agent_harness import (
        Aster,
        Agent,
        Harness,
        ModelConfig,
        Message,
        CallbackError,
        MessageStreamError,
        SessionConfiguration,
        ResponsesClient,
        ScriptedClient,
        ModelClient,
        ModelResponse,
        Prompt,
        ResponseEvent,
        RetryableModelError,
    )
