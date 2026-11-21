"""A live Session with an optional persistent Thread and one step scheduler."""

import asyncio
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from ..model_protocol import ModelAdapter
from ..client_common import ModelClient
from ..context_manager.agent_context import AgentContext
from ..context_manager.memory import MemoryProjection
from ..context_manager.mcp import McpProjection
from ..context.world_state.tools import ToolsState, bounded_namespaces
from ..context_manager.navigation import HistoryNavigator
from ..context.world_state import WorldState
from ..context.world_state.memory import MemoryState
from ..context.world_state.skills import SkillsState
from ..skills import validate_directories
from ..skills.service import SkillService, validate_sources
from ..sandbox import FilesystemSandbox, parse_policy
from ..context.world_state.generators import GeneratorsState
from ..context.world_state.environment import EnvironmentState
from ..event_generators import EventGeneratorManager
from ..rollout import RolloutRecorder, ResumedHistory
from ..state import SessionState
from ..tools.registry import ToolRegistry
from ..tools.runtime import ToolRuntime
from ..tools.mcp import append_mcp_tools
from ..tools.custom import append_custom_tools
from ..tools.discovery import DeferredBinding, append_search_tool
from ..custom_tools import CustomToolManager
from ..images import ImageStore
from ..tools.output_budget import ToolOutputStore, DEFAULT_TOOL_OUTPUT_TOKENS, validate_limit
from ..tools.router import ToolRouter
from ..mcp import McpConnectionManager, parse_servers
from ..unified_exec import UnifiedExecProcessManager
from .input_queue import HarnessEvent, InputQueue
from .context_budget import ContextBudget
from .services import SessionServices
from .step import run_step, ModelRequestPaused
from .tool_setup import build_tool_router


DEFAULT_BASE_INSTRUCTIONS = (Path(__file__).resolve().parents[1] / "prompt.md").read_text(encoding="utf-8")
PERSISTENCE_INSTRUCTIONS = Path(__file__).with_name("runtime_instructions.md").read_text(encoding="utf-8")
CHANNEL_INSTRUCTIONS = Path(__file__).with_name("channel_instructions.md").read_text(encoding="utf-8")
EVENT_INSTRUCTIONS = Path(__file__).with_name("event_instructions.md").read_text(encoding="utf-8")


@dataclass(frozen=True)
class SessionConfiguration:
    base_instructions: str = DEFAULT_BASE_INSTRUCTIONS
    max_consecutive_steps: int | None = 12
    context_budget_warning_tokens: int | None = 100_000
    mcp_servers: dict = field(default_factory=dict)
    tool_output_token_limit: int | None = DEFAULT_TOOL_OUTPUT_TOKENS
    code_mode_enabled: bool = False
    workspace_skills: bool = True
    skill_directories: tuple[str, ...] = ()
    skill_sources: tuple[dict, ...] = ()
    filesystem_policy: dict | None = None

    def __post_init__(self) -> None:
        if type(self.workspace_skills) is not bool:
            raise ValueError("workspace_skills must be a boolean")
        validate_limit(self.tool_output_token_limit)
        object.__setattr__(self,"filesystem_policy",parse_policy(self.filesystem_policy))
        object.__setattr__(self, "skill_directories", validate_directories(self.skill_directories))
        object.__setattr__(self, "skill_sources", validate_sources(self.skill_sources))
        if type(self.code_mode_enabled) is not bool:
            raise ValueError("code_mode_enabled must be a boolean")
        if self.max_consecutive_steps is not None and (
                type(self.max_consecutive_steps) is not int or self.max_consecutive_steps < 1):
            raise ValueError("max_consecutive_steps must be positive or None for no limit")
        if self.context_budget_warning_tokens is not None and (
                type(self.context_budget_warning_tokens) is not int
                or self.context_budget_warning_tokens < 1):
            raise ValueError("context_budget_warning_tokens must be positive or None to disable")
        object.__setattr__(self, "mcp_servers", {
            name: config.to_dict() for name, config in parse_servers(self.mcp_servers).items()})


