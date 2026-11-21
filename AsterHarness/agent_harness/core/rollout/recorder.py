"""Small local rollout format; deliberately not wire-compatible with Codex.

Clean shutdowns resume directly; explicit recovery validates failed-close
checkpoints or proven uncommitted runtime boundaries. Tools are never re-executed; pending events and
source registrations are hydrated from the latest orderly runtime checkpoint.
The writer holds a Unix advisory lock for the entire Session lifetime.
"""

from copy import deepcopy
try:
    import fcntl
except ImportError:
    from agent_harness import _windows_locks as fcntl
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from .runtime_checkpoint import validate_checkpoint
from ..context.world_state import WorldStateSnapshot
from ..context_manager.archive import history_id
from ..context_manager import ContextManager


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ResumedHistory:
    meta: dict
    items: list[dict]
    step_number: int
    pending_events: list[dict] = field(default_factory=list)
    generators: list[dict] = field(default_factory=list)
    world_state: WorldStateSnapshot | None = None
    history: ContextManager | None = None
    memory: dict | None = None
    context_budget: dict | None = None
    mcp_context: dict | None = None
    custom_tools: dict | None = None
    section_states: dict = field(default_factory=dict)


class RolloutRecorder:
    def __init__(self, path: Path, stream) -> None:
        self.path = path
        self._stream = stream
        self.closed = False
        self.failed = False
        self.attached = False

    @staticmethod
    def path_for(root: Path, thread_id: str) -> Path:
        try:
            if str(UUID(thread_id)) != thread_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Thread ID must be a canonical UUID") from None
        return root.resolve() / thread_id / "rollout.jsonl"

    @classmethod
    def create(cls, root: Path, workspace: Path, configuration: dict) -> tuple["RolloutRecorder", dict]:
        workspace = workspace.resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError("Workspace must be a directory")
        thread_id = str(uuid4())
        path = cls.path_for(root, thread_id)
        path.parent.mkdir(parents=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        recorder = cls(path, os.fdopen(fd, "r+", encoding="utf-8"))
        try:
            fcntl.flock(recorder._stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            meta = {"format_version": 1, "thread_id": thread_id, "created_at": timestamp(),
                    "cwd": str(workspace), "configuration": configuration}
            recorder.append("session_meta", meta)
            return recorder, meta
        except BaseException:
            recorder.close()
            raise

    @classmethod
    def resume(cls, root: Path, thread_id: str) -> tuple["RolloutRecorder", ResumedHistory]:
        path = cls.path_for(root, thread_id)
        recorder = cls(path, path.open("r+", encoding="utf-8"))
        try:
            try:
                fcntl.flock(recorder._stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Thread is already open in another Session") from None
            history = cls._read_stream(recorder._stream, thread_id)
            recorder._stream.seek(0, os.SEEK_END)
            return recorder, history
        except BaseException:
            recorder.close()
            raise

    @staticmethod
    def _recovery_hash(row):
        return hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @classmethod
    def recover(cls, root: Path, thread_id: str, reason: str, *, allow_trace: bool = False) -> dict:
        """Append an audited recovery marker; never truncate history or replay tools.

        A failed-close checkpoint or a proven uncommitted runtime boundary is
        eligible. Partial writes and unfinished calls fail closed.
        """
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise ValueError("Recovery requires a reason of 1-1000 characters")
        path = cls.path_for(root, thread_id)
        recorder = cls(path, path.open("r+", encoding="utf-8"))
        try:
            try:
                fcntl.flock(recorder._stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Thread is already open in another Session") from None
            text = recorder._stream.read()
            if not text.endswith("\n"):
                raise ValueError("Rollout is incomplete; refusing recovery")
            rows = [json.loads(line) for line in text.splitlines()]
            if rows and rows[-1].get("type") not in {"session_closed", "session_recovered"}:
                from .recovery import crash_evidence, digest
                try:
                    checkpoint, expected_history, source, trace_hash = crash_evidence(rows, path.parent, allow_trace)
                except (KeyError, TypeError, StopIteration, OSError) as error:
                    raise ValueError("Missing or invalid crash recovery evidence") from error
                payload = {"version": 2, "thread_id": thread_id, "reason": reason.strip(),
                           "source": source, "archive_hash": digest(rows),
                           "checkpoint": checkpoint, "trace_hash": trace_hash}
                marker = {"timestamp": timestamp(), "type": "session_recovered", "payload": payload}
                history = cls._read(text + json.dumps(marker) + "\n", thread_id)
                if expected_history is not None and history.history.snapshot() != expected_history:
                    raise ValueError("Recovery trace and authoritative effective history disagree")
                recorder._stream.seek(0, os.SEEK_END)
                recorder.append("session_recovered", payload)
                return {"thread_id": thread_id, "recovered": True, "source": source,
                        "pending_events": len(history.pending_events), "step_number": history.step_number}
            history = cls._read(text, thread_id, allow_unclean=True)
            closed = rows[-1]
            if closed.get("type") != "session_closed" or closed["payload"].get("clean") is not False:
                return {"thread_id": thread_id, "recovered": False}
            recorder._stream.seek(0, os.SEEK_END)
            recorder.append("session_recovered", {"version": 1, "thread_id": thread_id,
                "session_id": closed["payload"]["session_id"], "step_number": history.step_number,
                "closure_hash": cls._recovery_hash(closed), "reason": reason.strip()})
            return {"thread_id": thread_id, "recovered": True,
                    "pending_events": len(history.pending_events), "step_number": history.step_number}
        finally:
            recorder.close()

    @classmethod
    def _validate_recovery(cls, rows, index, thread_id):
        item = rows[index]["payload"]
        if item.get("version") == 2:
            from .recovery import digest
            required = {"version", "thread_id", "reason", "source", "archive_hash", "checkpoint", "trace_hash"}
            if (set(item) != required or item["thread_id"] != thread_id
                    or not isinstance(item["reason"], str) or not 1 <= len(item["reason"]) <= 1000
                    or item["source"] not in {"boundary", "trace"}
                    or item["archive_hash"] != digest(rows[:index])):
                raise ValueError("Invalid recovery record or archive hash")
            checkpoint = item["checkpoint"]
            validate_checkpoint(checkpoint)
            opened = next((r for r in reversed(rows[:index]) if r["type"] == "session_opened"), None)
            if (opened is None or checkpoint.get("session_id") != opened["payload"].get("session_id")
                    or type(checkpoint.get("step_number")) is not int or checkpoint["step_number"] < 0):
                raise ValueError("Recovery checkpoint does not match its Session")
            return checkpoint
        if (index < 2 or set(item) != {"version", "thread_id", "session_id", "step_number", "closure_hash", "reason"}
                or item.get("version") != 1 or item.get("thread_id") != thread_id
                or not isinstance(item.get("reason"), str) or not 1 <= len(item["reason"]) <= 1000):
            raise ValueError("Invalid recovery record")
        closed, checkpoint = rows[index-1], rows[index-2]
        if (closed.get("type") != "session_closed" or closed["payload"].get("clean") is not False
                or closed["payload"].get("checkpoint_version") != 1
                or checkpoint.get("type") != "runtime_checkpoint"
                or item["session_id"] != closed["payload"].get("session_id")
                or item["step_number"] != closed["payload"].get("step_number")
                or checkpoint["payload"].get("session_id") != item["session_id"]
                or checkpoint["payload"].get("step_number") != item["step_number"]
                or item["closure_hash"] != cls._recovery_hash(closed)):
            raise ValueError("Recovery record does not match its failed-close checkpoint")
        validate_checkpoint(checkpoint["payload"])

    @classmethod
    def _read_stream(cls, stream, thread_id: str) -> ResumedHistory:
        from .reader import JournalRows
        return cls._replay(JournalRows(stream), thread_id)

    @classmethod
    def _read(cls, text: str, thread_id: str, *, allow_unclean: bool = False) -> ResumedHistory:
        if not text.endswith("\n"):
            raise ValueError("Rollout is incomplete; automatic crash recovery is not implemented")
        try:
            rows = [json.loads(line) for line in text.splitlines()]
        except json.JSONDecodeError:
            raise ValueError("Corrupt rollout; no records were changed") from None
        return cls._replay(rows, thread_id, allow_unclean=allow_unclean)

    @staticmethod
    def _replay(rows, thread_id: str, *, allow_unclean: bool = False) -> ResumedHistory:
        try:
            if not rows or any(not isinstance(row, dict) or not isinstance(row.get("payload"), dict)
                               or not isinstance(row.get("timestamp"), str) for row in rows):
                raise ValueError("Invalid rollout structure")
            meta = deepcopy(rows[0]["payload"])
            if (rows[0].get("type") != "session_meta" or meta.get("format_version") != 1
                    or meta.get("thread_id") != thread_id
                    or not isinstance(meta.get("cwd"), str) or not Path(meta["cwd"]).is_absolute()
                    or not isinstance(meta.get("configuration"), dict)):
                raise ValueError("Invalid or unsupported Thread metadata")
            config = meta["configuration"]
            if (not {"base_instructions", "max_consecutive_steps"}.issubset(config)
                    or set(config) - {"base_instructions", "max_consecutive_steps",
                                      "context_budget_warning_tokens", "mcp_servers", "tool_output_token_limit", "code_mode_enabled", "skill_directories", "skill_sources", "workspace_skills", "filesystem_policy"}
                    or not isinstance(config.get("base_instructions"), str)
                    or (config.get("max_consecutive_steps") is not None and (
                        type(config["max_consecutive_steps"]) is not int
                        or config["max_consecutive_steps"] < 1))):
                raise ValueError("Invalid saved Session configuration")
            threshold = config.get("context_budget_warning_tokens", 100_000)
            if threshold is not None and (type(threshold) is not int or threshold < 1):
                raise ValueError("Invalid saved context budget threshold")
            from ..mcp.config import parse_servers
            from ..tools.output_budget import validate_limit
            validate_limit(config.get("tool_output_token_limit", 10000))
            parse_servers(config.get("mcp_servers", {}))
            from ..sandbox import parse_policy
            config["filesystem_policy"] = parse_policy(config.get("filesystem_policy"), legacy=True)
            from ..skills import validate_directories
            if type(config.get("workspace_skills", True)) is not bool:
                raise ValueError("Invalid workspace skills setting")
            validate_directories(config.get("skill_directories", []))
            from ..skills.service import validate_sources
            validate_sources(config.get("skill_sources", []))
            if type(config.get("code_mode_enabled", False)) is not bool:
                raise ValueError("Invalid saved Code Mode configuration")
            footer_index = len(rows) - 1
            recovered = rows[-1].get("type") == "session_recovered"
            recovered_checkpoint = None
            if recovered:
                recovered_checkpoint = RolloutRecorder._validate_recovery(rows, footer_index, thread_id)
                if recovered_checkpoint is None:
                    footer_index -= 1
            footer_row = rows[footer_index]
            footer = ({**recovered_checkpoint, "clean": False, "checkpoint_version": 1}
                      if recovered_checkpoint is not None else footer_row["payload"])
            if recovered_checkpoint is None and footer_row.get("type") != "session_closed":
                raise ValueError("Thread did not close cleanly; explicit boundary recovery is required")
            if footer.get("clean") is not True:
                if not ((allow_unclean or recovered) and footer.get("clean") is False
                        and footer.get("checkpoint_version") == 1):
                    raise ValueError("Thread did not close cleanly; explicit checkpoint recovery is required")
            items = []
            history = ContextManager()
            world_state = None
            memory = None
            context_budget = None
            mcp_context = None
            custom_tools = None
            pending = set()
            seen = set()
            step_number = footer.get("step_number")
            if type(step_number) is not int or step_number < 0:
                raise ValueError("Invalid saved step number")
            events, generators = [], []
            if recovered_checkpoint is not None:
                events, generators = validate_checkpoint(recovered_checkpoint)
            elif "checkpoint_version" in footer:
                if (footer["checkpoint_version"] != 1 or footer_index < 2
                        or rows[footer_index-1].get("type") != "runtime_checkpoint"
                        or rows[footer_index-1]["payload"].get("session_id") != footer.get("session_id")
                        or rows[footer_index-1]["payload"].get("step_number") != step_number):
                    raise ValueError("Missing or mismatched runtime checkpoint")
                events, generators = validate_checkpoint(rows[footer_index-1]["payload"])
            section_states = {}
            for row_index, row in enumerate(rows[1:], 1):
                kind, item = row.get("type"), row["payload"]
                if kind not in {"step_boundary", "session_recovered", "session_opened", "event_queued", "response_item", "session_closed", "runtime_checkpoint", "world_state", "history_compacted", "memory_state", "context_budget_warning", "mcp_context", "mcp_configuration", "custom_tool_state", "section_state", "filesystem_configuration", "session_configuration", "host_configuration"}:
                    raise ValueError("Unsupported rollout record type")
                if kind == "session_recovered":
                    RolloutRecorder._validate_recovery(rows, row_index, thread_id)
                if kind == 'session_configuration':
                    from ..session import SessionConfiguration
                    from dataclasses import asdict
                    saved = deepcopy(item['configuration'])
                    saved['filesystem_policy'] = parse_policy(saved.get('filesystem_policy'), legacy=True)
                    meta['configuration'] = asdict(SessionConfiguration(**saved))
                if kind == 'host_configuration':
                    if not isinstance(item.get('profile'), dict) or not isinstance(item.get('instructions'), str):
                        raise ValueError('Invalid host configuration snapshot')
                    if "mcp_servers" in item:
                        meta["host_mcp_servers"] = {
                            name: config.to_dict() for name, config in parse_servers(item["mcp_servers"]).items()}
                if kind == 'filesystem_configuration':
                    if set(item)!={'policy'}:
                        raise ValueError('Invalid saved filesystem configuration')
                    meta['configuration']['filesystem_policy']=parse_policy(item['policy'], legacy=True)
                if kind == "section_state":
                    if (not isinstance(item, dict) or set(item) != {"section_id", "state"}
                            or not isinstance(item["section_id"], str) or not item["section_id"]
                            or not isinstance(item["state"], dict)):
                        raise ValueError("Invalid saved section state")
                    section_states[item["section_id"]] = item["state"]
                if kind == "mcp_configuration":
                    if set(item) != {"servers"}:
                        raise ValueError("Invalid MCP configuration update")
                    meta["configuration"]["mcp_servers"] = {
                        name: config.to_dict() for name, config in parse_servers(item["servers"]).items()}
                if kind == "mcp_context":
                    from ..context_manager.mcp import McpProjection
                    mcp_context = McpProjection.from_dict(item).to_dict()
                if kind == "custom_tool_state":
                    if (not isinstance(item, dict) or item.get("format_version") != 1
                            or type(item.get("revision")) is not int or not isinstance(item.get("groups"), dict)):
                        raise ValueError("Invalid custom tool state")
                    custom_tools = item
                if kind == "context_budget_warning":
                    from ..session.context_budget import validate_warning
                    context_budget = validate_warning(item)
                if kind == "memory_state":
                    from ..context_manager.memory import MemoryProjection
                    if set(item) != {"state", "legacy_hidden_ids"}:
                        raise ValueError("Invalid memory-state record")
                    memory = MemoryProjection.from_dict(item["state"]).to_dict()
                    history.hide_legacy_memory(item["legacy_hidden_ids"])
                if kind == "history_compacted":
                    before = history.revision
                    history.apply_compaction(item)
                    if item["version"] in {2, 3} and "memory_state" in item:
                        memory = item["memory_state"]
                    if item["version"] == 3:
                        mcp_context = item["mcp_state"]
                    if item["version"] == 4:
                        for name, value in item["world_state_sections"].items():
                            section_states[name] = value["state"]
                            if name == "memory":
                                memory = value["state"]
                            elif name == "tools":
                                mcp_context = value["state"]
                    items.extend(history.item(i) for i in history.ids("archive")[before:])
                if kind == "world_state":
                    world_state = WorldStateSnapshot.from_dict(item)
                if kind != "response_item":
                    continue
                # Missing IDs are legacy records. Their deterministic replay IDs
                # agree with new records appended after a legacy Thread resumes.
                if "history_id" in row and row["history_id"] != history_id(len(items) + 1):
                    raise ValueError("Invalid stored history ID sequence")
                if not isinstance(item.get("type"), str):
                    raise ValueError("Invalid response item")
                if item["type"] in {"function_call", "tool_search_call"}:
                    call_id = item.get("call_id")
                    if not isinstance(call_id, str) or not call_id or call_id in seen:
                        raise ValueError("Invalid or duplicate stored tool call")
                    seen.add(call_id)
                    pending.add(call_id)
                elif item["type"] in {"function_call_output", "tool_search_output"}:
                    call_id = item.get("call_id")
                    if not isinstance(call_id, str) or call_id not in pending:
                        raise ValueError("Unpaired stored tool result")
                    pending.remove(call_id)
                items.append(item)
                history.record_items([item])
            # A result queued for the next step legitimately has no history entry yet.
            # Validate pairing without prematurely adding the queued item to history.
            for event in events:
                item = event["item"]
                if "_filesystem_policy" in item:
                    item["_filesystem_policy"] = parse_policy(item["_filesystem_policy"], legacy=True)
                if item["type"] in {"function_call_output", "tool_search_output"}:
                    call_id = item.get("call_id")
                    if not isinstance(call_id, str) or call_id not in pending:
                        raise ValueError("Unpaired or duplicate queued tool result")
                    pending.remove(call_id)
                elif item["type"] in {"function_call", "tool_search_call"}:
                    raise ValueError("Pending input cannot introduce a tool call")
            if pending:
                raise ValueError("Unfinished stored tool calls; refusing to replay them")
            return ResumedHistory(meta, items, step_number, events, generators,
                                  world_state, history, memory, context_budget, mcp_context, custom_tools, section_states)
        except (json.JSONDecodeError, KeyError, TypeError):
            raise ValueError("Corrupt rollout; no records were changed") from None

    @classmethod
    def list_threads(cls, root: Path) -> list[dict]:
        results = []
        for path in sorted(root.glob("*/rollout.jsonl")):
            try:
                thread_id = path.parent.name
                cls.path_for(root, thread_id)
                with path.open(encoding="utf-8") as stream:
                    row = json.loads(stream.readline())
                meta = row["payload"]
                if row["type"] != "session_meta" or meta["thread_id"] != thread_id:
                    raise ValueError("Invalid metadata")
                results.append({"thread_id": thread_id, "created_at": meta["created_at"],
                                "cwd": meta["cwd"], "path": str(path)})
            except (ValueError, KeyError, TypeError, OSError):
                results.append({"thread_id": path.parent.name, "error": "unreadable metadata"})
        return results

    def append(self, kind: str, payload: dict[str, Any], *, history_id: str | None = None) -> None:
        if self.closed or self.failed:
            raise RuntimeError("Rollout writer is closed or failed")
        row = {"timestamp": timestamp(), "type": kind, "payload": payload}
        if history_id is not None:
            row["history_id"] = history_id
        line = json.dumps(row, ensure_ascii=False) + "\n"
        try:
            self._stream.write(line)
            self._stream.flush()
            os.fsync(self._stream.fileno())
        except BaseException:
            self.failed = True
            raise

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._stream.close()  # Also releases the advisory lock.
