"""Codex-style environment context with pinned filesystem permission profiles."""

import os
from copy import deepcopy
from html import escape

from . import WorldStateUpdate
from .lifecycle import ContextSection, SectionCompaction


def checked(value):
    if (
        not isinstance(value, dict)
        or set(value) - {"protected_deny", "rule_precedence", "gpu_devices", "local_ipc", "private_tmp"}
        != {
            "policy_version",
            "workspace",
            "default_access",
            "rules",
            "protected_read",
            "network",
            "approvals",
        }
        or type(value["policy_version"]) is not int
        or value["policy_version"] < 1
        or not isinstance(value["workspace"], str)
        or value["default_access"] not in ("read", "write", "deny", "unrestricted")
        or value["network"] != "unchanged"
        or value["approvals"] != "never"
        or not isinstance(value["rules"], list)
        or not isinstance(value["protected_read"], list)
    ):
        raise ValueError("Invalid environment state")
    if (value.get("rule_precedence") not in (None, "most_specific")
            or not isinstance(value.get("protected_deny", []), list)
            or any(not isinstance(path, str) for path in value.get("protected_deny", []))):
        raise ValueError("Invalid environment rule semantics")
    if value["default_access"] == "unrestricted" and (value["rules"] or value["protected_read"] or value.get('protected_deny')):
        raise ValueError("Disabled sandbox cannot have restrictions")
    for entry in value["rules"]:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"path", "access"}
            or not isinstance(entry["path"], str)
            or entry["access"] not in {"read", "write", "deny"}
        ):
            raise ValueError("Invalid environment filesystem entry")
    if any(not isinstance(path, str) for path in value["protected_read"]):
        raise ValueError("Invalid protected paths")
    if not isinstance(value.get('gpu_devices', []), list) or any(
            not isinstance(path, str) for path in value.get('gpu_devices', [])):
        raise ValueError('Invalid GPU device permissions')
    if type(value.get('local_ipc', False)) is not bool or not isinstance(value.get('private_tmp', []), list):
        raise ValueError('Invalid local IPC permissions')
    if any(path not in ('/tmp', '/var/tmp') for path in value.get('private_tmp', [])):
        raise ValueError('Invalid private temporary directories')
    return deepcopy(value)


def environment_item(value):
    value = checked(value)
    platform_context = ([
        '<platform>Windows native</platform>',
        '<shell>PowerShell</shell>',
        'Use PowerShell syntax, including & $env:HARNESS_PYTHON to run Python. Windows preview supports non-TTY commands only; tty=true is unavailable. Ctrl+C stops the owned command tree.'
    ] if os.name == 'nt' else [])
    if value["default_access"] == "unrestricted":
        return {
            "type": "message", "role": "developer",
            "text": "\n".join([
                "<environment_context>",
                *platform_context,
                f'<filesystem policy_version="{value["policy_version"]}">',
                "<workspace_roots><root>" + escape(value["workspace"]) + "</root></workspace_roots>",
                '<permission_profile type="disabled"><file_system type="unrestricted" /></permission_profile>',
                "</filesystem>",
                "The host has disabled the sandbox. Local tools run with the host user's filesystem and process permissions; this does not grant root or administrator privileges.",
                "Use the highest policy_version; this context supersedes older filesystem restrictions.",
                "Network access is unchanged. No approval mechanism is available. Private memory/history use their dedicated host tools.",
                "Policy changes stop old command/cell handles, reconnect local MCPs and restart registered services; one-shot work is not replayed.",
                "</environment_context>",
            ]),
            "_environment_state": value,
        }
    text = [
        "<environment_context>",
        *platform_context,
        f"<filesystem policy_version=\"{value['policy_version']}\">",
        "<workspace_roots><root>"
        + escape(value["workspace"])
        + "</root></workspace_roots>",
        '<permission_profile type="managed"><file_system type="restricted" default_access="'
        + value["default_access"]
        + '">',
    ]
    text.append(
        '<entry access="'
        + value["default_access"]
        + '"><special>:root</special></entry>'
    )
    runtime_entries = ([] if value.get('rule_precedence') == 'most_specific' else
                       [{"path": p, "access": "read"} for p in value['protected_read']])
    for entry in value['rules'] + runtime_entries + [{"path": p, "access": "deny"} for p in value.get('protected_deny', [])]:
        text.append(
            '<entry access="'
            + entry["access"]
            + '"'
            + (' escalatable="false"' if entry["access"] == "deny" else "")
            + (' protected="true"' if entry['path'] in value.get('protected_deny', []) else '')
            + "><path>"
            + escape(entry["path"])
            + "</path></entry>"
        )
    if value.get('rule_precedence') == 'most_specific':
        for path in value['protected_read']:
            text.append('<read_only_ceiling><path>' + escape(path) + '</path></read_only_ceiling>')
    text += [
        "</file_system></permission_profile></filesystem>",
        *(['Local Unix socket communication is authorized. Visible host services may act beyond filesystem grants. Private temporary directories: ' + ', '.join(value.get('private_tmp', [])) + '; contents last for this execution tree only. Explicit path rules take precedence.'] if value.get('local_ipc') else []),
        *(['GPU device access: ' + ', '.join(value['gpu_devices']) + '. Device permission is separate from file read/write rules.'] if value.get('gpu_devices') else []),
        ("write includes read. deny blocks content except explicitly reopened narrower paths; it does not promise to hide mount point names or existence."
         if value.get('rule_precedence') == 'most_specific' else
         "write includes read. deny blocks real content and descendants, but does not promise to hide names or existence."),
        "Use the highest policy_version; a newer environment context supersedes older filesystem rules, rather than accumulating them.",
        ("More specific paths override broader paths; equal paths use deny > write > read. Host-protected denies cannot be reopened and trusted runtime paths remain read-only. The workspace does not change with cd."
         if value.get("rule_precedence") == "most_specific" else
         "Deny rules take precedence. Trusted runtime paths remain read-only. The workspace does not change with cd."),
        *(["Filesystem restrictions apply to local Windows commands and scripts. Program sources receive a private state grant. Network access is available."]
          if os.name == "nt" else [
              "/proc exposes sandbox processes, /dev exposes minimal devices, and /run is an isolated read-only runtime view; source state grants are private exceptions.",
              ("Filesystem restrictions are enforced for local commands and scripts. Network access is unchanged; Unix sockets are authorized and vsock remains blocked."
               if value.get('local_ipc') else "Filesystem restrictions are enforced for local commands and scripts. Network access is unchanged; new host Unix/vsock connections are blocked."),
          ]),
        "No approval or automatic unsandboxed retry is available. Private memory/history use their dedicated host tools.",
        "Policy changes stop old command/cell handles, reconnect local MCPs and restart registered services; one-shot work is not replayed.",
        "</environment_context>",
    ]
    return {
        "type": "message",
        "role": "developer",
        "text": "\n".join(text),
        "_environment_state": value,
    }


