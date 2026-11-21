"""Frozen tool metadata and exact client handles, modeled after Codex McpBinding."""

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import re

from .client import McpClient


def model_tool_name(server: str, tool: str) -> str:
    full = f"mcp__{server}__{tool}"
    if len(full) <= 64 and re.fullmatch(r"[A-Za-z0-9_-]+", full):
        return full
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", full)
    return safe[:51] + "_" + hashlib.sha256(full.encode()).hexdigest()[:12]


@dataclass(frozen=True)
class PreparedMcpCall:
    server: str
    name: str
    _spec: dict
    client: McpClient

    @classmethod
    def create(cls, server: str, tool: dict, client: McpClient):
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError("MCP tools must declare an object inputSchema")
        spec = {"type": "function", "name": model_tool_name(server, tool["name"]),
                "description": tool.get("description", ""), "parameters": deepcopy(schema)}
        return cls(server, tool["name"], spec, client)

    @property
    def namespace(self) -> str:
        return "mcp__" + self.server

    @property
    def callable_name(self) -> str:
        # Reuse existing normalization for unusual tool names; route by exact binding.
        name = self.spec["name"]
        prefix = self.namespace + "__"
        return name[len(prefix):] if name.startswith(prefix) else name

    @property
    def loadable_spec(self) -> dict:
        tool = self.spec
        tool.update(name=self.callable_name, strict=False, defer_loading=True)
        return {"type": "namespace", "name": self.namespace,
                "description": self.client.instructions,
                "tools": [tool]}

    @property
    def spec(self) -> dict:
        return deepcopy(self._spec)

    @property
    def search_text(self):
        spec = self.spec
        return " ".join((spec["name"], self.name, self.server, spec["description"],
                         self.client.instructions, " ".join(spec["parameters"].get("properties", {}))))

    async def call(self, arguments: dict) -> dict:
        return await self.client.call_tool(self.name, arguments)


@dataclass(frozen=True)
class McpBinding:
    tools: tuple[PreparedMcpCall, ...] = ()
    servers: tuple[tuple[str, McpClient], ...] = ()

    def namespaces(self) -> dict[str, str]:
        return {tool.namespace: tool.client.instructions for tool in self.tools}

    def resolve(self, namespace: str, name: str) -> PreparedMcpCall:
        return next(tool for tool in self.tools
                    if tool.namespace == namespace and tool.callable_name == name)
