from ..client_common import Item
from .context import ToolCall
from .registry import ToolRegistry
from .names import normalize_namespace, is_default_namespace
from .result import ToolResult, ToolSearchResult


class ToolRouter:
    def __init__(self, registry: ToolRegistry, *, mcp_binding=None, loaded_mcp=None) -> None:
        self.registry = registry
        self.mcp_binding = mcp_binding
        self.loaded_mcp = loaded_mcp or {}

    def _validate_namespace(self, call):
        namespace = normalize_namespace(call.get("namespace"))
        name = 'tool_search' if call.get('type') == 'tool_search_call' else call.get('name')
        if is_default_namespace(namespace) and isinstance(name, str) and name.startswith("local_"):
            raise ValueError("Custom tools require their namespace and a loaded definition; use tool_search")
        # Explicit historical MCP aliases keep their old default-namespace route.
        if is_default_namespace(namespace) and self.registry.has_legacy_alias(name):
            return
        try:
            registered = self.registry.get_tool(name, namespace)
        except KeyError:
            registered = None
        if registered is not None and not registered.deferred:
            return  # A directly advertised native namespace is not an MCP service.
        if registered is None:
            if is_default_namespace(namespace):
                return  # Let dispatch retain the normal missing-tool error.
            raise ValueError("Deferred tool is no longer available; use tool_search to discover current tools")
        advertised = self.loaded_mcp.get((namespace, name))
        current = self.registry.loadable_spec(name, namespace)
        if advertised != current["tools"][0]:
            raise ValueError("Deferred definition is missing or changed since loading; use tool_search before calling it")

    @staticmethod
    def _result(output: str | ToolResult) -> ToolResult:
        if isinstance(output, ToolResult):
            return output
        return ToolResult(output)

    def invoke(self, call: Item) -> ToolResult:
        try:
            self._validate_namespace(call)
            parsed = ToolCall.from_item(call)
            return self._result(self.registry.execute(parsed.name, parsed.arguments, namespace=parsed.namespace))
        except (KeyError, ValueError, TypeError, OSError) as error:
            # Expected tool errors are evidence for the model, not a crashed loop.
            if call.get("type") == "tool_search_call":
                return ToolSearchResult("[]")
            return ToolResult.from_error(error)

    async def invoke_async(self, call: Item) -> ToolResult:
        try:
            self._validate_namespace(call)
            parsed = ToolCall.from_item(call)
            return self._result(await self.registry.execute_async(parsed.name, parsed.arguments, namespace=parsed.namespace))
        except (KeyError, ValueError, TypeError, OSError) as error:
            if call.get("type") == "tool_search_call":
                return ToolSearchResult("[]")
            return ToolResult.from_error(error)

    def dispatch(self, call: Item) -> Item:
        """Compatibility helper for callers that only need the protocol item."""
        return self.invoke(call).to_item(call["call_id"])

    async def dispatch_async(self, call: Item) -> Item:
        return (await self.invoke_async(call)).to_item(call["call_id"])
