"""Agent-owned HTTP connections; credentials are bound privately by the host."""

import json

from .registry import Tool


def register_connections(registry, capabilities):
    async def connect(name, url, credential_ref=None):
        return json.dumps(await capabilities.connect(name, url, credential_ref))

    async def disconnect(name):
        return json.dumps(await capabilities.disconnect(name))

    def list_connections():
        owned = capabilities.state["connections"]
        return json.dumps([{"name": name, "owner": "agent" if name in owned else "host"}
                           for name in capabilities.session.services.mcp.config])

    fields = {"name": "Unique connection namespace (ASCII letters, digits, hyphens, underscores).",
              "url": "Full HTTP MCP endpoint without embedded credentials or query.",
              "credential_ref": "Host credential alias or absolute JSON file path. Pass the reference directly; printing the file exposes the token in history."}
    for name, description, handler, required, optional in (
        ("connect_mcp", "Connect a named HTTP MCP endpoint using an optional host credential reference.", connect, ["name", "url"], ["credential_ref"]),
        ("disconnect_mcp", "Remove an agent-owned MCP namespace.", disconnect, ["name"], []),
        ("list_mcp_connections", "List configured MCP namespaces and their owners.", list_connections, [], []),
    ):
        registry.register(Tool({"type": "function", "name": name, "description": description,
            "strict": False, "parameters": {"type": "object", "required": required,
            "properties": {key: {"type": "string", "description": fields[key]} for key in required + optional},
            "additionalProperties": False}}, handler))
