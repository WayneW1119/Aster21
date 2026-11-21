"""Aster: a persistent, event-driven Agent runtime and Python SDK.

Public exports are lazy so standalone event generators do not load the runtime.
"""

from importlib import import_module
from typing import TYPE_CHECKING

__all__ = [
    "Aster",
    "Agent",
    "Harness",
    "ModelConfig",
    "Message",
    "CallbackError",
    "MessageStreamError",
    "SessionConfiguration",
    "ResponsesClient",
    "ScriptedClient",
    "ModelClient",
    "ModelResponse",
    "Prompt",
    "ResponseEvent",
    "RetryableModelError",
]

_EXPORTS = {
    **{
        name: ".sdk"
        for name in (
            "Aster",
            "Agent",
            "Harness",
            "ModelConfig",
            "Message",
            "CallbackError",
            "MessageStreamError",
        )
    },
    "SessionConfiguration": ".core.session",
    **{name: ".core.client" for name in ("ResponsesClient", "ScriptedClient")},
    **{
        name: ".core.client_common"
        for name in ("ModelClient", "ModelResponse", "Prompt", "ResponseEvent", "RetryableModelError")
    },
}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(_EXPORTS[name], __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))


if TYPE_CHECKING:
    from .sdk import (
        Aster,
        Agent,
        Harness,
        ModelConfig,
        Message,
        CallbackError,
        MessageStreamError,
    )
    from .core.session import SessionConfiguration
    from .core.client import ResponsesClient, ScriptedClient
    from .core.client_common import ModelClient, ModelResponse, Prompt, ResponseEvent, RetryableModelError
