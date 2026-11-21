"""Official Python SDK adapter, analogous to Codex's rmcp-client boundary.

Each connection has an owner task. SDK async contexts (AnyIO cancel scopes) are
entered and exited by that same task, independent of which Step calls the tool.
No request replay, automatic reconnect, sampling or elicitation callbacks.
"""

import asyncio
from copy import deepcopy
from contextlib import asynccontextmanager
from datetime import timedelta
import os
from pathlib import Path

from .config import McpServerConfig


class McpCallError(Exception):
    def __init__(self, message: str, *, outcome_unknown: bool):
        super().__init__(message)
        self.outcome_unknown = outcome_unknown


def describe_error(error: BaseException) -> str:
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return f"{type(error).__name__}: {error}"[:1000]


def require_sdk():
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError:
        raise RuntimeError('MCP requires the optional SDK: python -m pip install ".[mcp]"') from None
    return ClientSession, StdioServerParameters, stdio_client


class McpClient:
    def __init__(self, config: McpServerConfig, workspace: Path, stderr_path: Path | None):
        self.config, self.workspace, self.stderr_path = config, workspace, stderr_path
        self._task = None
        self._ready = None
        self._queue = asyncio.Queue()
        self._active = None
        self._closed = False
        self.failure = None
        self.sandbox = None
        self.instructions = ""
        self.has_resources = False

    def _transport(self):
        raise NotImplementedError

    def _describe_error(self, error):
        return describe_error(error)

    async def start(self) -> list[dict]:
        if self._closed:
            raise RuntimeError("MCP client is closed")
        if self._task is None:
            self._ready = asyncio.get_running_loop().create_future()
            self._task = asyncio.create_task(self._run())
        return await self._ready

    async def request(self, method: str, arguments: dict) -> dict:
        if self._closed or self._task is None or self._task.done():
            raise McpCallError("MCP connection is unavailable", outcome_unknown=False)
        future = asyncio.get_running_loop().create_future()
        self._queue.put_nowait((method, arguments, future))
        return await future

    async def call_tool(self, name: str, arguments: dict) -> dict:
        return await self.request("tools/call", {"name": name, "arguments": arguments})

    async def _discover(self, session) -> list[dict]:
        tools, names, cursors, cursor = [], set(), set(), None
        for _ in range(100):
            page = await session.list_tools(cursor=cursor)
            for tool in page.tools:
                if tool.name in names or not tool.name:
                    raise ValueError("Duplicate or empty MCP tool name")
                names.add(tool.name)
                tools.append(tool.model_dump(by_alias=True, exclude_none=True))
                if len(tools) > 1000:
                    raise ValueError("MCP tool catalog exceeds 1000 tools")
            cursor = page.nextCursor
            if not cursor:
                return sorted(tools, key=lambda tool: tool["name"])
            if cursor in cursors:
                raise ValueError("MCP tool pagination repeated a cursor")
            cursors.add(cursor)
        raise ValueError("MCP tool catalog exceeds 100 pages")

    async def _run(self) -> None:
        try:
            ClientSession, _, _ = require_sdk()
            config = self.config
            async with self._transport() as (read, write):
                async with ClientSession(read, write) as session:
                    async with asyncio.timeout(config.startup_timeout_sec):
                        initialized = await session.initialize()
                        self.instructions = initialized.instructions or ""
                        self.has_resources = initialized.capabilities.resources is not None
                        tools = (await self._discover(session)
                                 if initialized.capabilities.tools is not None else [])
                    if not self._ready.done():
                        self._ready.set_result(tools)
                    while True:
                        request = await self._queue.get()
                        if request is None:
                            break
                        method, arguments, future = request
                        if future.cancelled():
                            continue
                        self._active = future
                        try:
                            async with asyncio.timeout(config.tool_timeout_sec):
                                if method == "tools/call":
                                    result = await session.call_tool(arguments["name"], arguments["arguments"],
                                        read_timeout_seconds=timedelta(seconds=config.tool_timeout_sec))
                                elif method == "resources/list":
                                    result = await session.list_resources(cursor=arguments.get("cursor"))
                                elif method == "resources/templates/list":
                                    result = await session.list_resource_templates(cursor=arguments.get("cursor"))
                                elif method == "resources/read":
                                    from pydantic import AnyUrl
                                    result = await session.read_resource(AnyUrl(arguments["uri"]))
                                else:
                                    raise ValueError("Unknown MCP operation")
                            if not future.done():
                                future.set_result(result.model_dump(mode="json", by_alias=True, exclude_none=True))
                        except asyncio.CancelledError:
                            if not future.done():
                                future.set_exception(McpCallError("MCP connection closed during call",
                                                                   outcome_unknown=True))
                            raise
                        except Exception as error:
                            if not future.done():
                                future.set_exception(McpCallError(self._describe_error(error),
                                                                  outcome_unknown=True))
                        finally:
                            self._active = None
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.failure = self._describe_error(error)
        finally:
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(RuntimeError(self.failure or "MCP startup cancelled"))
            if self._active is not None and not self._active.done():
                self._active.set_exception(McpCallError("MCP connection closed during call",
                                                       outcome_unknown=True))
            while not self._queue.empty():
                request = self._queue.get_nowait()
                if request is not None and not request[2].done():
                    request[2].set_exception(McpCallError("MCP connection closed before dispatch",
                                                          outcome_unknown=False))

    async def close(self) -> None:
        self._closed = True
        if self._task is not None:
            self._queue.put_nowait(None)
            if (self._active is not None or self._ready is None
                    or not self._ready.done() or self._ready.cancelled()):
                self._task.cancel()
            if self.config.url is not None:
                done, _ = await asyncio.wait([self._task], timeout=5)
                if not done:
                    self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


