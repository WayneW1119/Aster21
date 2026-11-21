"""Read exact pages from a full output retained outside model context."""

from .registry import Tool


def register_output_tools(registry, store, get_limit):
    def read_tool_output(output_id, offset=0, max_chars=8000):
        return store.read(output_id, offset, max_chars, get_limit())

    registry.register(Tool({"type": "function", "name": "read_tool_output", "strict": False,
        "description": "Read an exact page of a full tool output saved when the unified model-facing "
            "text budget truncated it. Ordinary business text defaults to approximately 10000 tokens "
            "(UTF-8 bytes / 4); images have separate limits and complete tool schemas/memory version "
            "evidence must remain structurally valid. A truncation receipt gives output_id. Saved "
            "outputs belong to this Thread and survive clean resume and compact. Pages are valid JSON "
            "with exact content and next_offset; repeat using next_offset without guessing positions. "
            "Reading archived schemas as text does not activate them; use tool_search for loading.",
        "parameters": {"type": "object", "properties": {
            "output_id": {"type": "string", "description": "Saved output_id from a truncation receipt or runtime notice."},
            "offset": {"type": "integer", "description": "Zero-based Unicode character offset. Defaults to 0."},
            "max_chars": {"type": "integer", "description": "Requested characters, 1-100000; reduced as needed to fit the output budget. Defaults to 8000."}},
            "required": ["output_id"], "additionalProperties": False}}, read_tool_output))
