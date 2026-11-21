from copy import deepcopy
import asyncio
import inspect
from collections.abc import Awaitable
from dataclasses import dataclass
import re
from typing import Any, Callable

from ..client_common import Item
from .result import ToolResult
from .names import DEFAULT_FUNCTION_NAMESPACE, normalize_namespace


@dataclass
class Tool:
    spec: Item
    handler: Callable[..., str | ToolResult | Awaitable[str | ToolResult]]
    # Only short, event-loop-affine callbacks may opt in. Blocking tools use a thread.
    run_inline: bool = False
    deferred: bool = False


@dataclass(frozen=True)
class DeferredToolDefinition:
    namespace: str
    _tool: Tool
    _description: str

    @property
    def name(self):
        return self._tool.spec["name"]

    @property
    def callable_name(self):
        return self.name

    @property
    def spec(self):
        return deepcopy(self._tool.spec)

    @property
    def loadable_spec(self):
        tool = self.spec
        return {"type": "namespace", "name": self.namespace,
                "description": self._description, "tools": [tool]}

    @property
    def search_text(self):
        spec = self.spec
        return " ".join((self.namespace, self.name, spec.get("description", ""), self._description,
                         " ".join(spec.get("parameters", {}).get("properties", {}))))


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._namespaces: dict[str, dict] = {}
        self._aliases: dict[str, tuple[str, str]] = {}
        self._read_only = False

    @staticmethod
    def _copy_tool(tool):
        return Tool(deepcopy(tool.spec), tool.handler, tool.run_inline, tool.deferred)

    def register(self, tool: Tool, *, namespace: str | None = None,
                 namespace_description: str | None = None) -> None:
        if self._read_only:
            raise ValueError("Cannot register tools in a Step snapshot")
        if namespace is not None:
            namespace = normalize_namespace(namespace)
            description = (self._namespaces.get(namespace, {}).get("description", "")
                           if namespace_description is None else namespace_description)
            self.register_namespace(namespace, description, [tool])
            return
        name = tool.spec["name"]
        if (name in self._tools or name in self._aliases
                or name in self._namespaces.get(DEFAULT_FUNCTION_NAMESPACE, {}).get("tools", {})):
            raise ValueError(f"Duplicate tool: {name}")
        self._tools[name] = self._copy_tool(tool)

    def register_namespace(self, name: str, description: str, tools=()) -> None:
        """Register one group atomically; identities are (namespace, name), not strings.

        Repeated additions to a group must agree on its description. Change group
        metadata by building a replacement registry, then publish at a Step boundary.
        """
        if self._read_only:
            raise ValueError("Cannot register tools in a Step snapshot")
        name = normalize_namespace(name)
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise ValueError("Namespace must contain 1-64 ASCII letters, digits, _ or -")
        if not isinstance(description, str):
            raise ValueError("Namespace description must be a string")
        previous = self._namespaces.get(name)
        if previous is not None and previous["description"] != description:
            raise ValueError(f"Conflicting namespace description: {name}")
        additions = {}
        for tool in tools:
            tool_name = tool.spec.get("name")
            if (tool.spec.get("type") != "function" or not isinstance(tool_name, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool_name)):
                raise ValueError("Namespace members must be function tools with valid 1-64 character names")
            if (tool_name in additions or previous is not None and tool_name in previous["tools"]
                    or name == DEFAULT_FUNCTION_NAMESPACE and (tool_name in self._tools or tool_name in self._aliases)):
                raise ValueError(f"Duplicate tool: {name}.{tool_name}")
            additions[tool_name] = self._copy_tool(tool)
        if previous is None:
            self._namespaces[name] = {"description": description, "tools": additions}
        else:
            previous["tools"].update(additions)

    def register_alias(self, alias: str, namespace: str, name: str) -> None:
        """Explicit compatibility route, never exposed as another model tool."""
        if self._read_only:
            raise ValueError("Cannot register tools in a Step snapshot")
        self.get_tool(name, namespace)
        if (alias in self._tools or alias in self._aliases
                or alias in self._namespaces.get(DEFAULT_FUNCTION_NAMESPACE, {}).get("tools", {})):
            raise ValueError(f"Duplicate tool alias: {alias}")
        self._aliases[alias] = (normalize_namespace(namespace), name)

    def has_legacy_alias(self, name: str) -> bool:
        return name in self._aliases

    def get_tool(self, name: str, namespace: str | None = None) -> Tool:
        namespace = normalize_namespace(namespace)
        if namespace != DEFAULT_FUNCTION_NAMESPACE:
            return self._namespaces[namespace]["tools"][name]
        if name in self._tools:
            return self._tools[name]
        if name in self._aliases:
            group, member = self._aliases[name]
            return self.get_tool(member, group)
        return self._namespaces.get(DEFAULT_FUNCTION_NAMESPACE, {}).get("tools", {})[name]

    def loadable_spec(self, name: str, namespace: str) -> Item:
        namespace = normalize_namespace(namespace)
        tool = deepcopy(self.get_tool(name, namespace).spec)
        return {"type": "namespace", "name": namespace,
                "description": self._namespaces.get(namespace, {}).get("description", ""), "tools": [tool]}

    def deferred_definitions(self):
        flat = [DeferredToolDefinition(DEFAULT_FUNCTION_NAMESPACE, self._copy_tool(tool),
                    self._namespaces.get(DEFAULT_FUNCTION_NAMESPACE, {}).get("description", ""))
                for tool in self._tools.values() if tool.deferred]
        return tuple(flat + [DeferredToolDefinition(namespace, self._copy_tool(tool), group["description"])
            for namespace, group in self._namespaces.items()
            for tool in group["tools"].values() if tool.deferred])

    def namespace_description(self, name: str) -> str:
        return self._namespaces[name]["description"]

    def all_definitions(self) -> list[dict]:
        """Detached metadata for host composition; handlers and aliases stay private."""
        return ([{"namespace": DEFAULT_FUNCTION_NAMESPACE, "spec": deepcopy(tool.spec), "deferred": tool.deferred}
                 for tool in self._tools.values()] +
                [{"namespace": name, "spec": deepcopy(tool.spec), "deferred": tool.deferred}
                 for name, group in self._namespaces.items() for tool in group["tools"].values()])

    def specs(self) -> list[Item]:
        specs = deepcopy([tool.spec for tool in self._tools.values() if not tool.deferred])
        for spec in specs:
            if spec.get("type") == "tool_search":
                spec.pop("name", None)  # Internal routing key is not a wire field.
        for name, group in self._namespaces.items():
            tools = [deepcopy(tool.spec) for tool in group["tools"].values() if not tool.deferred]
            if tools:
                specs.append({"type": "namespace", "name": name,
                              "description": group["description"], "tools": tools})
        return specs

    def copy(self) -> "ToolRegistry":
        registry = ToolRegistry()
        registry._tools = {name: self._copy_tool(tool) for name, tool in self._tools.items()}
        registry._namespaces = {name: {"description": group["description"], "tools": {
            member: self._copy_tool(tool) for member, tool in group["tools"].items()}}
            for name, group in self._namespaces.items()}
        registry._aliases = self._aliases.copy()
        return registry

    def snapshot(self) -> "ToolRegistry":
        """Detach definitions and handler bindings, but share their runtime services."""
        registry = self.copy()
        registry._read_only = True
        return registry

    def execute(self, name: str, arguments: dict[str, Any], *, namespace: str | None = None) -> str | ToolResult:
        tool = self.get_tool(name, namespace)
        if inspect.iscoroutinefunction(tool.handler):
            raise ValueError("This tool requires asynchronous dispatch")
        return tool.handler(**arguments)

    async def execute_async(self, name: str, arguments: dict[str, Any], *, namespace: str | None = None) -> str | ToolResult:
        tool = self.get_tool(name, namespace)
        if inspect.iscoroutinefunction(tool.handler):
            return await tool.handler(**arguments)
        if tool.run_inline:
            return self.execute(name, arguments, namespace=namespace)
        return await asyncio.to_thread(self.execute, name, arguments, namespace=namespace)
