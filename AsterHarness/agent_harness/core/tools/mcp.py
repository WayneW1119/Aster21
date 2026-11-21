"""Convert a captured MCP catalog into ordinary model tools and string receipts."""

import json
import asyncio

from ..mcp.binding import McpBinding
from ..mcp.client import McpCallError
from .registry import Tool, ToolRegistry
from .result import ToolResult
from .discovery import append_search_tool


def append_mcp_tools(registry: ToolRegistry, binding: McpBinding, *, include_search=True, images=None) -> None:
    for prepared in binding.tools:
        def bind(call):
            async def handle(**arguments):
                try:
                    result = await call.call(arguments)
                except McpCallError as error:
                    result = {"isError": True, "error": {
                        "type": "McpCallError", "message": str(error),
                        "outcome_unknown": error.outcome_unknown,
                        "note": "No automatic retry. Verify side effects before repeating the action."}}
                if images is not None and images.has_mcp_images(result):
                    projected, parts, _ = await asyncio.to_thread(images.mcp_output, result)
                    return ToolResult([{"type": "input_text", "text": json.dumps(projected, ensure_ascii=False)}, *parts],
                                      raw_output=json.dumps(result, ensure_ascii=False))
                return json.dumps(result, ensure_ascii=False)
            return handle
        alias = prepared.spec["name"]
        spec = prepared.loadable_spec["tools"][0]
        registry.register(Tool(spec, bind(prepared), deferred=True), namespace=prepared.namespace,
                          namespace_description=prepared.loadable_spec["description"])
        registry.register_alias(alias, prepared.namespace, prepared.callable_name)

    if include_search:
        append_search_tool(registry, binding)
    if binding.servers:
        from .mcp_resources import append_resource_tools
        append_resource_tools(registry, binding, images=images)
