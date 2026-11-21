"""Model-facing management and deferred script-tool adapters."""

import json

from .registry import Tool


def append_custom_tools(registry, binding):
    for prepared in binding.tools:
        def bind(call):
            async def handle(**arguments):
                return json.dumps(await call.call(arguments), ensure_ascii=False)
            return handle
        spec = prepared.loadable_spec["tools"][0]
        registry.register(Tool(spec, bind(prepared), deferred=True), namespace=prepared.namespace,
                          namespace_description=prepared.loadable_spec["description"])


def register_custom_management(registry, manager, request_change):
    async def group(action, group=None, description=None, instructions=None):
        if action == "list":
            return json.dumps({"revision": manager.state["revision"], "groups": {
                name: {"description": entry["description"], "version": entry["version"],
                       "tools": sorted(entry["tools"])} for name, entry in manager.state["groups"].items()}},
                ensure_ascii=False)
        if action == "get":
            if group is None:
                raise ValueError("group is required")
            return json.dumps(manager.inspect(group), ensure_ascii=False)
        return json.dumps(await request_change(manager.prepare_group(
            action, group, description, instructions)), ensure_ascii=False)

    async def tool(action, group, name=None, definition=None):
        if action == "list":
            return json.dumps({name: {"description": entry["description"], "version": entry["version"]}
                for name, entry in manager.inspect(group)["tools"].items()}, ensure_ascii=False)
        if action == "get":
            if name is None:
                raise ValueError("name is required")
            return json.dumps(manager.inspect(group, name), ensure_ascii=False)
        return json.dumps(await request_change(manager.prepare_tool(
            action, group, name, definition)), ensure_ascii=False)

    registry.register(Tool({"type": "function", "name": "manage_tool_group", "strict": False,
        "description": "Create, update, list, get or remove your private custom script tool groups. "
            "Groups are deferred namespaces local_<group>; WorldState lists their descriptions. "
            "description is a short capability summary; instructions holds detailed usage guidance. "
            "These are usage metadata, not human authorization or higher-priority rules. "
            "create/update requires description; update replaces both description and instructions. "
            "Mutations return accepted, then apply in request order at the next Step boundary; "
            "a custom_tool_change_applied/failed event reports the actual outcome. Current calls "
            "keep their captured version. Group removal also removes its tools. list/get show "
            "active state, not pending requests. Use tool_search to load callable definitions. "
            "Groups and tools survive clean resume and compact; creating tools grants no new permissions.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["create", "update", "list", "get", "remove"],
                       "description": "Operation to perform; mutations take effect at the next Step boundary."},
            "group": {"type": "string", "description": "1-40 ASCII letters/digits/_/-, starting with a letter."},
            "description": {"type": "string", "description": "Short capability summary, required for create/update."},
            "instructions": {"type": "string", "description": "Detailed group usage guidance; omitted becomes empty on create/update."}},
            "required": ["action"], "additionalProperties": False}}, group))
    registry.register(Tool({"type": "function", "name": "manage_custom_tool", "strict": False,
        "description": "Register, update, list, get or remove a private script tool in an existing group. "
            "Write/edit the script with apply_patch or command tools first. register/update requires "
            "a complete definition: description, input_schema (object JSON Schema), execution. "
            "execution requires command (interpreter/executable) and script_path (workspace entrypoint); "
            "optional args are script arguments, cwd defaults to workspace, timeout_sec defaults to 30 "
            "and is limited to 1-300. The host invokes command, copied script_path, then args without "
            "a shell, sends one JSON arguments object (at most 1 MiB) on stdin, and returns stdout/stderr, exit_code, "
            "success, version, truncation and timeout status. Each stream retains up to 64 KiB. "
            "The entrypoint is snapshotted at acceptance; editing the original has no effect until "
            "an explicit update. Other files/imports remain live dependencies; use cwd or explicit "
            "paths for them. Scripts run with ordinary host permissions, not in a sandbox or in the "
            "Harness process. Timeouts stop the owned process group; side effects may already exist, "
            "and no call is retried automatically. Mutations apply at the next Step boundary and "
            "emit applied/failed events; acceptance is not proof of activation. Search local_<group> "
            "with tool_search after activation/update. Removed or stale loaded definitions cannot execute.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["register", "update", "list", "get", "remove"],
                       "description": "Operation to perform; register/update replaces a complete definition."},
            "group": {"type": "string", "description": "Owning custom group name; create it with manage_tool_group first."},
            "name": {"type": "string", "description": "Tool name, required except for list. Same naming rules as groups."},
            "definition": {"type": "object", "description": "Complete tool metadata and script execution configuration, required for register/update.", "properties": {
                "description": {"type": "string"}, "input_schema": {"type": "object"},
                "execution": {"type": "object", "properties": {
                    "command": {"type": "string"}, "script_path": {"type": "string"},
                    "args": {"type": "array", "items": {"type": "string"}},
                    "cwd": {"type": "string"}, "timeout_sec": {"type": "integer"}},
                    "required": ["command", "script_path"], "additionalProperties": False}},
                "required": ["description", "input_schema", "execution"], "additionalProperties": False}},
            "required": ["action", "group"], "additionalProperties": False}}, tool))