class StdioMcpClient(McpClient):
    @asynccontextmanager
    async def _transport(self):
        _, Parameters, stdio_client = require_sdk()
        config = self.config
        env = {name: os.environ[name] for name in config.env_vars if name in os.environ}
        # The SDK supplies a minimal process environment, not all host credentials.
        env.pop("HARNESS_GENERATOR_SOCKET", None)
        env.pop("HARNESS_GENERATOR_FD", None)
        env.pop("HARNESS_GENERATOR_STATE_DIR", None)
        env.pop("HARNESS_GENERATOR_TOKEN", None)
        cwd = (self.workspace / (config.cwd or ".")).resolve(strict=True)
        self.stderr_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.stderr_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        command, args = config.command, list(config.args)
        with os.fdopen(descriptor, "w", encoding="utf-8") as log:
            from contextlib import ExitStack
            with ExitStack() as launch_scope:
                if self.sandbox is not None and self.sandbox.enabled:
                    argv, env = launch_scope.enter_context(self.sandbox.execution(
                        [command, *args], cwd=cwd, env=env, explicit_env_keys=config.env_vars,
                    ))
                    command, args = argv[0], argv[1:]
                async with stdio_client(Parameters(command=command, args=args,
                        cwd=str(cwd), env=env), errlog=log) as streams:
                    yield streams


