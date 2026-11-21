import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

from .config import load_env_file
from .core.mcp.config import load_mcp_config
from .core.client import ResponsesClient, ScriptedClient
from .core.session import Session, SessionConfiguration
from .core.thread_manager import ThreadManager
from .core.trace import CompositeTrace, ConsoleTrace, JsonlTrace


class StdinReader:
    """Unix terminal/pipe input; retain pasted lines without TextIO read-ahead."""

    def __init__(self) -> None:
        self.buffer = b""
        self.eof = False

    async def read_line(self) -> str:
        if os.name == 'nt':
            from ._windows import StdinLines
            if not hasattr(self, '_windows_reader'):
                self._windows_reader = StdinLines()
            return await self._windows_reader.readline()
        loop = asyncio.get_running_loop()
        descriptor = sys.stdin.fileno()
        while b"\n" not in self.buffer and not self.eof:
            ready = loop.create_future()
            def on_readable() -> None:
                if not ready.done():
                    ready.set_result(None)
            loop.add_reader(descriptor, on_readable)
            try:
                await ready
                chunk = os.read(descriptor, 65536)
            finally:
                loop.remove_reader(descriptor)
            self.eof = not chunk
            self.buffer += chunk
        line, separator, remaining = self.buffer.partition(b"\n")
        self.buffer = remaining
        return (line + separator).decode(sys.stdin.encoding or "utf-8", errors="replace")


