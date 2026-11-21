"""Public, event-driven Python facade over the existing persistent runtime."""

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
import inspect
import json
from pathlib import Path
import re
import tempfile
from typing import Any
from uuid import uuid4

from .agent_config import (
    atomic_write,
    default_profile,
    harness_home,
    load_profile,
    toml_text,
)
from .core.client import ResponsesClient
from .core.model_protocol import ModelAdapter
from .core.client_common import ModelClient
from .core.context_manager.agent_context import DEFAULT_AGENT_INSTRUCTIONS
from .core.session import SessionConfiguration
from .core.session.input_queue import HarnessEvent
from .core.sandbox import FilesystemSandbox
from .core.thread_manager import ThreadManager
from .core.trace import JsonlTrace
from .profiles import (
    client_for,
    find_thread,
    initialize_home,
    management_lock,
    named_threads,
    prepare_session,
    private_paths,
    resume_profile,
    save_profile_copy,
    running,
    template_path,
    valid_name,
    validate_candidate,
)


@dataclass(frozen=True)
class ModelConfig:
    """Connection settings. Secrets are referenced, never serialized into profiles."""

    name: str
    url: str = "https://api.openai.com/v1/responses"
    api_key_env: str = "OPENAI_API_KEY"
    env_file: str | Path | None = None
    allow_http: bool = False
    auth: str = "api_key"
    auth_home: str | Path | None = None
    auth_profile: str = "default"
    protocol: str | None = None


@dataclass(frozen=True)
class Message:
    text: str
    thread_id: str
    session_id: str


class CallbackError(RuntimeError):
    """An application callback failed; the model scheduler is independent."""


class MessageStreamError(RuntimeError):
    """A message consumer exceeded its explicit queue budget."""


_END = object()


class _Messages(AsyncIterator[Message]):
    def __init__(self, agent, max_queue):
        if type(max_queue) is not int or max_queue < 1:
            raise ValueError("max_queue must be a positive integer")
        self.agent, self.limit = agent, max_queue
        self.queue = asyncio.Queue(maxsize=max_queue + 1)  # Reserved terminal slot.
        self.closed = False

    def publish(self, value):
        if self.closed:
            return
        if self.queue.qsize() >= self.limit:
            self.finish(
                MessageStreamError(
                    "Message consumer fell behind; no further messages are delivered to this subscription"
                )
            )
        else:
            self.queue.put_nowait(value)

    def finish(self, error=None):
        if not self.closed:
            self.closed = True
            self.agent._streams.discard(self)
            self.queue.put_nowait(error or _END)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed and self.queue.empty():
            raise StopAsyncIteration
        item = await self.queue.get()
        if item is _END:
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        return item

    async def aclose(self):
        if not self.closed:
            self.closed = True
            self.agent._streams.discard(self)
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queue.put_nowait(_END)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()


def _overrides(data, values):
    result = deepcopy(data)
    for key, value in (values or {}).items():
        if not isinstance(key, str) or not all(
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", p) for p in key.split(".")
        ):
            raise ValueError("Overrides must use dotted configuration keys")
        target = result
        parts = key.split(".")
        for part in parts[:-1]:
            target = target.setdefault(part, {})
            if not isinstance(target, dict):
                raise ValueError("Override crosses a scalar")
        target[parts[-1]] = deepcopy(value)
    return result


