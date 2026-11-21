"""Internal execution outcome; scheduling metadata is never sent to the API."""

from dataclasses import dataclass
from copy import deepcopy
import json

from ..client_common import Item


@dataclass(frozen=True)
class ToolResult:
    output: str | list[dict]
    wake_model: bool = True
    raw_output: str | None = None
    protected_fields: dict | None = None

    def __post_init__(self) -> None:
        if (not isinstance(self.output, (str, list)) or type(self.wake_model) is not bool
                or self.raw_output is not None and not isinstance(self.raw_output, str)):
            raise TypeError("ToolResult requires string/content-parts output and boolean wake_model")
        if self.protected_fields is not None:
            if (not isinstance(self.protected_fields, dict)
                    or set(self.protected_fields) & {"tool_output_budget", "preview"}):
                raise TypeError("Invalid protected tool metadata")
            json.dumps(self.protected_fields, allow_nan=False)
        if isinstance(self.output, list) and any(not isinstance(part, dict) or part.get("type") not in
                {"input_text", "input_image"} for part in self.output):
            raise TypeError("ToolResult content parts must be input_text or input_image")
        if isinstance(self.output, list) and any(not isinstance(part.get(
                "text" if part["type"] == "input_text" else "image_url"), str) for part in self.output):
            raise TypeError("ToolResult content parts require text or image_url strings")

    def to_item(self, call_id: str) -> Item:
        return {"type": "function_call_output", "call_id": call_id,
                "output": deepcopy(self.output)}

    @classmethod
    def from_error(cls, error: Exception) -> "ToolResult":
        """Expected tool errors always wake; runtime failures still propagate."""
        return cls(json.dumps({"error": type(error).__name__, "message": str(error)},
                              ensure_ascii=False))


@dataclass(frozen=True)
class ToolSearchResult(ToolResult):
    def to_item(self, call_id: str) -> Item:
        return {"type": "tool_search_output", "call_id": call_id,
                "status": "completed", "execution": "client", "tools": json.loads(self.output)}
