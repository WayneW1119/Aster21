"""Agent-facing tools for creating and managing Session event sources."""

import json
import math
import time
from datetime import datetime
from pathlib import Path

from ..event_generators import EventGeneratorManager
from ..event_generators.timer import TimerGenerator
from ..event_generators.program import ProgramGenerator
from ..event_generators.program_testing import test_program
from .registry import Tool, ToolRegistry


def register_generator_tools(registry: ToolRegistry, manager: EventGeneratorManager,
                             workspace: Path | None = None) -> None:
    root = (workspace or Path.cwd()).resolve()

    def program_path(script_path: str) -> Path:
        if not isinstance(script_path, str) or not script_path:
            raise ValueError("script_path must be a nonempty Python file path")
        path = (root / script_path).resolve(strict=True)
        if not path.is_file() or path.suffix != ".py":
            raise ValueError("script_path must identify an existing .py file")
        return path

    async def register_program_generator(script_path: str, args: list[str] | None = None,
                                         mode: str = "service", name: str | None = None,
                                         notes: str | None = None) -> str:
        path = program_path(script_path)
        return json.dumps(manager.register(ProgramGenerator(str(path), str(root), args, mode), name=name, notes=notes),
                          ensure_ascii=False)

    async def test_program_generator(script_path: str, args: list[str] | None = None,
                                     timeout_ms: int = 5000, mode: str = "once") -> str:
        return json.dumps(await test_program(program_path(script_path), root, args,
                                             timeout_ms, mode, sandbox=manager.sandbox), ensure_ascii=False)

    async def restart_program_generator(generator_id: str) -> str:
        manager.check_agent_control(generator_id)
        return json.dumps(await manager.restart_program(generator_id), ensure_ascii=False)

    async def agent_time(utc_offset: str | None = None) -> str:
        if utc_offset is not None:
            manager.clock.set(utc_offset)
        now = datetime.now(manager.clock.zone).isoformat()
        return json.dumps({"utc_offset": now[-6:], "now": now})

    async def create_timer(delay_ms: int | None = None, message: str = "", interval_ms: int | None = None,
                           repeat_count: int = 1, at: str | None = None,
                           name: str | None = None, notes: str | None = None) -> str:
        if (delay_ms is None) == (at is None):
            raise ValueError("Provide exactly one of delay_ms or at")
        deadline = None
        if at is not None:
            deadline = manager.clock.deadline(at)
            delay_ms = math.ceil((deadline - time.time()) * 1000)
            if delay_ms <= 0:
                raise ValueError("Timer deadline must be in the future")
        timer = TimerGenerator(delay_ms, message, interval_ms, repeat_count)
        if deadline is not None:
            timer.next_fire_at = deadline
        return json.dumps(manager.register(timer, name=name, notes=notes), ensure_ascii=False)

    async def list_event_generators(include_finished: bool = False) -> str:
        return json.dumps(manager.list(include_finished), ensure_ascii=False)

    async def cancel_event_generator(generator_id: str) -> str:
        manager.check_agent_control(generator_id)
        return json.dumps(await manager.cancel(generator_id), ensure_ascii=False)

    for name, description, properties, required, handler in [
        ("test_program_generator", "Run a Python program self-test using the same interpreter, SDK, "
         "workspace cwd, IPC protocol, validation and rate limits as registered program generators. "
         "No registration and no real program events: emit_event receipts have scope=test, and "
         "events are collected only in this tool result. Uses a fresh temporary state_directory, "
         "discarded after testing; never uses a registered source's state. Returns status, exit_code, "
         "last 64 KiB of combined logs and at most 20 events. Timeout/cancellation stops the owned "
         "process group. timed_out is not a pass. Not a sandbox: file/network side effects are real. "
         "Inspect the script and use finite self-test arguments where supported.",
         {"script_path": {"type": "string", "description": "Existing Python script, relative to workspace or absolute. Read and inspect before executing."},
          "args": {"type": "array", "items": {"type": "string"}, "description": "Optional arguments, e.g. [\"--self-test\"]. Up to 64 strings; no shell expansion."},
          "timeout_ms": {"type": "integer", "description": "Test time limit in milliseconds, 100-30000; defaults to 5000. After expiry the test process group is stopped."},
          "mode": {"type": "string", "enum": ["once", "service"], "description": "Default once: finite self-test exit 0 completes. service: any exit fails; reaching the time limit is timed_out, not successful verification."}},
         ["script_path"], test_program_generator),
        ("register_program_generator", "Register and launch an agent-maintained Python event source. "
         "Returns a generator_id, state_dir and bounded log_path, not a guarantee of successful startup. "
         "The manager uses the harness Python interpreter and workspace cwd. Programs import "
         "emit_event and state_directory from agent_harness.events. Inspect and test first using "
         "test_program_generator; unlike isolated testing, registration enables real events. "
         "Source identity is manager-bound. "
         "Emissions enqueue program_event data, never authenticated human messages. Orderly resume "
         "restarts active programs from their entrypoint with the same state directory, not PID. "
         "5 attempts/10s or 20/60s triggers pause and one alert; no automatic restart.",
         {"script_path": {"type": "string", "description": "Existing Python script, relative to workspace or absolute. You own and maintain this program; inspect/test before registration."},
          "args": {"type": "array", "items": {"type": "string"}, "description": "Optional command-line arguments, up to 64 strings. No shell expansion."},
          "mode": {"type": "string", "enum": ["once", "service"], "description": "Default service: any exit is a failure notification. once: exit 0 completes silently; nonzero exit is a failure."}},
         ["script_path"], register_program_generator),
        ("restart_program_generator", "Explicitly restart a paused, failed, or completed program "
         "with its existing generator_id and state directory after inspection/repair. Does not "
         "restart active or cancelled sources. No events are replayed automatically.",
         {"generator_id": {"type": "string", "description": "Existing program generator_id from registration/listing."}},
         ["generator_id"], restart_program_generator),
        ("agent_time", "Read your current time and fixed UTC offset. Supply utc_offset to change your own default, saved across Thread resumes.",
         {"utc_offset": {"type": "string", "description": "Fixed UTC offset, e.g. +8, -05:00 or +05:30. Default +00:00."}}, [], agent_time),
        ("create_timer", "Register a Session-local timer with the event generator manager and "
         "return its record including generator_id. This does not block: timer_fired events "
         "enqueue the supplied message for a later step. Persistent Threads restore timers on "
         "orderly resume, preserving deadlines and progress. Overdue timers fire once, not a burst. "
         "In-memory Sessions do not preserve timers after close.",
         {"delay_ms": {"type": "integer", "description": "Delay before the first event in milliseconds; at least 1. Mutually exclusive with at."},
          "at": {"type": "string", "description": "Future ISO date and time, e.g. 2027-01-05 09:00. Uses your agent_time offset unless an explicit offset is supplied. Mutually exclusive with delay_ms."},
          "message": {"type": "string", "description": "Reminder or task context to include in each timer_fired event. Nonempty, at most 4000 characters; does not grant new authorization."},
          "interval_ms": {"type": "integer", "description": "Delay after each emission before the next. Required for repeats and must be at least 1000 ms. No catch-up bursts for missed intervals."},
          "repeat_count": {"type": "integer", "description": "Total event count, including the first. Defaults to 1 (one-shot); 0 repeats until cancelled. Persistent Thread suspension preserves progress; in-memory Session close stops the timer. Maximum 10000. Values other than 1 require interval_ms."}},
         ["message"], create_timer),
        ("list_event_generators", "List active and paused Session event generators. Set include_finished=true "
         "to include recent completed/cancelled/failed records. Does not create generators.",
         {"include_finished": {"type": "boolean", "description": "Defaults to false (active and paused). True also includes retained completed, cancelled, and failed records; not a permanent history."}}, [], list_event_generators),
        ("cancel_event_generator", "Cancel future emissions from a generator_id. "
         "Program sources terminate their owned process group. Command-completion monitors "
         "do not stop the separate exec_command. Does not remove already enqueued events.",
         {"generator_id": {"type": "string", "description": "Event source identifier returned by registration or listing, not a command session_id."}}, ["generator_id"], cancel_event_generator),
    ]:
        if name in {"create_timer", "register_program_generator"}:
            properties.update(name={"type": "string", "description": "Optional display name (up to 128 characters)."},
                              notes={"type": "string", "description": "Optional purpose or maintenance notes (up to 1024 characters)."})
        registry.register(Tool({"type": "function", "name": name, "description": description,
            "strict": False, "parameters": {"type": "object", "properties": properties,
            "required": required, "additionalProperties": False}}, handler))