class Aster:
    """A Thread store and Agent factory. Factories do disk I/O, never model calls."""

    def __init__(self, home: str | Path | None = None):
        self.home = (
            Path(home).expanduser().resolve() if home is not None else harness_home()
        )
        initialize_home(self.home)

    def threads(self) -> list[dict]:
        return named_threads(self.home)

    def recover(self, thread: str, *, reason: str, allow_trace: bool = False) -> dict:
        """Explicitly recover a validated checkpoint or uncommitted boundary.

        Preserves identity and archive, refusing live writers, partial archives
        and unresolved tools. Legacy trace evidence requires allow_trace=True.
        No model or tool is invoked by recovery itself.
        """
        if type(allow_trace) is not bool:
            raise TypeError("allow_trace must be a boolean")
        from .core.rollout import RolloutRecorder
        root = find_thread(self.home, thread)
        load_profile(root / "thread.toml")
        return RolloutRecorder.recover(root.parent, root.name, reason, allow_trace=allow_trace)

    def templates(self) -> list[str]:
        return sorted(
            path.parent.name for path in (self.home / "agents").glob("*/agent.toml")
        )

    def from_template(
        self,
        template: str | Path,
        *,
        name: str | None = None,
        workspace: str | Path | None = None,
        overrides: Mapping[str, Any] | None = None,
        client: ModelClient | ModelAdapter | None = None,
        on_message: Callable | None = None,
        on_trace: Callable | None = None,
        trace: bool = False,
    ) -> "Agent":
        """Create a new independent Thread from a named template or template path."""
        return self.from_config(
            template_path(self.home, str(template)),
            name=name,
            workspace=workspace,
            overrides=overrides,
            client=client,
            on_message=on_message,
            on_trace=on_trace,
            trace=trace,
        )

    def from_config(
        self,
        path: str | Path,
        *,
        name: str | None = None,
        workspace: str | Path | None = None,
        overrides: Mapping[str, Any] | None = None,
        client: ModelClient | ModelAdapter | None = None,
        on_message: Callable | None = None,
        on_trace: Callable | None = None,
        trace: bool = False,
    ) -> "Agent":
        source = load_profile(Path(path))
        data = _overrides(source.data, overrides)
        if workspace is not None:
            data["workspace"] = str(Path(workspace).expanduser().resolve())
        profile = validate_candidate(source.path, data)
        return self._create(profile, name, client, on_message, on_trace, trace)

    def create(
        self,
        workspace: str | Path,
        *,
        model: ModelConfig | None = None,
        client: ModelClient | ModelAdapter | None = None,
        configuration: SessionConfiguration | None = None,
        instructions: str = DEFAULT_AGENT_INSTRUCTIONS,
        name: str | None = None,
        on_message: Callable | None = None,
        on_trace: Callable | None = None,
        trace: bool = False,
    ) -> "Agent":
        """Create without a TOML file. Injected clients must be supplied again on resume."""
        if model is None and client is None:
            raise ValueError("Provide model settings or a ModelClient")
        workspace = Path(workspace).expanduser().resolve(strict=True)
        settings = configuration or SessionConfiguration(
            max_consecutive_steps=None, filesystem_policy={}
        )
        data = default_profile(workspace, self.home)
        connection = (
            asdict(model)
            if model is not None
            else {
                "name": "",
                "url": "https://api.openai.com/v1/responses",
                "api_key_env": "OPENAI_API_KEY",
            }
        )
        if connection.get("protocol") is None:
            connection.pop("protocol", None)
        if connection.get("env_file") is not None:
            connection["env_file"] = str(
                Path(connection["env_file"]).expanduser().resolve()
            )
        else:
            connection.pop("env_file", None)
        if connection.get("auth_home") is not None:
            connection["auth_home"] = str(Path(connection["auth_home"]).expanduser().resolve())
        else:
            connection.pop("auth_home", None)
        data["model"] = connection
        data["runtime"]["max_consecutive_steps"] = (
            settings.max_consecutive_steps or "unlimited"
        )
        data["context"] = {
            "warning_tokens": settings.context_budget_warning_tokens or 0,
            "tool_output_tokens": settings.tool_output_token_limit or "unlimited",
        }
        data["tools"]["code_mode"] = settings.code_mode_enabled
        data["skills"]["workspace"] = settings.workspace_skills
        data["skills"]["directories"] = [
            str((workspace / p).absolute()) for p in settings.skill_directories
        ]
        if settings.skill_sources:
            data["skills"]["sources"] = [
                {**source, "path": str((workspace / source["path"]).absolute())}
                for source in settings.skill_sources]
        data["sandbox"] = {
            "enabled": settings.filesystem_policy is not None,
            "format": 2,
            **({key: True for key in ('gpu', 'local_ipc') if (settings.filesystem_policy or {}).get(key)}),
            "rules": [
                {
                    "path": "/" if entry["path"] == "/" else str((workspace / entry["path"]).absolute()),
                    "access": entry["access"],
                }
                for entry in (settings.filesystem_policy or {}).get("rules", [])
            ],
        }
        with tempfile.TemporaryDirectory(
            prefix=".sdk-profile-", dir=self.home / "threads"
        ) as temporary:
            folder = Path(temporary)
            if settings.mcp_servers:

                def clean(value):
                    if isinstance(value, dict):
                        return {k: clean(v) for k, v in value.items() if v is not None}
                    if isinstance(value, (tuple, list)):
                        return [clean(v) for v in value]
                    return value

                atomic_write(
                    folder / "mcp.toml",
                    toml_text({"mcp_servers": clean(settings.mcp_servers)}),
                )
                data["mcp"] = {"config": "mcp.toml"}
            atomic_write(folder / "instructions.md", instructions)
            atomic_write(folder / "agent.toml", toml_text(data))
            profile = load_profile(folder / "agent.toml")
            profile.configuration = replace(
                profile.configuration, base_instructions=settings.base_instructions
            )
            return self._create(
                profile, name, client, on_message, on_trace, trace, copy_mcp=True
            )

    def _create(
        self, profile, name, client, on_message, on_trace, trace, copy_mcp=False
    ):
        if type(trace) is not bool:
            raise ValueError("trace must be a boolean")
        with management_lock(self.home):
            name = valid_name(name or profile.data["name"] + "-" + uuid4().hex[:8])
            if any(row.get("name") == name for row in self.threads()):
                raise ValueError("Thread name already exists")
            if profile.configuration.filesystem_policy is not None:
                FilesystemSandbox(
                    profile.workspace, profile.configuration.filesystem_policy
                ).probe()
            agent = Agent(self, profile, client, on_message, on_trace, trace)
            session = ThreadManager(self.home / "threads").create(
                agent._client(),
                profile.workspace,
                configuration=profile.configuration,
                agent_instructions=profile.instructions(),
                **agent._callbacks(),
            )
            try:
                root = session.rollout.path.parent
                if copy_mcp and profile.data.get("mcp", {}).get("config"):
                    atomic_write(
                        root / "mcp.toml",
                        profile.resolve(profile.data["mcp"]["config"]).read_text(encoding="utf-8"),
                    )
                    profile.data["mcp"]["config"] = str(root / "mcp.toml")
                save_profile_copy(profile, root, name)
                agent._attach(session, load_profile(root / "thread.toml"))
                # Programmatic base instructions belong to the saved Session configuration.
                agent._profile.configuration = replace(
                    agent._profile.configuration,
                    base_instructions=profile.configuration.base_instructions,
                )
            except BaseException:
                session.rollout.close()
                raise
            return agent

    def resume(
        self,
        thread: str,
        *,
        client: ModelClient | ModelAdapter | None = None,
        on_message: Callable | None = None,
        on_trace: Callable | None = None,
        trace: bool = False,
    ) -> "Agent":
        root = find_thread(self.home, thread)
        if running(root):
            raise ValueError("Thread is already running")
        profile = load_profile(root / "thread.toml")
        agent = Agent(self, profile, client, on_message, on_trace, trace)
        session = resume_profile(profile, root, agent._client(), preserve_instructions=True, **agent._callbacks())
        agent._attach(session, profile)
        return agent


