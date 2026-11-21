"""Codex-style stdio and Streamable HTTP MCP configuration.

Environment-backed credentials are resolved only when opening a connection.
"""

from dataclasses import asdict, dataclass, field
import math
import os
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit


@dataclass(frozen=True)
class McpServerConfig:
    command: str | None = None
    args: tuple[str, ...] = ()
    cwd: str | None = None
    env_vars: tuple[str, ...] = ()
    enabled: bool = True
    required: bool = False
    startup_timeout_sec: float = 10.0
    tool_timeout_sec: float = 60.0
    enabled_tools: tuple[str, ...] | None = None
    disabled_tools: tuple[str, ...] = ()
    url: str | None = None
    bearer_token_env_var: str | None = None
    credential_file: str | None = None
    http_headers: dict[str, str] = field(default_factory=dict)
    env_http_headers: dict[str, str] = field(default_factory=dict)
    connection_mode: str = "persistent"

    def __post_init__(self) -> None:
        if self.connection_mode not in ("persistent", "on_demand"):
            raise ValueError("MCP connection_mode must be persistent or on_demand")
        if self.connection_mode == "on_demand" and self.url is not None:
            raise ValueError("MCP on_demand connections require a stateless stdio server")
        if (self.command is None) == (self.url is None):
            raise ValueError("MCP requires exactly one of command (stdio) or url (Streamable HTTP)")
        if self.command is not None and (not isinstance(self.command, str) or not self.command.strip()):
            raise ValueError("MCP command must be a nonempty executable name or path")
        if self.url is not None:
            try:
                parsed = urlsplit(self.url)
                port = parsed.port
                if (not isinstance(self.url, str) or not self.url or self.url != self.url.strip()
                        or parsed.scheme not in {"http", "https"} or not parsed.hostname
                        or parsed.username is not None or parsed.password is not None or parsed.fragment
                        or any(ord(c) <= 32 or ord(c) == 127 for c in self.url)):
                    raise ValueError
            except (TypeError, ValueError, AttributeError):
                raise ValueError("MCP url must be an HTTP(S) endpoint without userinfo or a fragment") from None
        if self.cwd is not None and (not isinstance(self.cwd, str) or not self.cwd):
            raise ValueError("MCP cwd must be a nonempty path or None")
        for name in ("enabled", "required"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"MCP {name} must be a boolean")
        for name in ("startup_timeout_sec", "tool_timeout_sec"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"MCP {name} must be positive and finite")
        for name in ("args", "env_vars", "enabled_tools", "disabled_tools"):
            value = getattr(self, name)
            if value is None and name == "enabled_tools":
                continue
            if (not isinstance(value, (list, tuple))
                    or any(not isinstance(item, str) for item in value)):
                raise ValueError(f"MCP {name} must be a list of strings")
            if name != "args" and any(not item for item in value):
                raise ValueError(f"MCP {name} entries must not be empty")
            object.__setattr__(self, name, tuple(value))
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in self.env_vars):
            raise ValueError("MCP env_vars must contain environment variable names")
        if self.bearer_token_env_var is not None and not env_name(self.bearer_token_env_var):
            raise ValueError("MCP bearer_token_env_var must name an environment variable")
        for name in ("http_headers", "env_http_headers"):
            headers = getattr(self, name)
            if not isinstance(headers, dict):
                raise ValueError(f"MCP {name} must map header names to strings")
            seen = set()
            for key, value in headers.items():
                if (not isinstance(key, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key)
                        or key.lower() in seen or not isinstance(value, str)):
                    raise ValueError(f"Invalid or duplicate header in MCP {name}")
                seen.add(key.lower())
                # Protocol-owned headers are managed by the SDK, not user configuration.
                if key.lower() in {"host", "content-length", "content-type", "accept", "connection",
                                   "transfer-encoding", "mcp-session-id", "mcp-protocol-version", "last-event-id"}:
                    raise ValueError(f"MCP {name} cannot override protocol headers")
                if name == "env_http_headers":
                    if not env_name(value):
                        raise ValueError("MCP env_http_headers values must name environment variables")
                else:
                    validate_header_value(value)
            object.__setattr__(self, name, dict(headers))
        if self.credential_file is not None and (
                not isinstance(self.credential_file, str) or not Path(self.credential_file).is_absolute()
                or self.url is None or self.bearer_token_env_var or self.http_headers or self.env_http_headers):
            raise ValueError("A connection credential file requires HTTP and an absolute path")
        if self.url is None:
            if self.bearer_token_env_var is not None or self.http_headers or self.env_http_headers:
                raise ValueError("MCP HTTP authentication and headers require url")
        elif self.args or self.cwd is not None or self.env_vars:
            raise ValueError("MCP args, cwd and env_vars are only supported for stdio")
        if self.bearer_token_env_var is not None and any(
                key.lower() == "authorization" for key in (*self.http_headers, *self.env_http_headers)):
            raise ValueError("Use bearer_token_env_var or an Authorization header, not both")

    @property
    def transport(self) -> str:
        return "streamable_http" if self.url is not None else "stdio"

    def resolve_http_headers(self) -> dict[str, str]:
        headers = {name.lower(): value for name, value in self.http_headers.items()}
        for name, variable in self.env_http_headers.items():
            value = os.environ.get(variable)
            if not value:
                raise ValueError(f"MCP HTTP header environment variable {variable} is not set or empty")
            validate_header_value(value)
            headers[name.lower()] = value
        if self.bearer_token_env_var is not None:
            token = os.environ.get(self.bearer_token_env_var)
            if not token:
                raise ValueError(f"MCP bearer environment variable {self.bearer_token_env_var} is not set or empty")
            validate_header_value(token)
            headers["authorization"] = "Bearer " + token
        return headers

    def to_dict(self) -> dict:
        result = asdict(self)
        if self.credential_file is None:
            result.pop("credential_file")
        if self.connection_mode == "persistent":
            result.pop("connection_mode")
        for name in ("args", "env_vars", "enabled_tools", "disabled_tools"):
            if result[name] is not None:
                result[name] = list(result[name])
        if self.url is None:
            for name in ("url", "bearer_token_env_var", "http_headers", "env_http_headers"):
                result.pop(name)
        else:
            for name in ("command", "args", "cwd", "env_vars"):
                result.pop(name)
        return result


def env_name(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is not None


def validate_header_value(value):
    if any(ord(c) < 32 or ord(c) > 126 for c in value):
        raise ValueError("MCP HTTP header values must be printable ASCII")


def parse_servers(value: dict) -> dict[str, McpServerConfig]:
    if not isinstance(value, dict):
        raise ValueError("mcp_servers must be an object keyed by server name")
    servers = {}
    for name, raw in sorted(value.items(), key=lambda entry: str(entry[0])):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", name):
            raise ValueError("MCP server names must be 1–48 letters, digits, underscores or hyphens")
        if isinstance(raw, McpServerConfig):
            servers[name] = raw
        elif isinstance(raw, dict):
            try:
                servers[name] = McpServerConfig(**raw)
            except TypeError:
                raise ValueError(f"Invalid fields for MCP server '{name}'; supported transports are stdio and Streamable HTTP") from None
        else:
            raise ValueError(f"Invalid configuration for MCP server '{name}'")
    return servers


def load_mcp_config(path: Path) -> dict:
    """Read only explicit configuration; no implicit search or shell expansion."""
    with path.open("rb") as stream:
        value = tomllib.load(stream)
    if set(value) != {"mcp_servers"}:
        raise ValueError("MCP config must contain only [mcp_servers.<name>] tables")
    return {name: config.to_dict() for name, config in parse_servers(value["mcp_servers"]).items()}
