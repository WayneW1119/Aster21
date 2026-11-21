"""Versioned script definitions, prepared now and activated at Step boundaries.

The registry belongs to one Thread. Script entrypoints are copied on registration;
other files/imports and external state remain ordinary live dependencies.
"""

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from uuid import uuid4

from .runner import run_script


def name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}", value):
        raise ValueError("Names must start with a letter and contain 1-40 ASCII letters, digits, _ or -")
    return value


def text(value, field, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{field} must contain 1-{limit} characters")
    return value


def validator(schema):
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError("input_schema must be an object JSON Schema")
    if len(json.dumps(schema, allow_nan=False).encode()) > 32768:
        raise ValueError("input_schema exceeds 32 KiB")

    def check_refs(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"$ref", "$dynamicRef"} and (not isinstance(item, str) or not item.startswith("#")):
                    raise ValueError("Only local JSON Schema references are supported")
                check_refs(item)
        elif isinstance(value, list):
            for item in value:
                check_refs(item)

    check_refs(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise ValueError("Invalid input_schema: " + error.message) from None
    return Draft202012Validator(schema)


@dataclass(frozen=True)
class PreparedCustomCall:
    group: str
    _group_info: dict
    _definition: dict
    root: Path
    sandbox: object = None

    @property
    def name(self):
        return self._definition["name"]

    @property
    def namespace(self):
        return "local_" + self.group

    @property
    def callable_name(self):
        return self.name

    @property
    def spec(self):
        return {"type": "function", "name": self.namespace + "__" + self.name,
                "description": self._definition["description"]
                    + f"\n[Custom tool revision: {self._definition['version']}; group revision: {self._group_info['version']}]",
                "parameters": deepcopy(self._definition["input_schema"])}

    @property
    def loadable_spec(self):
        tool = self.spec
        # This is the loaded result, not the hidden catalog declaration. Omit
        # the optional defer_loading flag so compatible providers don't defer
        # an already discovered local function again.
        tool.update(name=self.name, strict=False)
        description = self._group_info["description"]
        if self._group_info.get("instructions"):
            description += "\n\n" + self._group_info["instructions"]
        return {"type": "namespace", "name": self.namespace, "description": description, "tools": [tool]}

    @property
    def search_text(self):
        return " ".join((self.group, self.name, self.spec["description"],
                         self.loadable_spec["description"],
                         " ".join(self._definition["input_schema"].get("properties", {}))))

    async def call(self, arguments):
        try:
            error = next(validator(self._definition["input_schema"]).iter_errors(arguments), None)
        except Exception as error:
            raise ValueError("Could not validate custom tool arguments: " + str(error)) from None
        if error is not None:
            raise ValueError("Invalid tool arguments: " + error.message)
        execution = self._definition["execution"]
        script = self.root / execution["snapshot"]
        if hashlib.sha256(script.read_bytes()).hexdigest() != execution["sha256"]:
            raise ValueError("Custom tool script snapshot changed; register an explicit update")
        result = await run_script(execution["command"], str(script), execution["args"],
                                  execution["cwd"], arguments, execution["timeout_sec"], self.sandbox)
        return {"group": self.group, "tool": self.name, "version": self._definition["version"], **result}


class CustomToolManager:
    def __init__(self, root, workspace, sandbox=None):
        self.root, self.workspace = Path(root), Path(workspace).resolve(strict=True)
        self.sandbox = sandbox
        self.path = self.root / "registry.json"
        self.state = {"format_version": 1, "revision": 0, "groups": {}}
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            if self.state.get("format_version") != 1:
                raise ValueError("Unsupported custom tool registry version")
            for group, entry in self.state["groups"].items():
                name(group)
                for tool in entry["tools"].values():
                    self.validate_prepared(tool)

    def inspect(self, group=None, tool=None):
        if group is None:
            return deepcopy(self.state)
        entry = self.state["groups"].get(name(group))
        if entry is None:
            raise ValueError("Unknown custom tool group")
        if tool is None:
            return deepcopy(entry)
        if tool not in entry["tools"]:
            raise ValueError("Unknown custom tool")
        return deepcopy(entry["tools"][tool])

    def prepare_group(self, action, group, description=None, instructions=None):
        if action not in {"create", "update", "remove"}:
            raise ValueError("Group action must be create, update, remove, list or get")
        result = {"kind": "group", "action": action, "group": name(group)}
        if action != "remove":
            result["description"] = text(description, "description", 1000)
            if instructions is not None and (not isinstance(instructions, str) or len(instructions) > 8000):
                raise ValueError("instructions must be a string of at most 8000 characters")
            result["instructions"] = instructions or ""
        return result

    def prepare_tool(self, action, group, tool, definition=None):
        if action not in {"register", "update", "remove"}:
            raise ValueError("Tool action must be register, update, remove, list or get")
        change = {"kind": "tool", "action": action, "group": name(group), "tool": name(tool)}
        if action == "remove":
            return change
        if not isinstance(definition, dict) or set(definition) != {"description", "input_schema", "execution"}:
            raise ValueError("definition requires description, input_schema and execution")
        text(definition["description"], "description", 8000)
        validator(definition["input_schema"])
        execution = definition["execution"]
        if not isinstance(execution, dict) or set(execution) - {"command", "script_path", "args", "cwd", "timeout_sec"}:
            raise ValueError("Unknown execution fields")
        source = self.workspace / text(execution.get("script_path"), "script_path", 4096)
        source = source.resolve(strict=True)
        if not source.is_relative_to(self.workspace) or not source.is_file():
            raise ValueError("script_path must be a file inside the workspace")
        if self.sandbox is not None and self.sandbox.enabled:
            import base64
            code = base64.b64decode(self.sandbox.file_operation('bytes',path=str(source),limit=1024*1024))
        else:
            with source.open("rb") as stream:
                code = stream.read(1024 * 1024 + 1)
        if len(code) > 1024 * 1024:
            raise ValueError("Script entrypoint exceeds 1 MiB")
        command = shutil.which(text(execution.get("command"), "command", 4096))
        if not command:
            raise ValueError("Execution command is not available")
        args = execution.get("args", [])
        if not isinstance(args, list) or any(not isinstance(a, str) or "\0" in a for a in args):
            raise ValueError("args must be a list of strings without NUL")
        if len(json.dumps(args)) > 32768:
            raise ValueError("args exceed 32 KiB")
        cwd = execution.get("cwd", ".")
        if not isinstance(cwd, str):
            raise ValueError("cwd must be a workspace directory path")
        cwd = (self.workspace / cwd).resolve(strict=True)
        if not cwd.is_relative_to(self.workspace) or not cwd.is_dir():
            raise ValueError("cwd must be a directory inside the workspace")
        timeout = execution.get("timeout_sec", 30)
        if type(timeout) is not int or not 1 <= timeout <= 300:
            raise ValueError("timeout_sec must be an integer from 1 to 300")
        relative = Path("revisions") / uuid4().hex / source.name
        destination = self.root / relative
        destination.parent.mkdir(parents=True, mode=0o700)
        destination.write_bytes(code)
        destination.chmod(0o400)
        change["definition"] = {"name": tool, **deepcopy(definition), "execution": {
            "command": command, "source_path": str(source), "snapshot": str(relative),
            "sha256": hashlib.sha256(code).hexdigest(), "args": args, "cwd": str(cwd), "timeout_sec": timeout}}
        return change

    def validate_prepared(self, definition):
        name(definition["name"])
        text(definition["description"], "description", 8000)
        validator(definition["input_schema"])
        execution = definition["execution"]
        snapshot = (self.root / execution["snapshot"]).resolve(strict=True)
        if not snapshot.is_relative_to(self.root.resolve()) or not snapshot.is_file():
            raise ValueError("Invalid custom tool snapshot path")
        if hashlib.sha256(snapshot.read_bytes()).hexdigest() != execution["sha256"]:
            raise ValueError("Custom tool snapshot checksum mismatch")

    def apply(self, change):
        state = deepcopy(self.state)
        groups, group, action = state["groups"], change["group"], change["action"]
        if change["kind"] == "group":
            if action == "create":
                if group in groups:
                    raise ValueError("Group already exists; use update")
                groups[group] = {"tools": {}}
            elif group not in groups:
                raise ValueError("Unknown custom tool group")
            if action == "remove":
                del groups[group]
            else:
                groups[group].update(description=change["description"], instructions=change["instructions"])
        else:
            if group not in groups:
                raise ValueError("Create the tool group before registering a tool")
            tools, tool = groups[group]["tools"], change["tool"]
            if action == "register" and tool in tools:
                raise ValueError("Tool already exists; use update")
            if action != "register" and tool not in tools:
                raise ValueError("Unknown custom tool")
            if action == "remove":
                del tools[tool]
            else:
                self.validate_prepared(change["definition"])
                tools[tool] = {**deepcopy(change["definition"]), "version": state["revision"] + 1}
        if group in groups:
            groups[group]["version"] = state["revision"] + 1
        if len(groups) > 64 or sum(len(g["tools"]) for g in groups.values()) > 256:
            raise ValueError("Custom tool registry limit: 64 groups and 256 tools")
        state["revision"] += 1
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.root / (".registry-" + uuid4().hex)
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                os.chmod(temporary, 0o600)
                json.dump(state, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)
        self.state = state
        return {"status": "applied", "revision": state["revision"], "kind": change["kind"],
                "group": group, "action": action, **({"tool": change["tool"]} if "tool" in change else {})}

    def bindings(self):
        return tuple(PreparedCustomCall(group, {key: entry[key] for key in ("description", "instructions", "version")},
                                       deepcopy(tool), self.root, self.sandbox)
            for group, entry in sorted(self.state["groups"].items())
            for _, tool in sorted(entry["tools"].items()))

    def namespaces(self):
        return {"local_" + group: f"[revision {entry['version']}; {len(entry['tools'])} tools] "
                + entry["description"]
                for group, entry in self.state["groups"].items()}
