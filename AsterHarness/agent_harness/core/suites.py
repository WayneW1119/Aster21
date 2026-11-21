"""Host-installed capabilities. A thread owns one resolved installation per suite ID."""

from copy import deepcopy
import asyncio
import json
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit

from .event_generators.program import ProgramGenerator
from .event_generators.timer import TimerGenerator
from .mcp.config import parse_servers
from .skills.service import validate_sources


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", value):
        raise ValueError("Expected an ASCII identifier of 1–48 characters")
    return value


def read_manifest(path, parameters):
    if not isinstance(parameters, dict) or any(not isinstance(v, str) for v in parameters.values()):
        raise ValueError("Suite parameters must map names to strings")
    path = Path(path).resolve(strict=True)
    if path.is_dir():
        path = path / "suite.toml"
    with path.open("rb") as stream:
        data = tomllib.load(stream)

    def bind(value):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                key = bind(key)
                if key in result:
                    raise ValueError("Duplicate suite resource after parameter binding")
                result[key] = bind(item)
            return result
        if isinstance(value, list):
            return [bind(item) for item in value]
        if isinstance(value, str):
            return re.sub(r"\$\{([a-zA-Z_][a-zA-Z_0-9]*)\}",
                          lambda match: parameters[match[1]], value)
        return value

    try:
        suite = bind(data)
    except KeyError as error:
        raise ValueError(f"Missing suite parameter {error.args[0]}") from None
    for source in suite.get("skills", []):
        source["path"] = str((path.parent / source["path"]).resolve(strict=True))
    for spec in suite.get("generators", {}).values():
        if spec.get("kind") == "program":
            spec["script_path"] = str((path.parent / spec["script_path"]).resolve(strict=True))
    return suite


def generator(spec, workspace):
    args = dict(spec)
    kind = args.pop("kind")
    args.pop("name", None)
    args.pop("notes", None)
    if kind == "timer":
        return TimerGenerator(**args)
    if kind == "program":
        return ProgramGenerator(cwd=str(workspace), **args)
    raise ValueError("Suite source must be a program or timer")


def compile_suites(installs, base_servers, grants):
    servers = dict(base_servers)
    sources, generators = [], {}
    for key, install in installs.items():
        spec = install["manifest"]
        if identifier(spec["id"]) != key or set(spec) - {"id", "skills", "mcp", "generators", "requires"}:
            raise ValueError("Invalid suite manifest")
        required = spec.get("requires", [])
        if not isinstance(required, list) or any(not isinstance(v, str) or not v for v in required):
            raise ValueError("Suite requires must list host permission names")
        parse_servers(spec.get("mcp", {}))
        skill_sources = validate_sources(spec.get("skills", []))
        if not install["enabled"]:
            continue
        missing = set(spec.get("requires", [])) - set(grants)
        if missing:
            raise ValueError("Missing host permission: " + ", ".join(sorted(missing)))
        for name, server in spec.get("mcp", {}).items():
            if name in servers:
                raise ValueError("Duplicate MCP namespace: " + name)
            servers[name] = server
        for source in skill_sources:
            sources.append({**source, "label": key})
        for name, definition in spec.get("generators", {}).items():
            generators[key + "/" + identifier(name)] = definition
    parse_servers(servers)
    return servers, sources, generators


