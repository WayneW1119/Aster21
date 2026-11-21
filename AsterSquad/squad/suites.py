"""Resolve host-selected capabilities for members enrolled in suite management."""
import hashlib
from pathlib import Path
import re
import sys


def relay_namespace(domain, username, reserved=()):
    original = f"relay_{domain}_{username}"
    normalized = re.sub(r"[^A-Za-z0-9_-]+", "_", original)
    if normalized == original and len(original) <= 30 and original not in reserved:
        return original
    suffix = hashlib.sha256(original.encode()).hexdigest()[:8]
    domain_part = re.sub(r"[^A-Za-z0-9_-]+", "_", domain).strip("_")[:8] or "domain"
    user_part = re.sub(r"[^A-Za-z0-9_-]+", "_", username).strip("_")[:7] or "user"
    candidate = f"relay_{domain_part}_{user_part}_{suffix}"
    counter = 0
    while candidate in reserved:
        counter += 1
        candidate = f"relay_{domain_part}_{user_part}_{suffix}_{counter}"
    return candidate


def requirements(name, server):
    if name == "browser" and sys.platform.startswith("linux") and server.get("command"):
        return ["local_ipc"]
    return []


def installations(config, catalog, selected, global_skills):
    alias = config["relay_namespace"]
    sources = [{"path": str(global_skills / name)} for name in ("squad", "relay", "aster-harness")
               if (global_skills / name / "SKILL.md").is_file()]
    base = {"id": "squad", "skills": sources, "mcp": {
        "squad": {"url": config["squad_mcp"], "bearer_token_env_var": "SQUAD_TOKEN", "required": True},
        alias: {"url": config["relay_mcp"], "bearer_token_env_var": "RELAY_TOKEN", "required": True}},
        "generators": {"inbox": {"kind": "program", "name": alias, "notes": "Default Relay inbox",
                                "script_path": config["relay_watch"], "mode": "service",
                                "args": ["--connection", config["relay_connection"], "--listen-name", alias]}}}
    result = [(base, True)]
    for name, entry in catalog.items():
        folder = Path(__file__).parent / "assets" / "suite-skills" / name
        requires = requirements(name, entry["server"])
        result.append(({"id": name, "requires": requires, "mcp": {name: entry["server"]},
                        "skills": [{"path": str(folder)}] if folder.is_dir() else []}, name in selected))
    return result