class Agent:
    """One live handle to one persistent Thread; close it before obtaining another."""

    def __init__(self, harness, profile, client, on_message, on_trace, trace):
        if type(trace) is not bool:
            raise ValueError("trace must be a boolean")
        for callback in (on_message, on_trace):
            if callback is not None and not callable(callback):
                raise TypeError("Callbacks must be callable")
        self._harness, self._profile, self._injected_client = harness, profile, client
        self._on_message, self._on_trace = on_message, on_trace
        self._trace_enabled, self._trace_sink = trace, None
        self._early_traces = []
        self._loop = None
        self._worker = self._callback_worker = None
        self._ready = asyncio.Event()
        self._callback_queue = asyncio.Queue()
        self._callback_errors = deque(maxlen=64)
        self._streams = set()
        self._closed = False
        self._guard = asyncio.Lock()
        self.trace_path = None

    def _client(self):
        if self._injected_client is None:
            return client_for(self._profile, self._trace)
        if isinstance(self._injected_client, ResponsesClient):
            client = self._injected_client.for_step()
            client.trace = self._trace
            return client
        return self._injected_client

    def _callbacks(self):
        return {
            "trace": self._trace,
            "send_message": self._message,
            "private_paths": private_paths(self._harness.home, self._profile),
        }

    def _attach(self, session, profile):
        self._session, self._profile = session, profile
        if self._trace_enabled:
            self.trace_path = (
                self.thread_path / "traces" / ("sdk-" + session.session_id + ".jsonl")
            )
            self._trace_sink = JsonlTrace(self.trace_path)
            for event, data in self._early_traces:
                self._trace_sink(event, data)
        self._early_traces.clear()

    @property
    def thread_id(self) -> str:
        return self._session.thread_id

    @property
    def session_id(self) -> str:
        return self._session.session_id

    @property
    def workspace(self) -> Path:
        return self._profile.workspace

    @property
    def thread_path(self) -> Path:
        return self._session.rollout.path.parent

    @property
    def config_path(self) -> Path:
        return self.thread_path / "thread.toml"

    @property
    def instructions_path(self) -> Path:
        return self._session.agent_context.instructions_path

    @property
    def status(self) -> str:
        return self._session.state.status

    @property
    def mcp_status(self) -> dict:
        """Last connection/discovery result, not a provider health probe.

        Read after start or an update receipt. Returns fresh containers without
        connection settings, credentials or upstream error bodies.
        """
        manager = self._session.services.mcp
        return {"servers": sorted(dict(manager.binding.servers)),
                "unavailable": sorted(failure["server"] for failure in manager.failures)}

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def callback_errors(self) -> tuple[Exception, ...]:
        return tuple(self._callback_errors)

    def _on_loop(self, callback, *args):
        if self._loop is None:
            callback(*args)
            return
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is self._loop:
            callback(*args)
        else:
            self._loop.call_soon_threadsafe(callback, *args)

    def _trace(self, event, data):
        if self._trace_sink is not None:
            self._trace_sink(event, data)
        elif self._trace_enabled:
            self._early_traces.append((event, deepcopy(data)))
        if event in ("session_waiting", "step_started"):
            self._on_loop(self._ready.set)
        if self._on_trace is not None:
            self._on_loop(
                self._callback_queue.put_nowait,
                (self._on_trace, (event, deepcopy(data))),
            )

    def _message(self, text):
        def deliver():
            message = Message(text, self.thread_id, self._session.session_id)
            for stream in tuple(self._streams):
                stream.publish(message)
            if self._on_message is not None:
                self._callback_queue.put_nowait((self._on_message, (text,)))

        self._on_loop(deliver)

    async def _dispatch_callbacks(self):
        while True:
            item = await self._callback_queue.get()
            try:
                if item is _END:
                    return
                callback, args = item
                try:
                    value = callback(*args)
                    if inspect.isawaitable(value):
                        await value
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                    error = CallbackError("Application callback was cancelled")
                    self._callback_errors.append(error)
                    self._loop.call_exception_handler(
                        {"message": "Agent SDK callback failed", "exception": error}
                    )
                except Exception as error:
                    self._callback_errors.append(error)
                    self._loop.call_exception_handler(
                        {"message": "Agent SDK callback failed", "exception": error}
                    )
            finally:
                self._callback_queue.task_done()

    def _check(self):
        if self._closed:
            raise RuntimeError(
                "Agent is closed; use Harness.resume to obtain a new handle"
            )
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("Use an Agent handle on a single asyncio event loop")
        self._loop = loop
        if self._worker is not None and self._worker.done():
            self._worker.result()
            raise RuntimeError("Agent scheduler has stopped")

    async def start(self) -> "Agent":
        if asyncio.current_task() is self._callback_worker:
            raise RuntimeError("Start the Agent from its owner task, not its callback")
        self._check()
        async with self._guard:
            self._check()
            if self._worker is not None:
                return self
            self._callback_worker = asyncio.create_task(self._dispatch_callbacks())
            try:
                await prepare_session(self._session, self._profile)
                self._worker = asyncio.create_task(self._session.run())
                self._worker.add_done_callback(self._scheduler_done)
                ready = asyncio.create_task(self._ready.wait())
                try:
                    done, _ = await asyncio.wait(
                        {ready, self._worker}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if self._worker in done:
                        await self._worker
                finally:
                    ready.cancel()
                    await asyncio.gather(ready, return_exceptions=True)
            except BaseException:
                await self._close_locked()
                raise
        return self

    def _scheduler_done(self, task):
        error = asyncio.CancelledError() if task.cancelled() else task.exception()
        if error is not None:
            for stream in tuple(self._streams):
                stream.finish(error)

    async def send(self, text: str, *, images: list[dict] | None = None) -> None:
        self._check()
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        await self._session.post_user_message(text, images)

    async def set_filesystem_policy(self, policy: dict | None) -> str:
        """Queue a host-only sandbox change at the next Step boundary.

        This changes the running Session; profile-based hosts also persist
        their desired policy in thread.toml for future resumes.
        Returns a durable request id. Completion is reported by the runtime's
        filesystem_policy_applied/failed event with the same request_id.
        """
        self._check()
        return await self._session.set_filesystem_policy(policy)

    def list_suites(self):
        """Installed suites and their desired status, without connection parameters."""
        return {key: item["enabled"] for key, item in self._session.capabilities.state["suites"].items()}

    async def _change_capabilities(self, operation):
        self._check()
        async with self._guard, self._session.capability_boundary:
            self._check()
            await self._session.capabilities.change(operation, offline=self._worker is None)
            self._session._rebuild_deferred_router()

    async def configure_suites(self, installations, *, allow_connect=None, owner=None, grants=None):
        """Apply a host's named installations together, preserving unrelated suites."""
        from copy import deepcopy
        def configure(state):
            suites = state["suites"]
            names = [spec["id"] for spec, _ in installations]
            if len(names) != len(set(names)):
                raise ValueError("Duplicate suite installation")
            if owner is not None:
                for key, item in list(suites.items()):
                    if key in names and item.get("owner") != owner:
                        raise ValueError("Installation belongs to another owner: " + key)
                    if key not in names and item.get("owner") == owner:
                        del suites[key]
            if grants is not None:
                state["grants"] = list(grants)
            for manifest, enabled in installations:
                if type(enabled) is not bool:
                    raise ValueError("enabled must be boolean")
                state["suites"][manifest["id"]] = {"manifest": deepcopy(manifest), "enabled": enabled, "owner": owner}
            if allow_connect is not None:
                if type(allow_connect) is not bool:
                    raise ValueError("allow_connect must be boolean")
                state["allow_connect"] = allow_connect
        await self._change_capabilities(configure)

    async def install_suite(self, manifest, *, parameters=None, enabled=True):
        from copy import deepcopy
        from .core.suites import read_manifest, identifier
        spec = deepcopy(manifest) if isinstance(manifest, dict) else read_manifest(manifest, parameters or {})
        key = identifier(spec["id"])
        if type(enabled) is not bool:
            raise ValueError("enabled must be boolean")
        def install(state):
            if key in state["suites"]:
                raise ValueError("Suite already installed: " + key)
            state["suites"][key] = {"manifest": spec, "enabled": enabled}
        await self._change_capabilities(install)

    async def set_suite_enabled(self, name, enabled):
        if type(enabled) is not bool:
            raise ValueError("enabled must be boolean")
        await self._change_capabilities(lambda state: state["suites"][name].update(enabled=enabled))

    async def remove_suite(self, name):
        await self._change_capabilities(lambda state: state["suites"].pop(name))

    async def configure_capabilities(self, *, grants=(), allow_connect=False, credentials=None):
        """Authorize suite requirements and optional agent HTTP connections for this Thread."""
        from .core.mcp.config import parse_servers
        if type(allow_connect) is not bool:
            raise ValueError("allow_connect must be boolean")
        credentials = credentials or {}
        for ref in credentials.values():
            parse_servers({"credential": {"url": ref["url"], "bearer_token_env_var": ref["env"]}})
        def authorize(state):
            allowed = {(ref["url"], ref["env"]) for ref in credentials.values()}
            for name, spec in list(state["connections"].items()):
                if "bearer_token_env_var" in spec and (spec["url"], spec["bearer_token_env_var"]) not in allowed:
                    del state["connections"][name]
            state.update(grants=list(grants), allow_connect=allow_connect, credentials=credentials)
        await self._change_capabilities(authorize)

    async def set_mcp_servers(self, servers: dict) -> str:
        """Queue a full MCP replacement at the next Step boundary.

        Applied settings survive resume unless the host edits the MCP profile
        while paused. Trace receipts identify request_id and superseded_request_ids;
        applied receipts also list unavailable optional servers. Acceptance alone
        is not proof that a server connected.
        """
        self._check()
        return await self._session.set_mcp_servers(servers)

    async def emit(self, kind: str, data: Any) -> str:
        self._check()
        if not isinstance(kind, str) or not kind or len(kind) > 128:
            raise ValueError(
                "Event kind must be a nonempty string of at most 128 characters"
            )
        body = json.dumps(
            {"source": "host", "kind": kind, "data": data},
            ensure_ascii=False,
            allow_nan=False,
        )
        event = HarnessEvent(
            kind,
            {
                "type": "message",
                "role": "user",
                "text": "<harness_event>\n" + body + "\n</harness_event>",
            },
        )
        await self._session.enqueue_event(event)
        return event.event_id

    def messages(self, *, max_queue: int = 256) -> _Messages:
        if self._closed:
            raise RuntimeError("Agent is closed")
        stream = _Messages(self, max_queue)
        self._streams.add(stream)
        return stream

    async def wait(self) -> None:
        if self._worker is None:
            raise RuntimeError("Start the Agent first")
        await asyncio.shield(self._worker)

    async def flush_callbacks(self) -> None:
        if asyncio.current_task() is self._callback_worker:
            raise RuntimeError(
                "Do not await callback completion from inside a callback"
            )
        if self._callback_worker is not None:
            await self._callback_queue.join()
        if self._callback_errors:
            raise CallbackError(
                "An Agent callback failed; inspect callback_errors"
            ) from self._callback_errors[0]

    async def close(self) -> None:
        if self._loop is not None and asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("Use an Agent handle on a single asyncio event loop")
        if asyncio.current_task() is self._callback_worker:
            raise RuntimeError("Close the Agent from its owner task, not its callback")
        async with self._guard:
            await self._close_locked()

    async def _close_locked(self) -> None:
        if self._closed:
            return
        self._closed = True
        failure = None
        try:
            await self._session.suspend()
            if self._worker is not None:
                await self._worker
        except BaseException as error:
            failure = error
        finally:
            self._session.rollout.close()
            if self._callback_worker is not None:
                await self._callback_queue.join()
                self._callback_queue.put_nowait(_END)
                await self._callback_worker
            for stream in tuple(self._streams):
                stream.finish(failure)
        if failure is not None:
            raise failure
        await self.flush_callbacks()

    async def pause(self) -> None:
        await self.close()

    async def __aenter__(self) -> "Agent":
        return await self.start()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()


Harness = Aster  # Previous public name remains import-compatible.
