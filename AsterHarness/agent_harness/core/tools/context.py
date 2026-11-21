"""Tool input and request-scoped execution context, inspired by Codex tools/context.

Wire items remain dictionaries for rollout compatibility. Parsing belongs to the
router so malformed arguments still produce the same ordinary error receipt.
"""

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import TYPE_CHECKING

from ..client_common import Item
from .names import normalize_namespace

if TYPE_CHECKING:
    from ..session.step_context import StepContext


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict
    call_id: str | None = None
    namespace: str | None = None

    @classmethod
    def from_item(cls, item: Item) -> "ToolCall":
        # Preserve the original parsing/error order at the protocol boundary.
        arguments = (item["arguments"] if item.get("type") == "tool_search_call"
                     else json.loads(item["arguments"]))
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be a JSON object")
        name = "tool_search" if item.get("type") == "tool_search_call" else item["name"]
        namespace = normalize_namespace(item.get("namespace"))
        return cls(name, arguments, item.get("call_id"), namespace)


@dataclass(frozen=True)
class ToolInvocation:
    """One raw call plus the exact Step and eligible compaction boundary.

    Raw input is retained until dispatch, including malformed calls. The accessor
    returns a copy; neither handlers nor observers can mutate this binding.
    """

    _call: Item
    step: "StepContext"
    compact_boundary: frozenset[str] | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "_call", deepcopy(self._call))
        if self.compact_boundary is not None:
            object.__setattr__(self, "compact_boundary", frozenset(self.compact_boundary))

    @property
    def call(self) -> Item:
        return deepcopy(self._call)
