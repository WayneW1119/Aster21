"""Codex-style command tools; scheduling remains Session's responsibility."""

import json
import os

from ..unified_exec import UnifiedExecProcessManager
from ..event_generators import EventGeneratorManager
from ..event_generators.command_completion import CommandCompletionGenerator
from .registry import Tool, ToolRegistry


def register_exec_tools(registry: ToolRegistry, manager: UnifiedExecProcessManager,
                        generators: EventGeneratorManager | None = None) -> None:
    async def exec_command(notify_on_completion: bool = False, **kwargs) -> str:
        if type(notify_on_completion) is not bool:
            raise ValueError("notify_on_completion must be a boolean")
        if notify_on_completion:
            if generators is None:
                raise ValueError("Completion generators are unavailable")
            generators.ensure_capacity()  # Fail before starting a command if full/closed.
        result = await manager.exec_command(**kwargs)
        if notify_on_completion and "session_id" in result:
            handle = result["session_id"]
            try:
                record = generators.register(CommandCompletionGenerator(handle, manager.processes[handle]))
                result["generator_id"] = record["generator_id"]
            except ValueError as error:
                # Shutdown/capacity can change while the command is awaiting output.
                # Do not hide an already-started command behind a tool-level error.
                result["notification_error"] = str(error)
        return json.dumps(result, ensure_ascii=False)

    async def write_stdin(**kwargs) -> str:
        return json.dumps(await manager.write_stdin(**kwargs), ensure_ascii=False)

    async def list_processes() -> str:
        return json.dumps(await manager.list_processes(), ensure_ascii=False)

    async def stop_process(**kwargs) -> str:
        return json.dumps(await manager.stop_process(**kwargs), ensure_ascii=False)

    for name, description, properties, required, handler in [
        ("exec_command", "Run a shell command, returning collected output and either exit_code "
         "if finished or session_id if still running. session_id is a tool handle, not an OS PID; "
         "use write_stdin to collect later output. Commands use the latest host-configured sandbox mode and permissions; consult the current environment context. "
         "Use \"$HARNESS_PYTHON\" to run the harness Python interpreter; PYTHONPATH includes its SDK. "
         "Programs can import emit_event from agent_harness.events, but ordinary commands have no "
         "generator IPC or state directory: emission raises RuntimeError, never queues a program event. "
         "Use test_program_generator for isolated emission tests, register_program_generator for real events. "
         "Processes are not restored across restart; saved active completion monitors report "
         "generator_unavailable on resume, not a successful completion.",
         {"cmd": {"type": "string", "description": "Shell command to execute."},
          "workdir": {"type": "string", "description": "Working directory. Defaults to the Session workspace; relative paths resolve from that workspace."},
          "yield_time_ms": {"type": "integer", "description": "Wait before returning output. Defaults to 10000 ms; effective range 250-30000 ms. Limits waiting, not command lifetime."},
          "max_output_tokens": {"type": "integer", "description": "Approximate output token budget for this call. Defaults to 10000; capped at 100000. Output may be truncated."},
          "shell": {"type": "string", "description": ("Shell binary to launch. Defaults to the host PowerShell on Windows." if os.name == "nt" else "Shell binary to launch. Defaults to /bin/bash.")},
          "login": {"type": "boolean", "description": "Use login-shell semantics. Defaults to true."},
          "tty": {"type": "boolean", "description": "True allocates a PTY and enables stdin interaction. False or omitted uses plain pipes with no interactive stdin."},
          "notify_on_completion": {"type": "boolean", "description": "Defaults to false. If true and still running after the initial wait, register a command-completion event generator and return generator_id. A later command_completed event reports status but does not consume output; collect remaining output with write_stdin. Fast commands create no generator. If notification_error is returned, the command started but no notification was registered."}},
         ["cmd"], exec_command),
        ("write_stdin", "Write to an existing exec_command session or collect its recent output. "
         "Returns only newly collected output, plus session_id while running or exit_code when finished. "
         "Completion removes the process handle. Polling does not cancel a registered completion notification.",
         {"session_id": {"type": "integer", "description": "Running command handle returned by exec_command, not an OS PID or generator_id."},
          "chars": {"type": "string", "description": "Defaults to empty, which polls without writing. A standalone Ctrl-C (U+0003) interrupts the owned command with or without tty. Other nonempty input requires tty=true on the original command."},
          "yield_time_ms": {"type": "integer", "description": "Wait before returning output. Empty polls default to 5000 ms, effective range 5000-300000 ms. Nonempty writes default to 250 ms, effective range 250-30000 ms. Does not impose a command timeout."},
          "max_output_tokens": {"type": "integer", "description": "Approximate output token budget for this call. Defaults to 10000; capped at 100000. Output may be truncated."}},
         ["session_id"], write_stdin),
        ("list_processes", "List command handles owned by this live Session, including commands "
         "started by the host through its process manager. Returns session_id, command, workdir, "
         "tty, status, exit_code and output_pending. Does not consume output. These are tool handles, "
         "not OS PIDs. Other Sessions, program generators and arbitrary host processes are not listed. "
         "Use these handles with write_stdin or stop_process instead of trying ps/kill in another "
         "command sandbox. Handles do not survive Session restart.", {}, [], list_processes),
        ("stop_process", "Stop one command owned by this live Session using its tool session_id, "
         "not an OS PID. The host sends TERM to the owned process group, then KILL if needed. "
         "Returns status stopped/already_exited and final exit_code/output; termination_pending "
         "means exit is not confirmed. Final collection invalidates the handle. Cannot stop "
         "another Session's process or adopt arbitrary host PIDs. For program generators, use "
         "cancel_event_generator or restart_program_generator. Registered command-completion "
         "notifications remain active and report the actual exit.",
         {"session_id": {"type": "integer", "description": "Owned command handle from exec_command or list_processes; not an OS PID."},
          "max_output_tokens": {"type": "integer", "description": "Approximate budget for remaining output. Defaults to 10000; capped at 100000."}},
         ["session_id"], stop_process),
    ]:
        registry.register(Tool({"type": "function", "name": name, "description": description,
            "strict": False, "parameters": {"type": "object", "properties": properties,
            "required": required, "additionalProperties": False}}, handler))
