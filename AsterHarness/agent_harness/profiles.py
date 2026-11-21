"""Reusable host profile operations, shared by the SDK, CLI and adapters."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
try:
    import fcntl
except ImportError:
    from agent_harness import _windows_locks as fcntl
import json
import os
from pathlib import Path
import shlex
from uuid import UUID, uuid4

from .agent_config import (
    atomic_write,
    default_profile,
    harness_home,
    load_profile,
    override,
    toml_text,
)
from .core.client import transport_for
from .core.sandbox import FilesystemSandbox
from .core.rollout import RolloutRecorder
from .core.thread_manager import ThreadManager
from .core.trace import JsonlTrace


def valid_name(name):
    import re

    if not isinstance(name, str) or not re.fullmatch(
        r"[A-Za-z0-9_\-\u4e00-\u9fff]{1,80}", name
    ):
        raise ValueError(
            "Name must contain 1–80 letters, digits, Chinese characters, _ or -"
        )
    try:
        UUID(name)
    except ValueError:
        return name
    raise ValueError("A name cannot be a UUID")


def initialize_home(home):
    for name in ("agents", "credentials", "threads"):
        (home / name).mkdir(parents=True, exist_ok=True, mode=0o700)


@contextmanager
def management_lock(home):
    initialize_home(home)
    with (home / ".management.lock").open("a", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def template_path(home, identifier):
    supplied = Path(identifier).expanduser()
    if supplied.exists():
        return (supplied / "agent.toml" if supplied.is_dir() else supplied).resolve()
    return home / "agents" / valid_name(identifier) / "agent.toml"


def find_thread(home, identifier):
    try:
        thread_id = str(UUID(identifier))
        if thread_id != identifier:
            raise ValueError("Use a canonical Thread UUID")
        path = home / "threads" / thread_id
        if not (path / "rollout.jsonl").is_file():
            raise ValueError("Thread does not exist")
        return path
    except (ValueError, AttributeError):
        matches = []
        for path in (home / "threads").glob("*/thread.toml"):
            import tomllib

            data = tomllib.loads(path.read_text(encoding="utf-8"))
            if data.get("name") == identifier:
                matches.append(path.parent)
        if len(matches) != 1:
            raise ValueError("Thread name is missing or ambiguous; use its UUID")
        return matches[0]


def running(root):
    with (root / "rollout.jsonl").open("r", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True


def saved_configuration(root):
    # Read a consistent, closed rollout; callers must not use this as a writer lock.
    with (root / "rollout.jsonl").open(encoding="utf-8") as stream:
        history = RolloutRecorder._read_stream(stream, root.name)
    return history.meta


def credentials(profile):
    values = {}
    if profile.env_file and profile.env_file.exists():
        for number, line in enumerate(
            profile.env_file.read_text(encoding="utf-8-sig").splitlines(), 1
        ):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, sep, raw = line.partition("=")
            parts = shlex.split(raw, comments=True)
            if not sep or len(parts) > 1:
                raise ValueError(f"Invalid credential syntax on line {number}")
            values[key.strip()] = parts[0] if parts else ""
    return {**values, **os.environ}


def client_for(profile, trace):
    model = profile.data["model"]
    if model.get("auth") == "chatgpt":
        from .core.chatgpt_client import ChatGPTResponsesClient
        from .agent_config import aster_home
        if not model["name"].strip():
            raise ValueError("Fill model.name using aster auth models")
        return ChatGPTResponsesClient(model.get("auth_home") or aster_home(), model["name"],
            profile=model.get("auth_profile", "default"), trace=trace)
    key = credentials(profile).get(model["api_key_env"])
    if not model["name"].strip() or not key:
        raise ValueError("Fill model.name and the referenced API key before starting")
    return transport_for(model)(
        model["url"],
        model["name"],
        key,
        allow_http=model.get("allow_http", False),
        trace=trace,
    )


def private_paths(home, profile):
    return (
        home / "credentials",
        home / "agents",
        *([Path(profile.data["model"]["auth_home"]) / "credentials"]
          if profile.data["model"].get("auth_home") else []),
        *([profile.env_file] if profile.env_file else []),
    )


def check_resume(root, profile, *, probe=False):
    if running(root):
        raise ValueError("Thread is already running")
    check_identity(root.parent.parent, root, profile.data)
    meta = saved_configuration(root)
    if Path(meta["cwd"]).resolve() != profile.workspace:
        raise ValueError("Changing an existing Thread workspace is not supported")
    new = profile.configuration.filesystem_policy
    if probe and new is not None:
        FilesystemSandbox(profile.workspace, new).probe()
    return meta


def named_threads(home):
    rows = ThreadManager(home / "threads").list_threads()
    for row in rows:
        root = home / "threads" / row["thread_id"]
        path = root / "thread.toml"
        if path.exists():
            try:
                import tomllib

                row["name"] = tomllib.loads(path.read_text(encoding="utf-8")).get(
                    "name"
                )
            except (ValueError, OSError):
                row["config_error"] = True
        row["running"] = running(root) if (root / "rollout.jsonl").exists() else False
    return rows


def save_profile_copy(profile, root, name):
    data = profile.snapshot()
    data["name"] = valid_name(name)
    atomic_write(root / "instructions.md", profile.instructions())
    atomic_write(root / "thread.toml", toml_text(data))


def resume_profile(profile, root, client, *, preserve_instructions=False, **callbacks):
    """Resume a host-configured Thread; preserve existing environment baselines."""
    check_identity(root.parent.parent, root, profile.data)
    def configuration(meta):
        if Path(meta["cwd"]).resolve() != profile.workspace:
            raise ValueError("Changing an existing Thread workspace is not supported")
        if preserve_instructions:
            profile.configuration = replace(profile.configuration, base_instructions=meta["configuration"]["base_instructions"])
        return resumed_configuration(profile, meta)

    return ThreadManager(root.parent).resume(client, root.name, configuration_from_meta=configuration, **callbacks)


def resumed_configuration(profile, meta):
    old = meta["configuration"].get("filesystem_policy")
    # A host may relocate a data tree while retaining its old root as an alias.
    # Rebase only rules under an actual alias in the immutable workspace path;
    # do not relax the sandbox's rejection of arbitrary symlink policy paths.
    original_workspace = Path(meta["cwd"])
    aliases = [p for p in (original_workspace, *original_workspace.parents) if p.is_symlink()]
    if old is not None and aliases:
        old = deepcopy(old)
        for rule in old["rules"]:
            if rule["path"] == "/":
                continue
            path = original_workspace / rule["path"]
            for alias in aliases:
                if path.is_relative_to(alias):
                    rule["path"] = str(alias.resolve() / path.relative_to(alias))
                    break
    return replace(
        profile.configuration,
        filesystem_policy=old,
        # An unchanged host profile must not undo a committed live MCP update.
        # Explicit edits while paused still override the saved runtime setting.
        mcp_servers=(meta["configuration"].get("mcp_servers", {})
                     if profile.configuration.mcp_servers == meta.get("host_mcp_servers")
                     else profile.configuration.mcp_servers),
    )


def open_session(home, profile, root=None):
    manager = ThreadManager(home / "threads")
    buffered = []
    sink = None

    def trace(event, data):
        if sink is None:
            buffered.append((event, data))
        else:
            sink(event, data)

    client = client_for(profile, trace)
    callbacks = {
        "trace": trace,
        "send_message": lambda text: print("\nAgent: " + text, flush=True),
        "private_paths": private_paths(home, profile),
    }
    if root is None:
        session = manager.create(
            client,
            profile.workspace,
            configuration=profile.configuration,
            agent_instructions=profile.instructions(),
            **callbacks,
        )
    else:
        session = resume_profile(profile, root, client, **callbacks)
    sink = JsonlTrace(
        session.rollout.path.parent
        / "traces"
        / ("run-" + session.session_id + ".jsonl")
    )
    for event, data in buffered:
        sink(event, data)
    return session


async def prepare_session(session, profile):
    from .core.session.input_queue import HarnessEvent

    old = session.configuration.filesystem_policy
    new = profile.configuration.filesystem_policy
    rules = lambda policy: None if policy is None else (policy["rules"], policy.get('gpu', False), policy.get('local_ipc', False))
    if rules(old) != rules(new):
        await session.apply_step_filesystem_policy(
            [HarnessEvent("host_policy_reload", {"_filesystem_policy": new})]
        )
        if rules(session.configuration.filesystem_policy) != rules(new):
            raise ValueError(
                "Filesystem policy reload failed; previous policy remains active"
            )
    session.agent_context.pin_instructions(profile.instructions())
    session.rollout.append(
        "host_configuration",
        {"profile": profile.snapshot(), "instructions": profile.instructions(),
         "mcp_servers": profile.configuration.mcp_servers},
    )
    session.record_step_world_state_if_changed()


def profile_from_data(profile, data):
    # Keep original path anchoring without rewriting a shared source template.
    from dataclasses import replace as dc_replace

    loaded = validate_candidate(profile.path, data, profile.instructions())
    return dc_replace(loaded, instructions_path=profile.instructions_path)


def validate_candidate(path, data, instructions=None):
    """Validate a draft in memory with the original path anchor; no source edits."""
    return load_profile(path, data=data, instruction_text=instructions)


def check_identity(home, root, data):
    valid_name(data["name"])
    import tomllib

    for path in (home / "threads").glob("*/thread.toml"):
        if (
            path.parent != root
            and tomllib.loads(path.read_text(encoding="utf-8")).get("name") == data["name"]
        ):
            raise ValueError("Thread name already exists")
    profile = validate_candidate(root / "thread.toml", data)
    if profile.instructions_path != root / "instructions.md":
        raise ValueError("Thread instructions must remain instructions.md")
    # Workspace is immutable even while the session writer owns the rollout lock.
    with (root / "rollout.jsonl").open(encoding="utf-8") as stream:
        first = json.loads(stream.readline())["payload"]
    if profile.workspace != Path(first["cwd"]).resolve():
        raise ValueError("Changing workspace requires a new Thread")
