"""Publish MCP bindings at Step boundaries, reusing unchanged exact clients.

Configuration replacement is host-owned; model calls never mutate the service
registry. Failed required replacements leave the previous binding intact.
"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from .binding import McpBinding, PreparedMcpCall
from .client import create_mcp_client, describe_error, require_sdk
from .config import parse_servers


class McpConnectionManager:
    def __init__(self, servers: dict, workspace: Path, log_root: Path, trace):
        self.sandbox = None
        self.config = parse_servers(servers)
        self.workspace, self.log_root, self.trace = workspace, log_root, trace
        self.binding = McpBinding()
        self.failures = []
        self._clients = []
        self._servers = {}
        self._catalogs = {}
        self._started = False
        self._closed = False
        self._revision = 0
        self._retiring = None

    async def start(self) -> McpBinding:
        if not any(config.enabled for config in self.config.values()):
            return self.binding  # Preserve close-drain semantics when no MCP exists.
        if self._closed:
            raise RuntimeError("MCP manager is closed")
        if not self._started:
            await self.replace_servers({name: config.to_dict() for name, config in self.config.items()})
        return self.binding

    async def replace_servers(self, servers: dict, *, require_ready=()) -> McpBinding:
        """Called only at a Session Step boundary; no in-flight batch is replaced."""
        if self._closed:
            raise RuntimeError("MCP manager is closed")
        desired = parse_servers(servers)
        enabled = [(name, config) for name, config in desired.items() if config.enabled]
        if enabled:
            require_sdk()
        created = []
        failures = []
        revision = self._revision + 1

        async def connect(name, config):
            if (self.config.get(name) == config and name in self._servers):
                return name, self._servers[name], self._catalogs[name]
            suffix = "" if not self._started else f".r{revision}"
            client = create_mcp_client(config, self.workspace, self.log_root / f"{name}{suffix}.stderr.log")
            client.sandbox = self.sandbox
            created.append(client)
            self.trace("mcp_startup", {"server": name, "status": "starting", "revision": revision,
                                       "transport": config.transport})
            try:
                tools = await client.start()
                calls = tuple(PreparedMcpCall.create(name, tool, client) for tool in tools
                    if (config.enabled_tools is None or tool["name"] in config.enabled_tools)
                    and tool["name"] not in config.disabled_tools)
                self.trace("mcp_startup", {"server": name, "status": "ready", "tool_count": len(calls),
                    "stderr_path": str(client.stderr_path) if client.stderr_path is not None else None, "revision": revision})
                return name, client, calls
            except Exception as error:
                failure = {"server": name, "error": describe_error(error),
                           "stderr_path": str(client.stderr_path) if client.stderr_path is not None else None, "required": config.required}
                failures.append(failure)
                self.trace("mcp_startup", {**failure, "status": "failed", "revision": revision})
                await client.close()
                return name, None, ()

        try:
            connected = await asyncio.gather(*(connect(name, config) for name, config in enabled))
            required = [failure["server"] for failure in failures if failure["required"] or failure["server"] in require_ready]
            if required:
                raise RuntimeError("Required MCP servers failed to start: " + ", ".join(sorted(required)))
        except BaseException:
            await asyncio.gather(*(client.close() for client in created), return_exceptions=True)
            raise
        current = {name: client for name, client, _ in connected if client is not None}
        catalogs = {name: calls for name, client, calls in connected if client is not None}
        retired = [client for name, client in self._servers.items() if current.get(name) is not client]
        self.binding = McpBinding(tuple(call for name in sorted(catalogs) for call in catalogs[name]),
                                  tuple(sorted(current.items())))
        self.config, self._servers, self._catalogs = desired, current, catalogs
        self.failures = failures
        self._revision = revision
        self._clients = list(current.values())
        self._started = True
        if self._retiring is None:
            await asyncio.gather(*(client.close() for client in retired))
        else:
            self._retiring.extend(retired)
        return self.binding

    @asynccontextmanager
    async def retain_batch_clients(self):
        """StepContext bindings stay callable through the last call in their batch."""
        retired = []
        self._retiring = retired
        try:
            yield
        finally:
            self._retiring = None
            await asyncio.gather(*(client.close() for client in retired))

    async def reset_for_sandbox(self):
        await asyncio.gather(*(client.close() for client in self._clients))
        self._clients=[];self._servers={};self._catalogs={}
        self.binding=McpBinding();self._started=False

    async def close(self) -> None:
        self._closed = True
        await asyncio.gather(*(client.close() for client in self._clients))
