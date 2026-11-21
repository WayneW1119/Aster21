"""Host-owned MCP definitions; members select service IDs, never credentials."""
import json
import re
from pathlib import Path

from aster import SessionConfiguration
from relay.store import Problem


RESERVED = {"squad", "relay"}
MAX_SERVICES = 16
PRIVATE_ENV = {"SQUAD_TOKEN", "RELAY_TOKEN", "SQUAD_MODEL_KEY"}


def environment_keys(servers):
    keys = set()
    for server in servers.values():
        keys.update(server.get("env_vars", []))
        keys.update(server.get("env_http_headers", {}).values())
        if name := server.get("bearer_token_env_var"):
            keys.add(name)
    return keys


def load_catalog(path):
    if not path:
        return {}
    value = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(value, dict) or len(value) > MAX_SERVICES:
        raise ValueError("MCP catalog must contain at most 16 services")
    result = {}
    for ident, entry in value.items():
        if (not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", ident)
                or ident in RESERVED or not isinstance(entry, dict)
                or set(entry) - {"name", "default", "server"}
                or not isinstance(entry.get("name"), str) or not 1 <= len(entry["name"]) <= 80
                or type(entry.get("default", False)) is not bool or not isinstance(entry.get("server"), dict)):
            raise ValueError("Invalid MCP service catalog entry")
        server = SessionConfiguration(mcp_servers={ident: entry["server"]}).mcp_servers[ident]
        if environment_keys({ident: server}) & PRIVATE_ENV:
            raise ValueError("Additional MCP services cannot reuse built-in private credentials")
        result[ident] = {"name": entry["name"], "default": entry.get("default", False), "server": server}
    return result


def public_catalog(catalog):
    return [{"id": ident, "name": entry["name"], "default": entry["default"]} for ident, entry in catalog.items()]


def selection(catalog, configured=None):
    if configured is None:
        return [name for name, entry in catalog.items() if entry["default"]]
    if (not isinstance(configured, list) or any(not isinstance(x, str) for x in configured)
            or len(configured) != len(set(configured)) or any(name not in catalog for name in configured)):
        raise Problem("MCP 服务须为宿主目录中有效且不重复的服务 ID 列表")
    return sorted(configured)


def clean(value):
    """TOML has no null; use the public SDK's normalized configuration."""
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    return value
