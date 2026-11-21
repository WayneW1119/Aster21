"""Aster's model boundary. History records retain their published storage format."""

from dataclasses import dataclass
from typing import Protocol

from .client_common import ModelResponse, Prompt


# These names belong to the existing history format, including old checkpoints.
_RECORD_KINDS = {
    "message": "message",
    "function_call": "tool_call",
    "function_call_output": "tool_result",
    "tool_search_call": "tool_discovery",
    "tool_search_output": "discovery_result",
    "reasoning": "reasoning",
    "additional_tools": "tool_definitions",
}
_RECORD_TYPES = {kind: name for name, kind in _RECORD_KINDS.items()}


@dataclass(frozen=True)
class ModelItem:
    kind: str
    fields: dict
    record_type: str | None = None
    type_position: int = 0

    @classmethod
    def from_record(cls, record):
        name = record.get("type")
        fields = {key: value for key, value in record.items() if key != "type"}
        position = list(record).index("type") if "type" in record else -1
        if name in _RECORD_KINDS:
            return cls(_RECORD_KINDS[name], fields, type_position=position)
        return cls("extension", fields, name, position)

    def to_record(self):
        name = self.record_type if self.kind == "extension" else _RECORD_TYPES[self.kind]
        entries = list(self.fields.items())
        if self.type_position >= 0:
            entries.insert(self.type_position, ("type", name))
        return dict(entries)


@dataclass
class ModelRequest:
    instructions: str
    history: list[ModelItem]
    tools: list[dict]
    memory: ModelItem | None
    cache_session_id: str | None
    history_labels: list[str] | None = None

    @classmethod
    def from_prompt(cls, prompt):
        return cls(prompt.base_instructions, [ModelItem.from_record(item) for item in prompt.input],
                   prompt.tools, None if prompt.memory is None else ModelItem.from_record(prompt.memory),
                   prompt.cache_session_id, prompt.history_labels)

    def to_prompt(self):
        return Prompt(self.instructions, [item.to_record() for item in self.history], self.tools,
                      history_labels=self.history_labels, memory=self.memory.to_record() if self.memory is not None else None,
                      cache_session_id=self.cache_session_id)


@dataclass
class ModelReply:
    items: list[ModelItem]
    usage: dict | None

    @classmethod
    def from_response(cls, response):
        return cls([ModelItem.from_record(item) for item in response.output], response.usage)

    def to_response(self):
        return ModelResponse([item.to_record() for item in self.items], self.usage)


class ModelAdapter(Protocol):
    def generate(self, request: ModelRequest) -> ModelReply: ...


def invoke(client, prompt):
    """Third-party and scripted clients may still implement the original respond API."""
    generate = getattr(client, "generate", None)
    if generate is None:
        return ModelReply.from_response(client.respond(prompt))
    return generate(ModelRequest.from_prompt(prompt))