class Session:
    def __init__(self, client: ModelClient | ModelAdapter, registry: ToolRegistry,
                 configuration: SessionConfiguration | None = None,
                 trace: Callable[[str, dict], None] | None = None,
                 send_message: Callable[[str], None] | None = None,
                 workspace: Path | None = None,
                 rollout: RolloutRecorder | None = None,
                 thread_id: str | None = None,
                 agent_context: AgentContext | None = None,
                 resumed_history: ResumedHistory | None = None,
                 private_paths: tuple[Path, ...] = ()) -> None:
        self.session_id = str(uuid4())  # New runtime identity on every resume.
        self.thread_id = thread_id
        self.rollout = rollout
        self.agent_context = agent_context
        if (rollout is None) != (thread_id is None):
            raise ValueError("Persistent Sessions need both rollout and thread_id")
        if rollout is not None:
            if rollout.attached:
                raise ValueError("Rollout already attached to a Session")
            rollout.attached = True
        self.configuration = configuration or SessionConfiguration()
        saved_mcp = resumed_history.mcp_context if resumed_history is not None else None
        self.mcp_projection = (McpProjection.from_dict(saved_mcp) if saved_mcp is not None
                               else McpProjection() if self.configuration.mcp_servers else None)
        self._mcp_has_snapshot = saved_mcp is not None
        self._world_state_sections = {}
        self._saved_section_states = (resumed_history.section_states if resumed_history is not None else {})
        self.state = SessionState()
        if resumed_history is not None and resumed_history.history is not None:
            self.state.history = resumed_history.history
        self.tool_runtime = ToolRuntime(self)
        self._compact_boundary = None
        self.context_budget = ContextBudget(
            resumed_history.context_budget if resumed_history is not None else None)
        self.memory = None
        if agent_context is not None:
            saved = resumed_history.memory if resumed_history is not None else None
            if saved is not None:
                self.memory = MemoryProjection.from_dict(saved)
                if self.memory.current["text"] != agent_context.read_memory("index.md"):
                    raise RuntimeError("Memory invariant failed on resume: disk differs from saved memory")
                # Validate the next drain's evidence before appending any new records.
                # Queued receipts are not exposed early in a model request.
                self.memory.validate(self.state.history.snapshot()
                    + [event["item"] for event in resumed_history.pending_events], agent_context)
            else:
                # Legacy migration is a one-time rebase, not a rewrite of archived items.
                legacy_items = self.state.history.snapshot()
                pending_items = ([event["item"] for event in resumed_history.pending_events]
                                 if resumed_history is not None else [])
                self.memory = MemoryProjection.migrate(legacy_items + pending_items, agent_context)
                hidden = [identifier for identifier in self.state.history.ids()
                          if MemoryState.matches_retained_fragment(self.state.history.item(identifier))]
                if self.rollout is not None:
                    self.rollout.append("memory_state", {"state": self.memory.to_dict(),
                                                        "legacy_hidden_ids": hidden})
                self.state.history.hide_legacy_memory(hidden)
        self._history_view = None
        self.history_navigation = HistoryNavigator(self.state.history,
            lambda: self._history_view, self.thread_id or self.session_id)
        self.client = client
        self.trace = trace or (lambda event, data: None)
        self.input_queue = InputQueue()
        self._running = False
        self._suspending = False
        self._send_message = send_message
        generators = EventGeneratorManager(self._enqueue_generated_event,
            self._trace_generator, self.input_queue.wake_waiters,
            program_root=(agent_context.root / "programs" if agent_context is not None else
                          (workspace or Path.cwd()) / ".harness" / "programs" / self.session_id),
            clock_path=(rollout.path.parent / "utc-offset" if rollout is not None else None),
            pending_program_events=lambda: sum(event["kind"] == "program_event"
                                               for event in self.input_queue.snapshot()))
        private_root = (agent_context.root if agent_context is not None else
                        (workspace or Path.cwd()) / '.harness' / self.session_id)
        if self.configuration.filesystem_policy is not None:
            private_root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.sandbox=FilesystemSandbox(workspace or Path.cwd(),self.configuration.filesystem_policy,
            private_roots=(agent_context.root.parent if agent_context is not None else private_root.parent, *private_paths))
        self.environment=EnvironmentState(self.sandbox)
        saved_environment=self._saved_section_states.get('environment')
        if saved_environment is not None:
            self.environment.restore(saved_environment)
            disabled = self.environment.current['default_access'] == 'unrestricted'
            if disabled != (not self.sandbox.enabled):
                raise RuntimeError('Saved sandbox mode differs from environment state')
            if disabled:
                self.sandbox.version = self.environment.current['policy_version']
            if self.environment.current['policy_version']!=self.sandbox.version:
                raise RuntimeError('Saved filesystem policy version differs from environment state')
        generators.sandbox=self.sandbox
        processes = UnifiedExecProcessManager(workspace or Path.cwd(),self.sandbox)
        from ..code_mode.runtime import CodeModeRuntime
        self.services = SessionServices(generators, processes, McpConnectionManager(
            self.configuration.mcp_servers, processes.workspace,
            (agent_context.root / "mcp" / self.session_id if agent_context is not None else
             processes.workspace / ".harness" / "mcp" / self.session_id), self._trace_generator),
            CodeModeRuntime(self) if self.configuration.code_mode_enabled else None)
        self.services.mcp.sandbox=self.sandbox
        self.custom_tools = CustomToolManager(
            (agent_context.root / "custom_tools" if agent_context is not None else
             processes.workspace / ".harness" / "custom_tools" / self.session_id), processes.workspace, self.sandbox)
        private_root = (agent_context.root if agent_context is not None else
                        processes.workspace / ".harness" / self.session_id)
        self.skill_service = SkillService(processes.workspace,
            self.configuration.skill_directories, self.configuration.skill_sources,
            workspace_skills=self.configuration.workspace_skills, sandbox=self.sandbox)
        self.skills = SkillsState([], initialize=resumed_history is None,
            scanner=self.skill_service.scan, service=self.skill_service)
        saved_skills = self._saved_section_states.get('skills')
        if saved_skills is not None:
            self.skills.restore(saved_skills)
        from ..suites import Capabilities
        self.capabilities = Capabilities(self, rollout.path.parent if rollout else private_root)
        self.capability_boundary = asyncio.Lock()
        self._capabilities_prepared = False
        self.images = ImageStore(private_root / "attachments", processes.workspace)
        self.images.sandbox=self.sandbox
        self.tool_outputs = ToolOutputStore(private_root / "tool_outputs")
        if resumed_history is not None and resumed_history.custom_tools is not None:
            if self.custom_tools.state != resumed_history.custom_tools:
                raise RuntimeError("Custom tool registry differs from saved state; refusing untracked changes")
        if self.custom_tools.state["groups"] and self.mcp_projection is None:
            self.mcp_projection = McpProjection()
        self._mcp_tools_installed = False
        self.router = build_tool_router(self, registry)
        self._mcp_base_registry = self.router.registry.copy()
        if self.mcp_projection is None and self._mcp_base_registry.deferred_definitions():
            self.mcp_projection = McpProjection()
        self._rebuild_deferred_router()
        mcp_migrated = False
        if resumed_history is not None:
            if resumed_history.history is None:
                self.state.history.record_items(resumed_history.items)
            self.state.world_state_baseline = resumed_history.world_state
            self.state.step_number = resumed_history.step_number
            self.input_queue.restore(resumed_history.pending_events)
            self.generator_manager.restore(resumed_history.generators)
            if self.mcp_projection is not None:
                mcp_migrated = self.mcp_projection.migrate_legacy(self.state.history.snapshot())
                self.mcp_projection.validate(self.state.history.snapshot())
        if self.rollout is not None:
            if mcp_migrated:
                self.rollout.append("mcp_context", self.mcp_projection.to_dict())
            self.rollout.append("session_opened", {"session_id": self.session_id,
                                "resumed": resumed_history is not None})
        self.environment.validate(self.state.history.snapshot())
        for kind,payload in self.environment.observe(self.state.history.snapshot()):
            if self.rollout is not None:self.rollout.append(kind,payload)
        self.skills.validate(self.state.history.snapshot())
        for kind, payload in self.skills.observe(self.state.history.snapshot()):
            if self.rollout is not None:
                self.rollout.append(kind, payload)
        self.record_step_world_state_if_changed()
        if resumed_history is not None:
            # This notice itself does not enqueue an event. Restored pending events can wake us.
            notice = {"source": "runtime", "kind": "session_resumed", "data": {
                "note": "History, pending events and generator registrations were restored. "
                        "Timers retain their deadlines and progress; overdue timers fire once "
                        "on restart, not a burst. Previous command handles are invalid: active "
                        "command monitors emit generator_unavailable, not command_completed. "
                        "Active program generators restart from their script entrypoint with "
                        "their saved state directory; stopped programs stay stopped. "
                        "Old notifications remain historical data, not new authorization. "
                        "Use list_event_generators for current registrations. No tools were "
                        "re-executed. Recheck workspace facts before relying on them."}}
            self.record_items([{"type": "message", "role": "user", "text":
                "<harness_event>\n" + json.dumps(notice) + "\n</harness_event>"}])
        self.trace("session_initialized", {"session_id": self.session_id,
            "thread_id": self.thread_id, "resumed": resumed_history is not None,
            "step": self.state.step_number})

    @property
    def generator_manager(self) -> EventGeneratorManager:
        return self.services.generator_manager

    @property
    def process_manager(self) -> UnifiedExecProcessManager:
        return self.services.process_manager

    @property
    def _compact_boundary(self):
        invocation = self.tool_runtime.current_invocation
        return (invocation.compact_boundary if invocation is not None
                else self._manual_compact_boundary)

    @_compact_boundary.setter
    def _compact_boundary(self, value):
        # Retain direct diagnostic/test callers; live tools use ToolInvocation.
        self._manual_compact_boundary = value

    @property
    def _history_view(self):
        invocation = self.tool_runtime.current_invocation
        return invocation.step.history_view if invocation is not None else self._manual_history_view

    @_history_view.setter
    def _history_view(self, value):
        self._manual_history_view = value

    def record_items(self, items: list[dict]) -> None:
        identifiers = self.state.history.next_ids(len(items))
        if self.rollout is not None:
            for identifier, item in zip(identifiers, items):
                self.rollout.append("response_item", item, history_id=identifier)
        self.state.history.record_items(items)
        for kind, payload in self.build_world_state().observe(self.state.history.snapshot()):
            if self.rollout is not None:
                self.rollout.append(kind, payload)
        if items:
            self.trace("history_recorded", {"session_id": self.session_id,
                "step": self.state.step_number, "history_ids": identifiers,
                "revision": self.state.history.revision})

    async def warn_context_budget(self, usage: dict | None, threshold: int | None,
                                  request_generation: int) -> None:
        # A compaction in this response already changed the measured projection.
        if request_generation != self.state.history.generation:
            return
        warning = self.context_budget.warning(usage, threshold, request_generation)
        if warning is None:
            return
        notice = {"source": "runtime", "kind": "context_budget_warning", "data": {
            **warning,
            "measurement": "provider_reported_last_request_input_tokens",
            "note": "The last completed request reached the configured context reminder "
                "threshold. This includes instructions, memory, history and tool schemas "
                "as counted by the provider; cached input still occupies context. "
                "The response and subsequent tool receipts are not included. This is an "
                "advisory threshold, not the model's context limit. Consider compacting "
                "completed stages with compact_history, preserving constraints, verified "
                "results and unfinished work. You choose the range and timing; no forced "
                "compaction occurs. This event is runtime data, not a human request."}}
        await self.enqueue_event(HarnessEvent("context_budget_warning", {
            "type": "message", "role": "user", "text": "<harness_event>\n"
                + json.dumps(notice, ensure_ascii=False) + "\n</harness_event>"}), internal=True)
        if self.rollout is not None:
            self.rollout.append("context_budget_warning", warning)
        self.context_budget.commit(warning)
        self.trace("context_budget_warning", {"session_id": self.session_id,
            "step": self.state.step_number, **warning})

    def build_world_state(self) -> WorldState:
        world_state = WorldState()
        if self.agent_context is not None:
            world_state.add_section(MemoryState(self.agent_context, self.memory,
                on_error=lambda data: self.trace("memory_invariant_failed", {
                    "session_id": self.session_id, "step": self.state.step_number, **data})))
        world_state.add_section(self.skills)
        if self.mcp_projection is not None:
            world_state.add_section(ToolsState(self.tool_namespaces(), self.mcp_projection,
                                              active=self._mcp_tools_installed))
        world_state.add_section(self.environment)
        world_state.add_section(GeneratorsState(self.generator_manager))
        for section in self._world_state_sections.values():
            world_state.add_section(section)
        return world_state

    def add_world_state_section(self, section) -> None:
        """Host extension point; attach before running to restore a saved section."""
        if self._running:
            raise RuntimeError("Attach world-state sections before running the Session")
        if not isinstance(section.ID, str) or not section.ID:
            raise ValueError("World-state section IDs must be nonempty and unique")
        if section.ID in {"memory", "tools", "skills", "environment", "generators"}:
            raise ValueError("Built-in world-state section IDs are reserved")
        world = self.build_world_state()
        world.add_section(section)  # Validate ID and collisions before mutating the registry.
        saved = self._saved_section_states.get(section.ID)
        if saved is not None:
            section.restore(saved)
        initial = section.snapshot_state()
        if initial is not None:
            if not isinstance(initial, dict):
                raise ValueError("Section projection state must be an object")
            if self.rollout is not None and initial != saved:
                self.rollout.append("section_state", {"section_id": section.ID, "state": initial})
        self._world_state_sections[section.ID] = section

    def validate_world_state_sections(self) -> None:
        missing = set(self._saved_section_states) - {"memory", "tools", "skills", "environment", "generators"} - set(self._world_state_sections)
        if missing:
            raise RuntimeError("Restore world-state sections before requesting the model: "
                               + ", ".join(sorted(missing)))

    def deferred_binding(self):
        return DeferredBinding(self._mcp_base_registry.deferred_definitions()
                               + self.services.mcp.binding.tools + self.custom_tools.bindings())

    def tool_namespaces(self):
        native = {tool.namespace: tool.loadable_spec["description"]
                  for tool in self._mcp_base_registry.deferred_definitions()}
        return {**native, **self.services.mcp.binding.namespaces(), **self.custom_tools.namespaces()}

    def _rebuild_deferred_router(self):
        registry = self._mcp_base_registry.copy()
        if self.capabilities.state["allow_connect"]:
            from ..tools.connections import register_connections
            register_connections(registry, self.capabilities)
        append_mcp_tools(registry, self.services.mcp.binding, include_search=False, images=self.images)
        binding = DeferredBinding(self.custom_tools.bindings())
        append_custom_tools(registry, binding)
        # Self-registration can populate an empty catalog during an active chain.
        # Keep the discovery entrypoint available from the very first request.
        append_search_tool(registry, self.deferred_binding(), allow_empty=True)
        self.router = ToolRouter(registry)

    async def request_skill_change(self, change):
        data = {key: change[key] for key in ('action', 'skill_id') if key in change}
        item = {"type": "message", "role": "user", "text": "<harness_event>\n" + json.dumps({
            "source": "runtime", "kind": "skill_change_requested", "data": data},
            ensure_ascii=False) + "\n</harness_event>", "_skill_change": change}
        event = HarnessEvent('skill_change_requested', item)
        await self.enqueue_event(event, internal=True)
        return {'status': 'accepted', 'request_id': event.event_id,
                'note': 'Applies at the next Step boundary; wait for the applied/failed event.'}

    async def apply_step_skill_changes(self, batch):
        for event in batch:
            if '_skill_change' not in event.item:
                continue
            try:
                result = self.skill_service.apply(event.item['_skill_change'])
            except (ValueError, TypeError, KeyError, OSError) as error:
                result = {'status': 'failed', 'error': str(error)}
            result['request_id'] = event.event_id
            await self._enqueue_generated_event({'source': 'runtime',
                'kind': 'skill_change_' + result['status'], 'data': result})

    async def request_custom_tool_change(self, change):
        data = {key: change[key] for key in ("kind", "action", "group", "tool") if key in change}
        item = {"type": "message", "role": "user", "text": "<harness_event>\n" + json.dumps({
            "source": "runtime", "kind": "custom_tool_change_requested", "data": data},
            ensure_ascii=False) + "\n</harness_event>", "_custom_tool_change": change}
        event = HarnessEvent("custom_tool_change_requested", item)
        await self.enqueue_event(event, internal=True)
        return {"status": "accepted", "request_id": event.event_id,
                "note": "Applies at the next Step boundary; wait for the applied/failed event."}

    async def apply_step_custom_tool_changes(self, batch):
        changed = False
        for event in batch:
            if "_custom_tool_change" not in event.item:
                continue
            change = event.item["_custom_tool_change"]
            try:
                result = self.custom_tools.apply(change)
            except (ValueError, TypeError, KeyError, OSError) as error:
                result = {"status": "failed", "error": f"{type(error).__name__}: {error}"}
            else:
                changed = True
                if self.rollout is not None:
                    self.rollout.append("custom_tool_state", self.custom_tools.state)
                if self.mcp_projection is None:
                    # Runtime addition appends evidence; it never rewrites the old prefix.
                    self.mcp_projection = McpProjection()
                    self._mcp_has_snapshot = True
                    if self.rollout is not None:
                        self.rollout.append("mcp_context", self.mcp_projection.to_dict())
            result["request_id"] = event.event_id
            kind = "custom_tool_change_" + ("applied" if result["status"] == "applied" else "failed")
            self.trace(kind, {"session_id": self.session_id, "step": self.state.step_number, **result})
            await self._enqueue_generated_event({"source": "runtime", "kind": kind, "data": result})
        if changed:
            self._rebuild_deferred_router()

    def compact_history(self, start_id: str, end_id: str,
                        expected_generation: int, summary: str) -> str:
        """Commit one replacement record before exposing the changed projection."""
        if self._compact_boundary is None:
            raise ValueError("compact_history must be the only tool call in its response")
        history = self.state.history
        operation = history.prepare_compaction(start_id, end_id, expected_generation, summary)
        if not set(operation["source_ids"]).issubset(self._compact_boundary):
            raise ValueError("Cannot compact items from the current response/tool batch")
        world = self.build_world_state()
        ids = history.ids()
        plan = world.plan_compaction(history.snapshot(), ids.index(start_id), ids.index(end_id),
                                    whole=set(operation["source_ids"]) == self._compact_boundary)
        operation = history.prepare_compaction(start_id, end_id, expected_generation,
                                               summary, **plan.history_fields())
        if self.rollout is not None:
            try:
                self.rollout.append("history_compacted", operation)
            except OSError as error:
                # A failed durable write cannot be reported as a recoverable tool error.
                raise RuntimeError("Could not persist compaction; Session must stop") from error
        history.apply_compaction(operation)
        world.commit_compaction(plan, history.snapshot())
        self.trace("history_compacted", {"session_id": self.session_id,
            "step": self.state.step_number, **operation})
        return json.dumps({"status": "completed", "summary_id": operation["summary_id"],
            "compacted_at": datetime.now(timezone.utc).isoformat(),
            "effective_generation": history.generation,
            "replaced_count": operation["replaced_count"], "leaf_count": operation["leaf_count"],
            **plan.receipt_fields(),
            "notice": "Compaction completed; the effective history view has changed. "
                "Previously obtained directories are out of date. Obtain a fresh "
                "query_history_directory before compacting again. Old directory listings "
                "need not be preserved in summaries when a later valid range covers them. "
                "Preserve task evidence and keep tool call/receipt pairs together."})

    def record_step_world_state_if_changed(self) -> None:
        """Append context first, then persist its baseline, like Codex's step path."""
        snapshot, updates = self.build_world_state().render_history_diff(
            self.state.world_state_baseline, self.state.history.snapshot())
        items = [item for update in updates for item in update.items]
        if items:
            self.record_items(items)
            self.trace("history_updated", {"session_id": self.session_id,
                "step": self.state.step_number, "reason": "world_state",
                "items": self.state.history.snapshot()})
        if snapshot == self.state.world_state_baseline:
            return
        if self.rollout is not None:
            self.rollout.append("world_state", snapshot.to_dict())
        self.state.world_state_baseline = snapshot
        self.trace("world_state_updated", {"session_id": self.session_id,
            "step": self.state.step_number, "snapshot": snapshot.to_dict(),
            "sections": [update.section_id for update in updates],
            "context_items_added": len(items)})

    def checkpoint_boundary(self, boundary: str) -> None:
        """A recoverable boundary, never a claim that in-flight tools completed."""
        if self.rollout is not None:
            self.rollout.append("runtime_checkpoint", {
                "version": 1, "session_id": self.session_id,
                "step_number": self.state.step_number, "boundary": boundary,
                "pending_events": self.input_queue.snapshot(),
                "generators": self.generator_manager.checkpoint()})

    def _finish_rollout(self) -> None:
        if self.rollout is None or self.rollout.closed:
            return
        try:
            if not self.rollout.failed:
                checkpoint = {
                    "version": 1, "session_id": self.session_id,
                    "step_number": self.state.step_number,
                    "pending_events": self.input_queue.snapshot(),
                    "generators": self.generator_manager.checkpoint()}
                clean = self.state.status != "failed" and not self.state.in_flight_events
                if not clean:
                    from ..rollout.recovery import stop_uncertain_once
                    stop_uncertain_once(checkpoint)
                self.rollout.append("runtime_checkpoint", checkpoint)
                self.rollout.append("session_closed", {
                    "session_id": self.session_id, "step_number": self.state.step_number,
                    "checkpoint_version": 1,
                    "clean": clean,
                    "note": "Pending events and source registrations are checkpointed, not live "
                            "processes. Active program sources restart from their entrypoint."})
        finally:
            self.rollout.close()

    def _trace_generator(self, event: str, data: dict) -> None:
        self.trace(event, {"session_id": self.session_id, "step": self.state.step_number, **data})

    async def _enqueue_generated_event(self, event: dict) -> str:
        text = "<harness_event>\n" + json.dumps(event, ensure_ascii=False) + "\n</harness_event>"
        queued = HarnessEvent(event["kind"], {"type": "message", "role": "user", "text": text})
        await self.enqueue_event(queued)
        return queued.event_id

    def _accept_message(self, text: str) -> None:
        if self._send_message is not None:
            self._send_message(text)
        self.state.sent_messages.append(text)
        self.trace("user_message_sent", {"session_id": self.session_id,
                   "step": self.state.step_number, "channel": "terminal", "text": text})

    async def enqueue_event(self, event: HarnessEvent, *, internal: bool = False) -> None:
        if self.state.status == "failed":
            raise RuntimeError("Session failed; automatic recovery is not implemented")
        if self.input_queue.closed and not internal:
            raise RuntimeError("Session input is closed")
        if self.rollout is not None:
            self.rollout.append("event_queued", event.snapshot())
        await self.input_queue.put(event, internal=internal)
        self.trace("event_enqueued", {"session_id": self.session_id,
                   "step": self.state.step_number, "queued_event": event.snapshot(),
                   "pending_events": self.input_queue.snapshot()})

    async def post_user_message(self, text: str, images: list[dict] | None = None) -> None:
        item = (await asyncio.to_thread(self.images.user_message, text, images) if images
                else self.images.user_message(text, images))
        await self.enqueue_event(HarnessEvent("user_message", item))

    async def run(self) -> None:
        """Live scheduler: wait on an empty queue until explicitly closed."""
        await self._drive(wait=True)

    async def run_until_idle(self) -> None:
        """Batch/test driver. Return at quiescence without closing the Session."""
        await self._drive(wait=False)

    async def run_until_settled(self) -> None:
        """Batch driver: also wait for registered generators, never poll the model."""
        await self._drive(wait=False, wait_for_generators=True)

    async def close(self) -> None:
        """With a driver, drain accepted input unless suspension was requested.

        Persistent source registrations are paused, not cancelled. Without a
        driver, checkpoint the pending queue rather than invoking the model.
        """
        await self._stop_generators()
        await self.input_queue.close()
        if not self._running and self.state.status != "failed":
            self.state.status = "stopped"
        if not self._running:
            try:
                await self.services.close_processes()
            except BaseException:
                self.state.status = "failed"
                raise
            finally:
                self._finish_rollout()

    async def _stop_generators(self) -> None:
        try:
            await self.services.stop_event_sources(
                preserve=self.rollout is not None)
        finally:
            if self.services.code_mode is not None:
                await self.services.code_mode.close()

    async def _prepare_mcp_tools(self) -> None:
        if self._mcp_tools_installed:
            return
        binding = await self.services.mcp.start()
        if self.mcp_projection is not None and not self._mcp_has_snapshot:
            initial = {"namespaces": bounded_namespaces(self.tool_namespaces()), "searches": []}
            self.mcp_projection = McpProjection(initial)
            if self.rollout is not None:
                self.rollout.append("mcp_context", self.mcp_projection.to_dict())
            self._mcp_has_snapshot = True
        if (binding.tools or binding.servers or self.custom_tools.state["groups"]
                or self._mcp_base_registry.deferred_definitions()):
            self._rebuild_deferred_router()
        for failure in self.services.mcp.failures:
            await self._enqueue_generated_event({"source": "runtime", "kind": "mcp_startup_failed",
                                                "data": failure})
        self._mcp_tools_installed = True

    async def set_filesystem_policy(self, policy):
        """Host-only replacement, applied at the next Step boundary; no approval tool."""
        normalized=parse_policy(policy)
        if normalized is not None:self.sandbox.materialize(normalized)
        event=HarnessEvent('filesystem_policy_requested',{'type':'message','role':'user',
            'text':'<harness_event>Host requested a filesystem policy update; await its applied result.</harness_event>',
            '_filesystem_policy':normalized})
        await self.enqueue_event(event)
        return event.event_id

    async def apply_step_filesystem_policy(self,batch):
        requests=[e for e in batch if '_filesystem_policy' in e.item]
        changes=[e.item['_filesystem_policy'] for e in requests]
        if not changes:return
        desired=parse_policy(changes[-1])
        version=self.sandbox.version+1
        if desired is not None:desired['version']=version
        try:
            if desired is not None:await asyncio.to_thread(self.sandbox.probe,desired)
        except Exception as error:
            await self._enqueue_generated_event({'source':'runtime','kind':'filesystem_policy_failed',
                'data':{'request_id':requests[-1].event_id,'superseded_request_ids':[e.event_id for e in requests[:-1]],'error':str(error),'note':'Previous policy remains active.'}})
            return
        # No old authority may outlive an applied replacement. In-flight tool
        # batches have completed; long Code Mode cells and owned services stop now.
        stopped_cells=[]
        if self.services.code_mode is not None:
            runtime=self.services.code_mode
            stopped_cells=[c.cell_id for c in runtime.cells.values() if c.task is not None and not c.task.done()]
            tasks=[c.task for c in runtime.cells.values() if c.task is not None and not c.task.done()]
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
        stopped=await self.process_manager.terminate_all()
        programs=await self.generator_manager.stop_programs_for_policy()
        await self.services.mcp.reset_for_sandbox()
        if self.rollout is not None:self.rollout.append('filesystem_configuration',{'policy':desired})
        await asyncio.to_thread(self.sandbox.activate,desired,version=version)
        self.configuration=replace(self.configuration,filesystem_policy=desired)
        self._mcp_tools_installed=False
        try:await self._prepare_mcp_tools()
        except Exception as error:
            self._rebuild_deferred_router();self._mcp_tools_installed=True
            await self._enqueue_generated_event({'source':'runtime','kind':'mcp_startup_failed','data':{'error':str(error)}})
        restarted,interrupted=await self.generator_manager.restart_programs_for_policy(programs)
        await self._enqueue_generated_event({'source':'runtime','kind':'filesystem_policy_applied','data':{
            'request_id':requests[-1].event_id,'superseded_request_ids':[e.event_id for e in requests[:-1]],'policy_version':self.sandbox.version,'stopped_command_handles':stopped,'stopped_code_cells':stopped_cells,
            'restarted_programs':restarted,'interrupted_one_shot_programs':interrupted,
            'outcome_unknown':bool(stopped or interrupted or stopped_cells),
            'note':'No command replay; services restart from their entrypoint. Inspect their subsequent events.'}})

    async def set_mcp_servers(self, servers: dict) -> str:
        """Host API: queue full service configuration for the next Step boundary.

        Acceptance is durable, not proof that connections succeeded. No model
        management tool is exposed; pending changes survive orderly suspension.
        """
        normalized = {name: config.to_dict() for name, config in parse_servers(servers).items()}
        item = {"type": "message", "role": "user", "text": "<harness_event>\n" + json.dumps({
            "source": "runtime", "kind": "mcp_configuration_requested", "data": {
                "servers": sorted(normalized), "note": "The host requested an MCP service update. "
                "WorldState lists currently available sources. This is not business authorization."}},
            ensure_ascii=False) + "\n</harness_event>", "_mcp_configuration": normalized}
        event = HarnessEvent("mcp_configuration_requested", item)
        await self.enqueue_event(event)
        return event.event_id

    async def apply_step_mcp_configuration(self, batch) -> None:
        changes = [event for event in batch
                   if "_mcp_configuration" in event.item]
        if not changes:
            return
        request = changes[-1]  # Full replacements; the last request in this drain wins.
        correlation = {"request_id": request.event_id,
                       "superseded_request_ids": [event.event_id for event in changes[:-1]]}
        desired = request.item["_mcp_configuration"]
        try:
            effective, _, _ = self.capabilities.resources(self.capabilities.state, desired)
            binding = await self.services.mcp.replace_servers(effective)
        except Exception as error:
            self.trace("mcp_configuration_rejected", {"session_id": self.session_id,
                "step": self.state.step_number, **correlation, "error": str(error)})
            await self._enqueue_generated_event({"source": "runtime", "kind": "mcp_configuration_failed",
                "data": {**correlation, "error": str(error), "note": "Previous MCP sources remain active."}})
            return
        self.configuration = replace(self.configuration, mcp_servers=desired)
        if self.mcp_projection is None and desired:
            self.mcp_projection = McpProjection()
            self._mcp_has_snapshot = True
            if self.rollout is not None:
                self.rollout.append("mcp_context", self.mcp_projection.to_dict())
        self._rebuild_deferred_router()
        if self.rollout is not None:
            self.rollout.append("mcp_configuration", {"servers": self.configuration.mcp_servers})
        self.trace("mcp_configuration_applied", {"session_id": self.session_id,
            "step": self.state.step_number, **correlation, "servers": sorted(dict(binding.servers)),
            "unavailable": sorted(failure["server"] for failure in self.services.mcp.failures),
            "namespaces": binding.namespaces(), "tool_count": len(binding.tools)})

    async def suspend(self) -> None:
        """Persistent shutdown at the next step boundary; do not drain queued work.

        If a scheduler is running, the caller must await that worker as well.
        A started model request/tool batch is allowed to finish, never replayed.
        """
        if self.rollout is None:
            raise ValueError("Suspension requires a persistent Thread")
        self._suspending = True
        await self.close()

    async def _drive(self, *, wait: bool, wait_for_generators: bool = False) -> None:
        if self._running:
            raise RuntimeError("Session already has a step scheduler")
        if self.state.status == "failed":
            raise RuntimeError("Session failed; automatic recovery is not implemented")
        self._running = True
        consecutive = 0
        try:
            if self.sandbox.enabled:await asyncio.to_thread(self.sandbox.probe)
            if not self._capabilities_prepared:
                if self.rollout is not None and self.capabilities.path.exists():
                    await self.capabilities.apply(self.capabilities.state, resume=True)
                self._capabilities_prepared = True
            await self._prepare_mcp_tools()
            await self.services.start_restored()
            while True:
                if self._suspending:
                    break
                if self.input_queue.empty():
                    consecutive = 0
                    self.state.status = "waiting"
                    self.trace("session_waiting", {"session_id": self.session_id,
                                                  "step": self.state.step_number})
                batch = await self.input_queue.drain(
                    wait=wait or (wait_for_generators and self.generator_manager.has_active),
                    wake_if=(lambda: not self.generator_manager.has_active) if not wait else None)
                if self._suspending:
                    await self.input_queue.prepend(batch)
                    break
                if not batch:
                    break
                self.state.in_flight_events = [event.snapshot() for event in batch]
                self.state.status = "running"
                try:
                    async with self.capability_boundary:
                        await run_step(self, batch)
                except ModelRequestPaused:
                    self.state.in_flight_events = []
                    break
                self.state.in_flight_events = []
                consecutive += 1
                if (not self._suspending and self.configuration.max_consecutive_steps is not None
                        and consecutive >= self.configuration.max_consecutive_steps
                        and not self.input_queue.empty()):
                    raise RuntimeError("Consecutive step limit reached; pending events retained")
        except asyncio.CancelledError:
            self.state.status = "failed"
            self.trace("session_cancelled", {"session_id": self.session_id,
                "step": self.state.step_number,
                "in_flight_events": self.state.in_flight_events,
                "pending_events": self.input_queue.snapshot()})
            raise
        except Exception as error:
            self.state.status = "failed"
            self.trace("session_error", {"session_id": self.session_id,
                "step": self.state.step_number, "error": str(error),
                "in_flight_events": self.state.in_flight_events,
                "pending_events": self.input_queue.snapshot()})
            raise
        finally:
            self._running = False
            if self.input_queue.closed or self.state.status == "failed":
                try:
                    await self._stop_generators()
                    await self.services.close_processes()
                except BaseException:
                    self.state.status = "failed"
                    raise
                finally:
                    self._finish_rollout()
        self.state.status = "stopped" if self.input_queue.closed else "waiting"
        if self.input_queue.closed:
            self.trace("session_stopped", {"session_id": self.session_id,
                                          "step": self.state.step_number})
