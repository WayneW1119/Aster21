"""Host-owned, relocatable TOML profiles; no runtime state or credentials in profiles."""

from copy import deepcopy
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import tempfile
import tomllib

from .core.session import SessionConfiguration
from .core.mcp.config import load_mcp_config
from .core.sandbox import FilesystemSandbox, parse_policy


def aster_home() -> Path:
    configured = os.environ.get("ASTER_HOME") or os.environ.get("HARNESS_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    current, legacy = Path.home() / ".aster", Path.home() / ".harness"
    # Keep existing installations on their original store without moving live data.
    return (legacy if not current.exists() and legacy.exists() else current).resolve()


harness_home = aster_home  # Compatibility for existing hosts and adapters.


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def toml_text(data: dict) -> str:
    def value(v):
        if isinstance(v, str):
            return json.dumps(v, ensure_ascii=False)
        if type(v) is bool:
            return str(v).lower()
        if type(v) is int:
            return str(v)
        if type(v) is float and math.isfinite(v):
            return repr(v)
        if isinstance(v, list):
            return "[" + ", ".join(value(x) for x in v) + "]"
        if isinstance(v, dict):
            return (
                "{ "
                + ", ".join(json.dumps(k) + " = " + value(x) for k, x in v.items())
                + " }"
            )
        raise ValueError("Unsupported TOML value")

    lines = []

    def section(table, prefix=()):
        if prefix:
            lines.append("[" + ".".join(json.dumps(k) for k in prefix) + "]")
        for k, v in table.items():
            if not isinstance(v, dict):
                lines.append(json.dumps(k) + " = " + value(v))
        for k, v in table.items():
            if isinstance(v, dict):
                lines.append("")
                section(v, (*prefix, k))

    section(data)
    return "\n".join(lines).strip() + "\n"


def override(data: dict, entries: list[str]) -> dict:
    result = deepcopy(data)
    for entry in entries:
        key, separator, raw = entry.partition("=")
        parts = key.split(".")
        if not separator or any(
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", p) for p in parts
        ):
            raise ValueError("Override must be dotted.key=TOML_VALUE")
        value = tomllib.loads("value = " + raw)["value"]
        target = result
        for part in parts[:-1]:
            target = target.setdefault(part, {})
            if not isinstance(target, dict):
                raise ValueError("Override crosses a scalar")
        target[parts[-1]] = value
    return result


def default_profile(workspace: Path, home: Path) -> dict:
    return {
        "name": "assistant",
        "workspace": str(workspace.resolve()),
        "instructions": "instructions.md",
        "model": {
            "name": "",
            "url": "https://api.openai.com/v1/responses",
            "api_key_env": "OPENAI_API_KEY",
            "env_file": str(home / "credentials" / "default.env"),
            "allow_http": False,
        },
        "runtime": {"max_consecutive_steps": "unlimited"},
        "context": {"warning_tokens": 100000, "tool_output_tokens": 10000},
        "sandbox": {"enabled": True, "format": 2, "rules": []},
        "tools": {"code_mode": False},
        "skills": {"directories": []},
    }


@dataclass
class Profile:
    path: Path
    data: dict
    workspace: Path
    instructions_path: Path
    configuration: SessionConfiguration
    env_file: Path | None
    instruction_text: str | None = None

    def snapshot(self) -> dict:
        data = deepcopy(self.data)
        data["workspace"] = str(self.workspace)
        data["instructions"] = "instructions.md"
        if self.env_file is not None:
            data["model"]["env_file"] = str(self.env_file)
        data["runtime"] = {
            "max_consecutive_steps": self.configuration.max_consecutive_steps
            or "unlimited"
        }
        data["context"] = {
            "warning_tokens": self.configuration.context_budget_warning_tokens or 0,
            "tool_output_tokens": self.configuration.tool_output_token_limit
            or "unlimited",
        }
        data["tools"] = {"code_mode": self.configuration.code_mode_enabled}
        data["sandbox"] = {
            "enabled": self.configuration.filesystem_policy is not None,
            "format": 2,
            "rules": [],
        }
        data["model"].setdefault("allow_http", False)
        data["skills"] = {"directories": list(self.configuration.skill_directories),
                          "workspace": self.configuration.workspace_skills}
        if self.configuration.skill_sources:
            data["skills"]["sources"] = list(self.configuration.skill_sources)
        if self.configuration.filesystem_policy is not None:
            data["sandbox"]["rules"] = deepcopy(
                self.configuration.filesystem_policy["rules"]
            )
            for key in ('gpu', 'local_ipc'):
                if self.configuration.filesystem_policy.get(key):
                    data['sandbox'][key] = True
        if data.get("mcp", {}).get("config"):
            data["mcp"]["config"] = str(self.resolve(data["mcp"]["config"]))
        return data

    def resolve(self, value) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Expected a nonempty path")
        return (self.path.parent / Path(value).expanduser()).absolute()

    def instructions(self) -> str:
        text = (
            self.instruction_text
            if self.instruction_text is not None
            else self.instructions_path.read_text(encoding="utf-8")
        )
        if len(text) > 32000:
            raise ValueError("Instructions exceed 32000 characters")
        return text


def load_profile(
    path: Path,
    entries=(),
    *,
    data: dict | None = None,
    instruction_text: str | None = None,
) -> Profile:
    path = path.expanduser().resolve()
    raw = (
        tomllib.loads(path.read_text(encoding="utf-8"))
        if data is None
        else deepcopy(data)
    )
    data = override(raw, entries)
    allowed = {
        "model": {"protocol", "name", "url", "api_key_env", "env_file", "allow_http", "auth", "auth_home", "auth_profile"},
        "runtime": {"max_consecutive_steps"},
        "context": {"warning_tokens", "tool_output_tokens"},
        "sandbox": {"enabled", "rules", "format", "gpu", "local_ipc", "default_access"},
        "tools": {"code_mode"},
        "skills": {"directories", "sources", "workspace"},
        "mcp": {"config"},
    }
    if set(data) - {"name", "workspace", "instructions", *allowed}:
        raise ValueError("Unknown profile field")
    for key, fields in allowed.items():
        table = data.get(key, {})
        if not isinstance(table, dict) or set(table) - fields:
            raise ValueError("Invalid profile section: " + key)
    if not isinstance(data.get("name"), str) or not data["name"].strip():
        raise ValueError("Profile requires a name")

    def resolve(value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Expected a nonempty path")
        return (path.parent / Path(value).expanduser()).absolute()

    workspace = resolve(data["workspace"]).resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("Workspace must be a directory")
    instructions = resolve(data.get("instructions", "instructions.md"))
    model = data.setdefault("model", {})
    if model.get("protocol", "responses") not in ("responses", "messages"):
        raise ValueError("model.protocol must be responses or messages")
    authentication = model.get("auth", "api_key")
    if authentication not in ("api_key", "chatgpt"):
        raise ValueError("model.auth must be api_key or chatgpt")
    if authentication == "chatgpt" and model.get("protocol") == "messages":
        raise ValueError("Messages requires API key authentication")
    if authentication == "chatgpt":
        model.setdefault("api_key_env", "OPENAI_API_KEY")
        model.setdefault("url", "https://api.openai.com/v1/responses")
        if model["url"] != "https://api.openai.com/v1/responses":
            raise ValueError("ChatGPT plan auth requires the official Responses endpoint")
        model["auth_home"] = (str(resolve(model["auth_home"]))
                              if model.get("auth_home") else str(aster_home()))
        from .auth.chatgpt import ChatGPTAccount
        ChatGPTAccount(model.get("auth_home") or aster_home(), model.get("auth_profile", "default"))
    for key in ("name", "url", "api_key_env"):
        if not isinstance(model.get(key), str):
            raise ValueError("model." + key + " must be a string")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", model["api_key_env"]):
        raise ValueError("Invalid API key environment variable")
    if type(model.get("allow_http", False)) is not bool:
        raise ValueError("allow_http must be boolean")
    from .core.client import transport_for

    transport_for(model)(
        model["url"],
        model["name"],
        "validation-placeholder",
        allow_http=model.get("allow_http", False),
    )
    env_file = resolve(model["env_file"]) if model.get("env_file") else None
    runtime, context, sandbox = (
        data.get(key, {}) for key in ("runtime", "context", "sandbox")
    )

    def limit(value, disabled):
        if value == disabled and type(value) is type(disabled):
            return None
        if type(value) is not int or value < 1:
            raise ValueError("Limit must be positive or " + repr(disabled))
        return value

    enabled = sandbox.get("enabled", True)
    if type(enabled) is not bool:
        raise ValueError("sandbox.enabled must be boolean")
    rules = deepcopy(sandbox.get("rules", []))
    if not isinstance(rules, list):
        raise ValueError("sandbox.rules must be an array")
    if not enabled and rules:
        raise ValueError("Disabled sandbox cannot have rules")
    if 'default_access' in sandbox and 'format' in sandbox:
        raise ValueError('default_access is only supported when migrating an old profile')
    gpu = sandbox.get('gpu', False)
    if type(gpu) is not bool or not enabled and gpu:
        raise ValueError('sandbox.gpu requires a boolean and an enabled sandbox')
    local_ipc = sandbox.get('local_ipc', False)
    if type(local_ipc) is not bool or not enabled and local_ipc:
        raise ValueError('sandbox.local_ipc requires a boolean and an enabled sandbox')
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {"path", "access"}:
            raise ValueError("Invalid sandbox rule")
        rule["path"] = "/" if rule["path"] == "/" else str(resolve(rule["path"]))
    directories = data.get("skills", {}).get("directories", [])
    if not isinstance(directories, list):
        raise ValueError("skills.directories must be an array")
    from .core.skills.service import validate_sources
    validated_skill_sources = validate_sources(data.get("skills", {}).get("sources", []))
    mcp_path = data.get("mcp", {}).get("config")
    configuration = SessionConfiguration(
        max_consecutive_steps=limit(
            runtime.get("max_consecutive_steps", "unlimited"), "unlimited"
        ),
        context_budget_warning_tokens=limit(context.get("warning_tokens", 100000), 0),
        tool_output_token_limit=limit(
            context.get("tool_output_tokens", 10000), "unlimited"
        ),
        code_mode_enabled=data.get("tools", {}).get("code_mode", False),
        workspace_skills=data.get("skills", {}).get("workspace", True),
        skill_directories=tuple(str(resolve(p)) for p in directories),
        skill_sources=tuple({**source, "path": str(resolve(source["path"]))}
            for source in validated_skill_sources),
        filesystem_policy=parse_policy({"rules": rules, **({"default_access": sandbox["default_access"]} if "default_access" in sandbox else {}), **({key: True for key, flag in (('gpu', gpu), ('local_ipc', local_ipc)) if flag}), **({"format": sandbox["format"]} if "format" in sandbox else {})}, legacy=True) if enabled else None,
        mcp_servers=load_mcp_config(resolve(mcp_path)) if mcp_path else {},
    )
    # Validate path policy without launching any tools or making model requests.
    FilesystemSandbox(workspace, configuration.filesystem_policy)
    profile = Profile(
        path,
        data,
        workspace,
        instructions,
        configuration,
        env_file,
        instruction_text=instruction_text,
    )
    profile.instruction_text = profile.instructions()
    return profile