async def run_cli(session: Session, args: argparse.Namespace) -> bool | None:
    if args.prompt is not None or args.demo:
        try:
            await session.post_user_message(args.prompt or "请读取 README.md 的开头并解释这个项目。")
            await session.run_until_settled()
        finally:
            await session.close()
        return
    print("Session 已启动；可在工作中继续发消息。/quit 或 EOF 退出。", flush=True)
    reader = getattr(args, "stdin_reader", None) or StdinReader()
    reload_requested = False
    reload_waiter = (asyncio.create_task(args.wait_reload())
                     if getattr(args, "wait_reload", None) else None)
    worker = asyncio.create_task(session.run())
    try:
        while True:
            incoming = asyncio.create_task(reader.read_line())
            try:
                done, _ = await asyncio.wait({incoming, worker, *([reload_waiter] if reload_waiter else [])},
                                             return_when=asyncio.FIRST_COMPLETED)
                if worker in done:
                    await worker
                    break
                if reload_waiter is not None and reload_waiter in done and incoming not in done:
                    reload_waiter.result()
                    reload_requested = True
                    break
                line = incoming.result()
                if not line or line.strip() == "/quit":
                    break
                if line.strip():
                    await session.post_user_message(line.rstrip("\n"))
            finally:
                if not incoming.done():
                    incoming.cancel()
                    await asyncio.gather(incoming, return_exceptions=True)
    finally:
        if reload_waiter is not None:
            reload_waiter.cancel()
            await asyncio.gather(reload_waiter, return_exceptions=True)
        if session.rollout is not None:
            await session.suspend()
        else:
            await session.close()
        await worker
    return reload_requested


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        from .auth_cli import main as auth_main
        auth_main(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] in {"suite", "agent", "start", "resume", "threads", "status", "config", "instructions", "reload"}:
        from .management import main as management_main
        management_main(sys.argv[1:])
        return
    from .agent_config import harness_home
    parser = argparse.ArgumentParser(description="Aster: a live event-driven Agent Session")
    parser.add_argument("--demo", action="store_true", help="Use an offline scripted model")
    parser.add_argument("--env-file", type=Path,
                        default=Path.cwd() / ".env",
                        help="Model environment file (defaults to .env in the current directory)")
    parser.add_argument("--allow-http", action="store_true",
                        help="Allow plaintext HTTP to a trusted internal model endpoint")
    parser.add_argument("--workspace", type=Path, help="New Thread workspace; resume uses its saved workspace")
    parser.add_argument("--instructions-file", type=Path,
                        help="Copy user-written agent instructions into a NEW Thread")
    parser.add_argument("--thread-dir", type=Path,
                        default=harness_home() / "threads",
                        help="Persistent Thread store (contains full history, not just debug logs)")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--resume", metavar="THREAD_ID", help="Resume a cleanly closed Thread")
    operation.add_argument("--list-threads", action="store_true", help="List Threads without calling a model")
    parser.add_argument("--trace-dir", type=Path, help="Opt in to full request/output logs")
    parser.add_argument("--debug", action="store_true",
                        help="Show full prompts, API bodies, tool events, and history snapshots")
    parser.add_argument("--prompt", help="Batch mode: wait for queue and event generators to settle")
    parser.add_argument("--context-budget-warning-tokens", type=int,
                        help="New Thread input-token reminder threshold (default: 100000; 0 disables)")
    parser.add_argument("--mcp-config", type=Path,
                        help="New Thread stdio/Streamable HTTP MCP servers TOML; resume uses saved settings")
    parser.add_argument("--code-mode", action="store_true",
                        help="Enable optional JavaScript composition tools for a new Thread")
    parser.add_argument("--skills-dir", action="append", default=[], metavar="PATH",
                        help="Additional local skill directory for a new Thread; repeatable; workspace/skills is always scanned")
    parser.add_argument('--sandbox', choices=('workspace-write','off'), default=None,
                        help='New Thread filesystem sandbox (default: workspace-write; Linux/WSL2)')
    parser.add_argument('--filesystem-policy', type=Path, help='New Thread filesystem rules JSON')
    parser.add_argument('--deny-path', action='append', default=[], help='Deny a path subtree; repeatable')
    parser.add_argument('--read-only-path', action='append', default=[], help='Read-only carveout; repeatable')
    parser.add_argument('--writable-root', action='append', default=[], help='Additional writable root; repeatable')
    args = parser.parse_args()
    fs_flags=args.filesystem_policy or args.deny_path or args.read_only_path or args.writable_root
    if (args.sandbox is not None or fs_flags) and (args.resume or args.list_threads):
        parser.error('Filesystem options are for new Threads; resume uses saved permissions')
    if args.sandbox=='off' and fs_flags:
        parser.error('Filesystem rules cannot be combined with --sandbox off')
    if args.filesystem_policy and (args.deny_path or args.read_only_path or args.writable_root):
        parser.error('Use a policy file or individual path flags, not both')
    filesystem_policy=None if args.sandbox=='off' else {'rules':[
        {'path':path,'access':access} for access,paths in [('deny',args.deny_path),('read',args.read_only_path),('write',args.writable_root)] for path in paths]}
    if args.filesystem_policy:
        try:
            from .core.sandbox import parse_policy
            filesystem_policy=parse_policy(json.loads(args.filesystem_policy.read_text(encoding="utf-8")))
            if filesystem_policy is None:raise ValueError('Policy file must contain a rules object')
        except (OSError,ValueError) as error:parser.error(str(error))
    if args.skills_dir and (args.resume or args.list_threads):
        parser.error("--skills-dir is for new Threads; resume uses saved directories")
    if args.code_mode and (args.resume or args.list_threads):
        parser.error("--code-mode is for new Threads; resume uses saved configuration")
    mcp_servers = {}
    if args.mcp_config is not None:
        if args.resume or args.list_threads:
            parser.error("--mcp-config is for new Threads; resume uses saved settings")
        try:
            mcp_servers = load_mcp_config(args.mcp_config)
        except (ValueError, OSError) as error:
            parser.error(str(error))
    if args.context_budget_warning_tokens is not None:
        if args.context_budget_warning_tokens < 0:
            parser.error("--context-budget-warning-tokens must be nonnegative")
        if args.resume or args.list_threads:
            parser.error("--context-budget-warning-tokens is for new Threads; resume uses saved settings")
    if args.instructions_file is not None and (args.resume or args.list_threads):
        parser.error("--instructions-file is for new Threads; edit the existing instructions.md to update one")
    manager = ThreadManager(args.thread_dir)
    if args.list_threads:
        try:
            print(json.dumps(manager.list_threads(), ensure_ascii=False, indent=2))
        except (ValueError, OSError) as error:
            parser.error(str(error))
        return
    if args.resume and args.workspace is not None:
        parser.error("--resume uses the saved workspace; do not supply --workspace")
    if args.demo:
        client = ScriptedClient([
            [{"type": "function_call", "call_id": f"demo_read_{uuid4().hex}", "name": "read_file",
              "arguments": json.dumps({"path": "README.md", "start_line": 1,
                                       "end_line": 20})}],
            [{"type": "function_call", "call_id": f"demo_send_{uuid4().hex}", "name": "send_user_message",
              "arguments": json.dumps({"text": "离线演示结束：真实文件读取工具已被执行，"
                 "结果已进入第二次模型请求的 input。注意：这段回答是预设脚本，"
                 "不是模型对文件的理解。请在 trace 中查看真实读取结果。"}, ensure_ascii=False)}],
            [{"type": "function_call", "call_id": f"demo_wait_{uuid4().hex}",
              "name": "wait_for_event", "arguments": "{}"}],
        ])
    else:
        try:
            load_env_file(args.env_file)
        except (ValueError, OSError):
            parser.error("Could not load .env; check file permissions and KEY=value syntax")
        model, key = os.getenv("HARNESS_MODEL"), os.getenv("OPENAI_API_KEY")
        if not model or not key:
            parser.error("Use --demo, or set HARNESS_MODEL and OPENAI_API_KEY")
        try:
            client = ResponsesClient(os.getenv("HARNESS_RESPONSES_URL",
                                               "https://api.openai.com/v1/responses"),
                                     model, key, allow_http=args.allow_http)
        except ValueError as error:
            parser.error(str(error))
    sinks = []
    if args.debug:
        print("DEBUG: full prompts, responses and file contents will be displayed. "
              "Auth headers are excluded; do not read credential files. "
              "Trace records are distinct from queued harness input events.")
        sinks.append(ConsoleTrace())
    if args.trace_dir:
        path = args.trace_dir / f"run-{uuid4().hex}.jsonl"
        sinks.append(JsonlTrace(path))
        print(f"Trace: {path} (contains prompts and file contents; no auth headers)")
    trace = CompositeTrace(sinks)
    if isinstance(client, ResponsesClient):
        client.trace = trace
    try:
        callbacks = {"trace": trace,
                     "private_paths": (harness_home() / "credentials", harness_home() / "agents", args.env_file),
                     "send_message": lambda text: print(f"\nAgent: {text}", flush=True)}
        if args.resume:
            session = manager.resume(client, args.resume, **callbacks)
        else:
            agent_instructions = (args.instructions_file.read_text(encoding="utf-8")
                                  if args.instructions_file is not None else None)
            session = manager.create(client, args.workspace or Path.cwd(),
                                     agent_instructions=agent_instructions,
                                     configuration=SessionConfiguration(
                                         context_budget_warning_tokens=(
                                             args.context_budget_warning_tokens or None)
                                         if args.context_budget_warning_tokens is not None else 100_000,
                                         mcp_servers=mcp_servers, code_mode_enabled=args.code_mode,
                                         skill_directories=tuple(args.skills_dir),
                                         filesystem_policy=filesystem_policy),
                                     **callbacks)
        print(f"Thread: {session.thread_id}\nRollout: {session.rollout.path}\n"
              f"Instructions: {session.agent_context.instructions_path}\n"
              f"Memory: {session.agent_context.memory_root}\n"
              "History is persisted locally; no auth headers are stored.", flush=True)
        try:
            asyncio.run(run_cli(session, args))
        finally:
            # A setup/event-loop failure must not leave a writer lock held.
            session.rollout.close()
    except KeyboardInterrupt:
        parser.exit(130, "Interrupted\n")
    except (RuntimeError, ValueError, OSError) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
