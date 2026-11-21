"""Human-facing profiles and per-Thread configuration, independent of agent tools."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import shlex
import subprocess
from uuid import uuid4

from .agent_config import (
    atomic_write,
    default_profile,
    harness_home,
    load_profile,
    override,
    toml_text,
)
from .core.context_manager.agent_context import DEFAULT_AGENT_INSTRUCTIONS
from .core.sandbox import FilesystemSandbox


from . import profiles as _profiles
from .profiles import (
    valid_name,
    initialize_home,
    management_lock,
    template_path,
    find_thread,
    running,
    saved_configuration,
    credentials,
    client_for,
    private_paths,
    check_resume,
    named_threads,
    save_profile_copy,
    resume_profile,
    open_session,
    prepare_session,
    profile_from_data,
    validate_candidate,
    check_identity,
)


async def managed_run(home, root, session, args):
    from .__main__ import StdinReader, run_cli

    reader = StdinReader() if args.prompt is None else None
    pending = load_profile(root / "thread.toml")
    applied_request = None
    accepted_request = None
    while True:
        request_path = root / "reload.request.json"
        runtime_path = root / "runtime.json"
        baseline = (
            json.dumps({"id": applied_request})
            if applied_request is not None
            else request_path.read_text(encoding="utf-8") if request_path.exists() else None
        )
        atomic_write(
            runtime_path,
            json.dumps({"pid": os.getpid(), "session_id": session.session_id}),
        )

        async def wait_reload():
            nonlocal pending, accepted_request
            observed = baseline
            while True:
                await asyncio.sleep(0.25)
                current = request_path.read_text(encoding="utf-8") if request_path.exists() else None
                if current == observed:
                    continue
                observed = current
                request = {}
                try:
                    request = json.loads(current)
                    candidate = load_profile(root / "thread.toml")
                    check_identity(home, root, candidate.data)
                    if candidate.workspace != session.process_manager.workspace:
                        raise ValueError("Changing workspace requires a new Thread")
                    _profiles.client_for(candidate, lambda *_: None)
                    if candidate.configuration.filesystem_policy is not None:
                        await asyncio.to_thread(
                            FilesystemSandbox(
                                candidate.workspace,
                                candidate.configuration.filesystem_policy,
                            ).probe
                        )
                    pending = candidate
                    accepted_request = request["id"]
                    atomic_write(
                        root / "reload.status.json",
                        json.dumps({"request_id": request["id"], "status": "applying"}),
                    )
                    return
                except (
                    OSError,
                    ValueError,
                    RuntimeError,
                    KeyError,
                    TypeError,
                ) as error:
                    atomic_write(
                        root / "reload.status.json",
                        json.dumps(
                            {
                                "request_id": request.get("id"),
                                "status": "rejected",
                                "error": str(error),
                            }
                        ),
                    )
                    print("Reload rejected: " + str(error), flush=True)
                    # Another explicit request is required; file edits alone do not apply.

        try:
            await prepare_session(session, pending)
            if applied_request is not None:
                atomic_write(
                    root / "reload.status.json",
                    json.dumps(
                        {
                            "request_id": applied_request,
                            "status": "applied",
                            "session_id": session.session_id,
                        }
                    ),
                )
                print("Configuration reloaded; resumed the same Thread.", flush=True)
            options = argparse.Namespace(
                prompt=args.prompt,
                demo=False,
                stdin_reader=reader,
                wait_reload=wait_reload if args.prompt is None else None,
            )
            print(
                f'Thread: {root.name}\nConfig: {root / "thread.toml"}\nInstructions: {root / "instructions.md"}',
                flush=True,
            )
            reload_requested = await run_cli(session, options)
        finally:
            if not session.rollout.closed:
                await session.suspend()
            session.rollout.close()
            runtime_path.unlink(missing_ok=True)
        if not reload_requested:
            return
        applied_request = accepted_request
        session = open_session(home, pending, root)


def edit_file(path):
    command = shlex.split(
        os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
    )
    subprocess.run([*command, str(path)], check=True)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Manage Aster templates and persistent Threads in ASTER_HOME"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    agent = commands.add_parser("agent").add_subparsers(dest="operation", required=True)
    init = agent.add_parser("init")
    init.add_argument("name")
    init.add_argument("--from", dest="source")
    init.add_argument("--workspace", type=Path, default=None)
    init.add_argument("--set", action="append", default=[])
    agent.add_parser("list")
    start = commands.add_parser("start")
    start.add_argument("--agent")
    start.add_argument("--name")
    start.add_argument("--workspace", type=Path)
    start.add_argument("--set", action="append", default=[])
    start.add_argument("--prompt")
    resume = commands.add_parser("resume")
    resume.add_argument("thread")
    resume.add_argument("--set", action="append", default=[])
    resume.add_argument("--prompt")
    suite = commands.add_parser("suite").add_subparsers(dest="operation", required=True)
    for action in ("list", "install", "enable", "disable", "remove"):
        command = suite.add_parser(action)
        command.add_argument("thread")
        if action != "list":
            command.add_argument("target")
        if action == "install":
            command.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")
    commands.add_parser("threads")
    for command in ("status", "reload"):
        sub = commands.add_parser(command)
        sub.add_argument("thread")
    for command in ("config", "instructions"):
        subs = commands.add_parser(command).add_subparsers(
            dest="operation", required=True
        )
        for operation in (
            ("show", "check", "edit", "set")
            if command == "config"
            else ("show", "edit")
        ):
            sub = subs.add_parser(operation)
            sub.add_argument("target")
            sub.add_argument("--agent", action="store_true")
            if operation == "set":
                sub.add_argument("entries", nargs="+")
    args = parser.parse_args(argv)
    home = harness_home()
    initialize_home(home)
    try:
        if args.command == "suite":
            if args.operation == "list":
                path = find_thread(home, args.thread) / "capabilities.json"
                suites = json.loads(path.read_text(encoding="utf-8"))["suites"] if path.exists() else {}
                print(json.dumps({key: item["enabled"] for key, item in suites.items()}))
                return
            from .sdk import Harness
            from .core.client import ScriptedClient
            async def manage_suite():
                agent = Harness(home).resume(args.thread, client=ScriptedClient([]))
                try:
                    if args.operation == "install":
                        params = dict(pair.split("=", 1) for pair in args.param)
                        await agent.install_suite(args.target, parameters=params)
                    elif args.operation == "remove":
                        await agent.remove_suite(args.target)
                    elif args.operation in {"enable", "disable"}:
                        await agent.set_suite_enabled(args.target, args.operation == "enable")
                    print(json.dumps(agent.list_suites(), ensure_ascii=False))
                finally:
                    await agent.close()
            asyncio.run(manage_suite())
            return
        if args.command == "agent":
            if args.operation == "list":
                print(
                    "\n".join(
                        p.parent.name
                        for p in sorted((home / "agents").glob("*/agent.toml"))
                    )
                )
                return
            with management_lock(home):
                dest = home / "agents" / valid_name(args.name)
                if dest.exists():
                    raise ValueError("Agent template already exists")
                if args.source:
                    source = load_profile(template_path(home, args.source))
                    data, instructions = source.snapshot(), source.instructions()
                else:
                    data = default_profile(args.workspace or Path.cwd(), home)
                    instructions = DEFAULT_AGENT_INSTRUCTIONS
                data["name"] = args.name
                if args.workspace:
                    data["workspace"] = str(args.workspace.resolve())
                data = override(data, args.set)
                if any(entry.partition("=")[0] == "instructions" for entry in args.set):
                    guide = (dest / Path(data["instructions"]).expanduser()).absolute()
                    instructions = guide.read_text(encoding="utf-8")
                data["instructions"] = "instructions.md"
                # Validate before creating the template directory.
                try:
                    validate_candidate(dest / "agent.toml", data, instructions)
                except BaseException:
                    if dest.exists() and not any(dest.iterdir()):
                        dest.rmdir()
                    raise
                atomic_write(dest / "instructions.md", instructions)
                atomic_write(dest / "agent.toml", toml_text(data))
            print(dest)
            return
        if args.command == "threads":
            print(json.dumps(named_threads(home), ensure_ascii=False, indent=2))
            return
        if args.command in ("config", "instructions"):
            path = (
                template_path(home, args.target)
                if args.agent
                else find_thread(home, args.target) / "thread.toml"
            )
            if args.command == "instructions":
                profile = load_profile(path)
                if args.operation == "show":
                    print(profile.instructions(), end="")
                else:
                    edit_file(profile.instructions_path)
                return
            if args.operation == "show":
                print(path.read_text(encoding="utf-8"), end="")
                return
            if args.operation == "edit":
                edit_file(path)
            if args.operation == "set":
                import tomllib

                with management_lock(home):
                    data = override(tomllib.loads(path.read_text(encoding="utf-8")), args.entries)
                    validate_candidate(path, data)
                    if not args.agent:
                        check_identity(home, path.parent, data)
                    atomic_write(path, toml_text(data))
            profile = load_profile(path)
            if not args.agent:
                check_identity(home, path.parent, profile.data)
            print("Valid: " + str(path))
            return
        if args.command in ("status", "reload"):
            root = find_thread(home, args.thread)
            if args.command == "status":
                state = {
                    "thread_id": root.name,
                    "running": running(root),
                    "config": str(root / "thread.toml"),
                }
                status = root / "reload.status.json"
                if status.exists():
                    state["reload"] = json.loads(status.read_text(encoding="utf-8"))
                print(json.dumps(state, ensure_ascii=False, indent=2))
                return
            if not running(root) or not (root / "runtime.json").exists():
                raise ValueError(
                    "Thread is not running under the managed CLI; use resume to apply configuration"
                )
            profile = load_profile(root / "thread.toml")
            check_identity(home, root, profile.data)
            request_id = uuid4().hex
            atomic_write(root / "reload.request.json", json.dumps({"id": request_id}))
            print(
                "Reload requested: " + request_id + "; use status to inspect the result"
            )
            return
        if args.command == "start":
            with management_lock(home):
                if args.agent:
                    profile = load_profile(template_path(home, args.agent), args.set)
                else:
                    # Default profile is materialized as a reusable default template.
                    path = home / "agents" / "default" / "agent.toml"
                    if not path.exists():
                        atomic_write(
                            path.parent / "instructions.md", DEFAULT_AGENT_INSTRUCTIONS
                        )
                        atomic_write(
                            path,
                            toml_text(
                                default_profile(args.workspace or Path.cwd(), home)
                            ),
                        )
                    profile = load_profile(path, args.set)
                if args.workspace:
                    data = profile.snapshot()
                    data["workspace"] = str(args.workspace.resolve())
                    profile = profile_from_data(profile, data)
                name = args.name or profile.data["name"] + "-" + uuid4().hex[:8]
                valid_name(name)
                if any(row.get("name") == name for row in named_threads(home)):
                    raise ValueError("Thread name already exists")
                if profile.configuration.filesystem_policy is not None:
                    FilesystemSandbox(
                        profile.workspace, profile.configuration.filesystem_policy
                    ).probe()
                session = open_session(home, profile)
                root = session.rollout.path.parent
                try:
                    save_profile_copy(profile, root, name)
                except BaseException:
                    asyncio.run(session.close())
                    raise
            asyncio.run(managed_run(home, root, session, args))
            return
        root = find_thread(home, args.thread)
        if args.set:
            import tomllib

            with management_lock(home):
                data = override(
                    tomllib.loads((root / "thread.toml").read_text(encoding="utf-8")), args.set
                )
                validate_candidate(root / "thread.toml", data)
                check_identity(home, root, data)
                if running(root):
                    raise ValueError("Thread is already running")
                atomic_write(root / "thread.toml", toml_text(data))
        profile = load_profile(root / "thread.toml")
        check_resume(root, profile, probe=True)
        session = open_session(home, profile, root)
        asyncio.run(managed_run(home, root, session, args))
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.CalledProcessError,
        KeyError,
        TypeError,
    ) as error:
        parser.exit(1, "Error: " + str(error) + "\n")
    except KeyboardInterrupt:
        parser.exit(130, "Interrupted\n")
