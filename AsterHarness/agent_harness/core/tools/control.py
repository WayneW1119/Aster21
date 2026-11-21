"""Control tools use the same call/result protocol as workspace tools."""

import json
from typing import Callable

from .registry import Tool, ToolRegistry
from .result import ToolResult


def register_control_tools(registry: ToolRegistry, send_message: Callable[[str], None]) -> None:
    def send_user_message(text: str, wake_model: bool = True) -> ToolResult:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("send_user_message.text must be a non-empty string")
        if type(wake_model) is not bool:
            raise ValueError("send_user_message.wake_model must be a boolean")
        send_message(text)
        return ToolResult(json.dumps({"status": "accepted"}), wake_model=wake_model)

    def wait_for_event() -> ToolResult:
        return ToolResult(json.dumps({"status": "accepted"}), wake_model=False)

    registry.register(Tool({
        "type": "function", "name": "send_user_message",
        "description": "Send a message to the user in the terminal: a progress update, question, "
                       "or answer. This tool writes to the terminal; ordinary assistant text "
                       "is internal and is NOT displayed there. This is the basic built-in "
                       "communication channel; use the corresponding tools for other connected channels. "
                       "By default, success queues an accepted "
                       "tool-result event so you can decide what to do next. Set wake_model=false "
                       "to record the receipt without waking yourself; errors still produce events. "
                       "Sending does not finish work or end the Session. May accompany other calls, but do not "
                       "claim their results before receiving them in a later step.",
        # wake_model is optional; strict schemas require every property to be required.
        "strict": False,
        "parameters": {"type": "object", "properties": {"text": {"type": "string",
                       "description": "Nonempty text to display to the user: a useful update, question, finding or result."},
                       "wake_model": {"type": "boolean", "default": True,
                       "description": "Whether a successful send queues a tool-result event for a later step. Defaults to true. False only suppresses this send's wake-up event; it does not discard pending events or suppress errors."}},
                       "required": ["text"], "additionalProperties": False},
    }, send_user_message, run_inline=True))

    registry.register(Tool({
        "type": "function", "name": "wait_for_event",
        "description": "Wait for future input when no useful action is ready. Records a tool "
                       "receipt in history without queuing a wake-up event, sending a user message "
                       "or blocking the scheduler. Does not finish a task, clear pending events, "
                       "cancel sources or close the Session. Already queued events are still processed. "
                       "Use when no other action is needed in this response; arrange a notification "
                       "first if you need an external result to wake you.",
        "strict": True,
        "parameters": {"type": "object", "properties": {}, "required": [],
                       "additionalProperties": False},
    }, wait_for_event, run_inline=True))