class StreamableHttpMcpClient(McpClient):
    def __init__(self, config, workspace, stderr_path=None):
        super().__init__(config, workspace, None)
        self._headers = {}

    @asynccontextmanager
    async def _transport(self):
        import httpx
        from mcp.client.streamable_http import streamable_http_client

        self._headers = self.config.resolve_http_headers()
        if self.config.credential_file:
            self._headers["authorization"] = "Bearer " + await asyncio.to_thread(self._credential_file_token)
        timeout = httpx.Timeout(30, read=max(300, self.config.startup_timeout_sec,
                                           self.config.tool_timeout_sec))
        # Enforce this at the response boundary too: MCP SDK versions may
        # explicitly follow redirects even when the client's default is false.
        async def reject_redirect(response):
            if response.is_redirect:
                response.raise_for_status()

        async with httpx.AsyncClient(headers=self._headers, timeout=timeout,
                                     follow_redirects=False,
                                     event_hooks={"response": [reject_redirect]}) as client:
            async with streamable_http_client(self.config.url, http_client=client,
                                               terminate_on_close=True) as (read, write, _):
                yield read, write

    def _credential_file_token(self):
        import base64
        import json
        from .config import validate_header_value
        path = Path(self.config.credential_file)
        try:
            if self.sandbox is not None and self.sandbox.enabled:
                raw = base64.b64decode(self.sandbox.file_operation("bytes", path=str(path), limit=16384))
            else:
                with path.open("rb") as stream:
                    raw = stream.read(16385)
            if len(raw) > 16384:
                raise ValueError
            value = json.loads(raw)
            if value["mcp_url"] != self.config.url or not value["token"]:
                raise ValueError
            validate_header_value(value["token"])
            return value["token"]
        except (ValueError, OSError, KeyError, TypeError):
            raise ValueError("Invalid or inaccessible MCP credential file for this endpoint") from None

    def _describe_error(self, error):
        import httpx

        while isinstance(error, BaseExceptionGroup) and error.exceptions:
            error = error.exceptions[0]
        if isinstance(error, httpx.HTTPStatusError):
            return f"MCP HTTP endpoint returned status {error.response.status_code}"
        if isinstance(error, httpx.TimeoutException):
            return "MCP HTTP transport timed out"
        if isinstance(error, httpx.RequestError):
            return f"MCP HTTP transport failed ({type(error).__name__})"
        message = f"{type(error).__name__}: {error}"
        # A remote error may echo request data. Never put resolved credentials in traces.
        for value in self._headers.values():
            if value:
                message = message.replace(value, "[REDACTED]")
                if value.startswith("Bearer ") and value[7:]:
                    message = message.replace(value[7:], "[REDACTED]")
        if self.config.url:
            message = message.replace(self.config.url, "[MCP endpoint]")
        return message[:1000]


class OnDemandMcpClient(McpClient):
    """Explicit opt-in for stateless stdio tools; no idle child or call replay.

    Discovery stays cached in the binding. Each operation owns one SDK session,
    validates the same catalog, then closes it, including on failure/cancellation.
    """

    def __init__(self, config, workspace, stderr_path):
        super().__init__(config, workspace, stderr_path)
        self._lock = asyncio.Lock()
        self._catalog = None
        self._current = None

    @asynccontextmanager
    async def _connection(self):
        if self._closed:
            raise McpCallError("MCP client is closed", outcome_unknown=False)
        client = StdioMcpClient(self.config, self.workspace, self.stderr_path)
        client.sandbox = self.sandbox
        self._current = client
        try:
            try:
                tools = await client.start()
            except Exception as error:
                raise McpCallError(describe_error(error), outcome_unknown=False) from error
            if self._catalog is None:
                self._catalog = deepcopy(tools)
                self.instructions = client.instructions
                self.has_resources = client.has_resources
            elif (tools != self._catalog or client.instructions != self.instructions
                  or client.has_resources != self.has_resources):
                raise McpCallError("MCP catalog changed; reload server configuration before calling",
                                   outcome_unknown=False)
            yield client
        finally:
            await client.close()
            self._current = None

    async def start(self) -> list[dict]:
        async with self._lock:
            if self._closed:
                raise RuntimeError("MCP client is closed")
            if self._catalog is None:
                async with self._connection():
                    pass
            return deepcopy(self._catalog)

    async def request(self, method: str, arguments: dict) -> dict:
        async with self._lock:
            if self._catalog is None:
                raise McpCallError("MCP client has not been initialized", outcome_unknown=False)
            async with self._connection() as client:
                return await client.request(method, arguments)

    async def close(self) -> None:
        self._closed = True
        if self._current is not None:
            await self._current.close()
        async with self._lock:
            pass  # Wait until the operation has released its transport resources.


def create_mcp_client(config, workspace, stderr_path):
    if config.connection_mode == "on_demand":
        return OnDemandMcpClient(config, workspace, stderr_path)
    client_type = StreamableHttpMcpClient if config.url is not None else StdioMcpClient
    return client_type(config, workspace, stderr_path)
