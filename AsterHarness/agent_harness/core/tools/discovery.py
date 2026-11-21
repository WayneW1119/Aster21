"""One deferred catalog for external MCP and local script tools."""

from dataclasses import dataclass
import json

from ..mcp.search import SEARCH_SPEC, ToolSearch
from .registry import Tool
from .result import ToolSearchResult


@dataclass(frozen=True)
class DeferredBinding:
    tools: tuple = ()

    def namespaces(self):
        return {tool.namespace: tool.loadable_spec["description"] for tool in self.tools}

    def resolve(self, namespace, name):
        return next(tool for tool in self.tools
                    if tool.namespace == namespace and tool.callable_name == name)


def append_search_tool(registry, binding, *, allow_empty=False):
    if not binding.tools and not allow_empty:
        return
    search = ToolSearch(binding)

    def handle(query, limit=8):
        return ToolSearchResult(json.dumps(search.search(query, limit), ensure_ascii=False))

    registry.register(Tool(SEARCH_SPEC, handle))