class EnvironmentState(ContextSection):
    ID = "environment"

    def __init__(self, sandbox):
        self.sandbox = sandbox
        self.baseline = sandbox.describe()
        self.current = deepcopy(self.baseline)
        self._observed_state = None

    def snapshot_state(self):
        return (
            {
                "version": 1,
                "baseline": deepcopy(self.baseline),
                "current": deepcopy(self.current),
            }
            if self.current is not None
            else None
        )

    def restore(self, state):
        if (
            not isinstance(state, dict)
            or set(state) != {"version", "baseline", "current"}
            or type(state["version"]) is not int
            or state["version"] != 1
        ):
            raise ValueError("Invalid environment projection")
        self.baseline, self.current = (checked(state["baseline"]) if state["baseline"] is not None else None), checked(
            state["current"]
        )

    def reconstruct(self, items):
        current = deepcopy(self.baseline)
        for item in items:
            if "_environment_state" in item:
                state = checked(item["_environment_state"])
                if item != environment_item(state):
                    raise RuntimeError("Invalid environment evidence")
                current = state
        return current

    def prefix_items(self):
        return [environment_item(self.baseline)] if self.baseline is not None else []

    def project_items(self, items):
        result = deepcopy(items)
        for item in result:
            item.pop("_environment_state", None)
            item.pop("_filesystem_policy", None)
        return result

    def validate(self, items):
        if self.reconstruct(items) != self.current:
            raise RuntimeError("Environment invariant failed")

    def observe(self, items):
        self.current = self.reconstruct(items)
        return super().observe(items)

    def render_diff(self, previous, items):
        desired = self.sandbox.describe()
        if desired is None:
            return None
        additions = (
            () if self.reconstruct(items) == desired else (environment_item(desired),)
        )
        return (
            WorldStateUpdate(self.ID, desired, additions)
            if previous != desired or additions
            else None
        )

    def plan_compaction(self, items, first, last, whole):
        if self.current is None:
            return None
        state = self.snapshot_state()
        selected = [i for i in items[first : last + 1] if "_environment_state" in i]
        if whole:
            state["baseline"] = deepcopy(self.current)
        points = (
            ()
            if whole or not selected
            else (environment_item(selected[-1]["_environment_state"]),)
        )
        return SectionCompaction(
            self.ID, state, points, state["baseline"] != self.baseline
        )

    def validate_compaction(self, plan, items):
        other = EnvironmentState(self.sandbox)
        other.restore(plan.state)
        other.validate(items)

    def commit_compaction(self, plan):
        self.restore(plan.state)
