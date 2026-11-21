"""Translate Aster's filesystem policy into native MXC path lists."""

import os
from pathlib import Path
import re

from agent_harness._windows import is_reparse
from agent_harness.core.sandbox import parse_policy
from agent_harness.core.sandbox.policy import TIE, access_for


_DRIVE = re.compile(r"[A-Za-z]:")
_RESERVED = re.compile(r"(?i)(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[0-9¹²³])")
_FIELDS = {"read": "readonlyPaths", "write": "readwritePaths", "deny": "deniedPaths"}


def _path(value, *, base=None):
    path = Path(value)
    if not path.anchor and base is not None:
        path = base / path
    if (not path.is_absolute() or not _DRIVE.fullmatch(path.drive)
            or any(part.endswith((" ", ".")) or _RESERVED.fullmatch(part.split(".", 1)[0].rstrip())
                   or any(ord(char) < 32 or char in '<>:"|?*' for char in part)
                   for part in path.parts[1:])):
        raise ValueError(f"MXC requires an unambiguous local Windows path: {value}")
    if any(is_reparse(part) for part in (path, *path.parents)):
        raise ValueError(f"MXC policy paths cannot cross reparse points: {value}")
    resolved = path.resolve(strict=True)
    if not _DRIVE.fullmatch(resolved.drive):
        raise ValueError(f"MXC path resolved outside local drive paths: {value}")
    return resolved


def _fixed_volume_roots():
    import win32api
    import win32file

    return [
        _path(root)
        for root in win32api.GetLogicalDriveStrings().split("\0")
        if root and win32file.GetDriveType(root) == 3  # DRIVE_FIXED
    ]


def _rules(entries):
    return [{"path": str(path), "access": access} for path, access in entries.items()]


def compile_filesystem(workspace, policy, *, cwd=None, private_roots=(), runtime_roots=(), role_grants=()):
    """Produce an MXC policy snapshot without changing host state or launching it."""
    if os.name != "nt":
        raise OSError("MXC filesystem policy requires native Windows")
    policy = parse_policy(policy)
    if policy is None:
        raise ValueError("MXC requires an enabled filesystem policy")
    workspace = _path(workspace)
    if workspace == workspace.parent or not workspace.is_dir():
        raise ValueError("MXC workspace must be a directory below a volume root")
    if policy.get("local_ipc"):
        raise ValueError("Local IPC permission is currently supported only on Linux/WSL")
    if policy.get("gpu"):
        raise ValueError("GPU device permission currently requires WSL2")
    root_access = next((r["access"] for r in policy["rules"] if r["path"] == "/"), "deny")
    entries = {}
    for rule in policy["rules"]:
        access = rule["access"]
        if rule["path"] == "/":
            continue
        path = _path(rule["path"], base=workspace)
        if path not in entries or TIE[access] > TIE[entries[path]]:
            entries[path] = access
    entries.setdefault(workspace, "write")
    if root_access != "deny":
        for root in _fixed_volume_roots():
            entries.setdefault(root, root_access)

    requested = _rules(entries)
    runtime = tuple(_path(path) for path in runtime_roots)
    for root in runtime:
        inherited = access_for(root, requested, default="read")
        entries.setdefault(root, "deny" if inherited == "deny" else "read")
    for path, access in list(entries.items()):
        if access == "write" and any(path.is_relative_to(root) for root in runtime):
            entries[path] = "read"

    # MXC lets more-specific grants reopen an ordinary deny. Remove every
    # descendant grant before adding a host-owned deny so it cannot be reopened.
    private = tuple(_path(path) for path in private_roots)
    if any(root == root.parent for root in private):
        raise ValueError("MXC protected paths must be below a volume root")
    entries = {path: access for path, access in entries.items()
               if not any(path.is_relative_to(root) for root in private)}
    entries.update(dict.fromkeys(private, "deny"))
    # These host-owned state grants are independent of unmatched user paths;
    # explicit ordinary denies and protected ancestors still reject them.
    for source, access in role_grants:
        path = _path(source)
        if (access not in ("read", "write") or path == path.parent
                or any(root.is_relative_to(path) for root in private)
                or any(path.is_relative_to(root) or root.is_relative_to(path) for root in runtime)
                or access_for(path, requested, default="read") == "deny"):
            raise ValueError("MXC role grant conflicts with filesystem protections")
        entries[path] = access
    command_cwd = _path(cwd if cwd is not None else workspace, base=workspace)
    if not command_cwd.is_dir() or access_for(command_cwd, _rules(entries), default="deny") == "deny":
        raise ValueError("MXC command working directory must be a readable directory")

    filesystem = {field: [] for field in _FIELDS.values()}
    for path, access in sorted(entries.items()):
        filesystem[_FIELDS[access]].append(str(path))
    return filesystem