class Capabilities:
    """Only references and resolved host configuration are persisted in private thread storage."""
    def __init__(self, session, root):
        self.lock = asyncio.Lock()
        self.session = session
        self.path = root / "capabilities.json"
        self.state = (json.loads(self.path.read_text(encoding="utf-8")) if session.rollout is not None and self.path.exists()
                      else {"suites": {}, "connections": {}, "credentials": {}, "allow_connect": False, "grants": []})

    def save(self, state):
        from ..agent_config import atomic_write
        atomic_write(self.path, json.dumps(state, ensure_ascii=False))
        self.state = state

    def resources(self, state, base_servers):
        base = dict(base_servers)
        for name, connection in state["connections"].items():
            if name in base:
                raise ValueError("Duplicate MCP namespace: " + name)
            if state["allow_connect"]:
                base[name] = connection
        return compile_suites(state["suites"], base, state["grants"])

    async def apply(self, state, *, offline=False, resume=False):
        session = self.session
        servers, sources, definitions = self.resources(state, session.configuration.mcp_servers)
        manager = session.generator_manager
        programs = {key: generator(spec, session.process_manager.workspace)
                    for key, spec in definitions.items()}
        owned = {r["resource"]: r for r in manager.list(True) if "resource" in r}
        changed = set()
        for key in owned.keys() & definitions.keys():
            spec, record = definitions[key], owned[key]
            old_config = {k: v for k, v in record["configuration"].items() if k not in {"state_dir", "log_path"}}
            new_config = {k: v for k, v in programs[key].describe().items() if k not in {"state_dir", "log_path"}}
            if (new_config != old_config or record["kind"] != spec["kind"]
                    or any(record.get(k) != spec.get(k) for k in ("name", "notes"))):
                changed.add(key)
        retry = {key for key in (owned.keys() & definitions.keys()) - changed
                 if resume and owned[key]["status"] == "failed" and definitions[key]["kind"] == "program"
                 and definitions[key].get("mode", "service") == "service"}
        removed = (owned.keys() - definitions.keys()) | changed
        added = (definitions.keys() - owned.keys()) | changed
        if sum(r["status"] == "active" and r.get("resource") not in removed
               for r in manager.list()) + len(added) + len(retry) > manager.MAX_ACTIVE:
            raise ValueError("Active event generator limit reached")
        for spec in definitions.values():
            manager.metadata(spec.get("name"), spec.get("notes"))
        if offline:
            if state != self.state:
                self.save(state)
            return
        previous = self.state
        if state != previous:
            self.save(state)
        if parse_servers(servers) != session.services.mcp.config:
            try:
                new_connections = state["connections"].keys() - previous["connections"].keys()
                await session.services.mcp.replace_servers(servers, require_ready=new_connections)
            except BaseException:
                if state != previous:
                    self.save(previous)
                raise
            if session.mcp_projection is None:
                from .context_manager.mcp import McpProjection
                session.mcp_projection = McpProjection()
            session._rebuild_deferred_router()
        session.skill_service.managed = sources
        for key in sorted(removed):
            await manager.retire_resource(owned[key]["generator_id"])
        for key in sorted(retry):
            await manager.restart_program(owned[key]["generator_id"])
        for key in sorted(added):
            spec = definitions[key]
            manager.register(programs[key], name=spec.get("name"), notes=spec.get("notes"), resource=key)

    async def change(self, operation, *, offline=False):
        async with self.lock:
            candidate = deepcopy(self.state)
            operation(candidate)
            await self.apply(candidate, offline=offline)

    async def connect(self, name, url, credential_ref=None):
        identifier(name)
        if not self.state["allow_connect"]:
            raise ValueError("Host has not enabled agent-managed HTTP MCP")
        server = {"url": url}
        parse_servers({name: server})
        target = urlsplit(url)
        if target.query:
            raise ValueError("Use a credential reference instead of URL query parameters")
        if credential_ref is not None:
            ref = self.state["credentials"].get(credential_ref)
            if ref is None and Path(credential_ref).is_absolute():
                server["credential_file"] = credential_ref
            elif not ref or ref["url"] != url:
                raise ValueError("Credential is unavailable for this endpoint")
            else:
                server["bearer_token_env_var"] = ref["env"]
        def add(state):
            if len(state["connections"]) >= 32:
                raise ValueError("Agent HTTP connection limit reached")
            if name in state["connections"]:
                raise ValueError("Duplicate MCP namespace: " + name)
            state["connections"][name] = server
        await self.change(add)
        return {"connected": name}

    async def disconnect(self, name):
        def remove(state):
            if not state["allow_connect"] or name not in state["connections"]:
                raise ValueError("Only authorized agent-owned connections can be removed")
            del state["connections"][name]
        await self.change(remove)
        return {"disconnected": name}
