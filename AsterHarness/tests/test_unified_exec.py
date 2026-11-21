import asyncio
import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_harness.core.client import ScriptedClient
from agent_harness.core.session import Session
from agent_harness.core.tools.handlers import build_file_tools
from agent_harness.core.tools.registry import ToolRegistry
from agent_harness.core.tools.router import ToolRouter
from agent_harness.core.tools.unified_exec import register_exec_tools
from agent_harness.core.unified_exec import UnifiedExecProcessManager


def command(code):
    return f"{shlex.quote(sys.executable)} -u -c {shlex.quote(code)}"


def call(name, arguments, ident):
    return {"type": "function_call", "name": name, "arguments": json.dumps(arguments),
            "call_id": ident}


class UnifiedExecTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = UnifiedExecProcessManager(self.root)
        self.addAsyncCleanup(self.manager.close)

    async def test_fast_command_default_workdir_and_exit_code(self):
        result = await self.manager.exec_command(command("import os; print(os.getcwd())"), login=False)
        self.assertEqual(result["exit_code"], 0)
        self.assertNotIn("session_id", result)
        self.assertEqual(result["output"].strip(), str(self.root))
        self.assertFalse(self.manager.processes)

    async def test_yield_preserves_process_and_polls_only_new_output(self):
        result = await self.manager.exec_command(command(
            "import time; print('START'); time.sleep(.65); print('FINISH')"),
            yield_time_ms=250, login=False)
        self.assertIn("START", result["output"])
        self.assertNotIn("exit_code", result)
        handle = result["session_id"]
        self.assertIsNone(self.manager.processes[handle].process.returncode)
        final = await self.manager.write_stdin(handle)
        self.assertEqual(final["exit_code"], 0)
        self.assertIn("FINISH", final["output"])
        self.assertNotIn("START", final["output"])
        with self.assertRaisesRegex(ValueError, "Unknown"):
            await self.manager.write_stdin(handle)

    async def test_output_stderr_and_nonzero_exit(self):
        result = await self.manager.exec_command(command(
            "import sys; print('normal'); print('error', file=sys.stderr); sys.exit(7)"), login=False)
        self.assertEqual(result["exit_code"], 7)
        self.assertIn("normal", result["output"])
        self.assertIn("error", result["output"])

    async def test_utf8_character_split_across_collects(self):
        result = await self.manager.exec_command(command(
            "import os,time; os.write(1, bytes([228])); time.sleep(.5); os.write(1, bytes([189,160]))"),
            yield_time_ms=250, login=False)
        self.assertEqual(result["output"], "")
        final = await self.manager.write_stdin(result["session_id"])
        self.assertEqual(final["output"], "你")

    async def test_two_background_commands_have_independent_output(self):
        results = await asyncio.gather(*(
            self.manager.exec_command(f"sleep .5; printf {label}", yield_time_ms=250, login=False)
            for label in ("ONE", "TWO")))
        self.assertNotEqual(results[0]["session_id"], results[1]["session_id"])
        finals = await asyncio.gather(*(
            self.manager.write_stdin(result["session_id"]) for result in results))
        self.assertEqual([result["output"] for result in finals], ["ONE", "TWO"])

    async def test_tty_input_and_ctrl_c(self):
        result = await self.manager.exec_command(command("print('READY'); print('GOT=' + input())"),
                                                 tty=True, yield_time_ms=250, login=False)
        self.assertIn("READY", result["output"])
        final = await self.manager.write_stdin(result["session_id"], "hello\n", yield_time_ms=1000)
        self.assertEqual(final["exit_code"], 0)
        self.assertIn("GOT=hello", final["output"])
        result = await self.manager.exec_command("sleep 30", tty=True, yield_time_ms=250, login=False)
        final = await self.manager.write_stdin(result["session_id"], "\x03", yield_time_ms=1000)
        self.assertIn("exit_code", final)
        self.assertNotEqual(final["exit_code"], 0)

    async def test_non_tty_input_error_does_not_destroy_process(self):
        result = await self.manager.exec_command("sleep 30", yield_time_ms=250, login=False)
        with self.assertRaisesRegex(ValueError, "tty=true"):
            await self.manager.write_stdin(result["session_id"], "hello")
        self.assertIn(result["session_id"], self.manager.processes)

    async def test_workspace_override_and_invalid_arguments(self):
        (self.root / "sub").mkdir()
        result = await self.manager.exec_command("pwd", workdir="sub", login=False)
        self.assertEqual(result["output"].strip(), str(self.root / "sub"))
        for kwargs in ({"cmd": ""}, {"cmd": "true", "yield_time_ms": True},
                       {"cmd": "true", "yield_time_ms": -1},
                       {"cmd": "true", "max_output_tokens": 0},
                       {"cmd": "true", "tty": "yes"}, {"cmd": "true", "workdir": 2},
                       {"cmd": "true", "shell": "bash"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await self.manager.exec_command(**kwargs)

    async def test_bounded_output_and_pipe_does_not_deadlock(self):
        result = await self.manager.exec_command(command("print('x' * 2000000); print('THE_END')"),
                                                 max_output_tokens=20, login=False)
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("truncated", result["output"])
        self.assertIn("THE_END", result["output"])
        self.assertLess(len(result["output"]), 250)

    async def test_close_terminates_background_and_is_idempotent(self):
        result = await self.manager.exec_command("sleep 30", yield_time_ms=250, login=False)
        entry = self.manager.processes[result["session_id"]]
        await self.manager.close()
        self.assertIsNotNone(entry.process.returncode)
        self.assertTrue(entry.monitor.done())
        self.assertFalse(self.manager.processes)
        await self.manager.close()
        with self.assertRaisesRegex(ValueError, "closed"):
            await self.manager.exec_command("true")

    async def test_router_errors_and_schema_optional_parameters(self):
        registry = ToolRegistry()
        register_exec_tools(registry, self.manager)
        router = ToolRouter(registry)
        result = await router.dispatch_async(call("write_stdin", {"session_id": 999}, "bad"))
        self.assertIn("error", result["output"])
        result = await router.dispatch_async(call("exec_command", {"cmd": "true", "unknown": 1}, "bad2"))
        self.assertIn("error", result["output"])
        self.assertEqual(registry.specs()[0]["parameters"]["required"], ["cmd"])

    async def test_session_result_is_queued_not_appended_until_next_step(self):
        events = []
        session = Session(ScriptedClient([
            [call("exec_command", {"cmd": "printf EVENT_OK", "login": False}, "exec")],
            [call("send_user_message", {"text": "done", "wake_model": False}, "send")],
        ]), build_file_tools(self.root), workspace=self.root,
            trace=lambda e, d: events.append((e, d)))
        self.addAsyncCleanup(session.close)
        await session.post_user_message("run")
        await session.run_until_idle()
        queued = [d for e, d in events if e == "event_enqueued"]
        self.assertEqual([d["queued_event"]["kind"] for d in queued], ["user_message", "tool_result"])
        result = json.loads(queued[1]["queued_event"]["item"]["output"])
        self.assertEqual(result["output"], "EVENT_OK")
        requests = [d["prompt"] for e, d in events if e == "model_request"]
        self.assertEqual(len(requests[0]["input"]), 1)
        self.assertEqual(requests[1]["input"][-1]["type"], "function_call_output")

    async def test_background_exit_does_not_wake_but_session_close_cleans_up(self):
        events = []
        session = Session(ScriptedClient([
            [call("exec_command", {"cmd": command("import time; time.sleep(.5); print('DONE')"),
                                   "yield_time_ms": 250, "login": False}, "exec")],
            [call("send_user_message", {"text": "background started", "wake_model": False}, "send")],
        ]), build_file_tools(self.root), workspace=self.root,
            trace=lambda e, d: events.append((e, d)))
        self.addAsyncCleanup(session.close)
        await session.post_user_message("run")
        await session.run_until_idle()
        entry = next(iter(session.process_manager.processes.values()))
        await asyncio.wait_for(entry.complete.wait(), 2)
        self.assertTrue(session.input_queue.empty())
        self.assertEqual(session.state.step_number, 2)
        self.assertEqual(sum(e == "event_enqueued" for e, _ in events), 2)
        await session.close()
        self.assertFalse(session.process_manager.processes)

    async def test_user_interjection_during_exec_wait_and_later_poll(self):
        entered = asyncio.Event()
        received = asyncio.Queue()
        session = Session(ScriptedClient([]), build_file_tools(self.root), workspace=self.root,
                          send_message=received.put_nowait)
        self.addAsyncCleanup(session.close)
        requests = []
        def respond(prompt):
            from agent_harness.core.client_common import ModelResponse
            requests.append(prompt)
            if len(requests) == 1:
                return ModelResponse([call("exec_command", {"cmd": "sleep .6; printf DONE",
                    "yield_time_ms": 250, "login": False}, "exec")])
            if len(requests) == 2:
                output = next(i for i in prompt.input if i.get("call_id") == "exec"
                              and i["type"] == "function_call_output")
                return ModelResponse([call("write_stdin", {
                    "session_id": json.loads(output["output"])["session_id"]}, "poll")])
            return ModelResponse([call("send_user_message", {"text": "B received; DONE", "wake_model": False}, "send")])
        session.client.respond = respond
        def trace(event, data):
            if event == "tool_started" and data["call"]["name"] == "exec_command":
                entered.set()
        session.trace = trace
        await session.post_user_message("A")
        worker = asyncio.create_task(session.run())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await session.post_user_message("B")
            self.assertEqual(await asyncio.wait_for(received.get(), 3), "B received; DONE")
            self.assertTrue(any("<terminal_message>" in i.get("text", "")
                                and json.loads(i["text"].split("\n")[1])["text"] == "B"
                                for i in requests[1].input))
            self.assertFalse(worker.done())
        finally:
            await session.close()
            await worker

    async def test_cancelling_scheduler_terminates_running_command(self):
        started = asyncio.Event()
        session = Session(ScriptedClient([
            [call("exec_command", {"cmd": "sleep 30", "yield_time_ms": 30000,
                                   "login": False}, "exec")],
        ]), build_file_tools(self.root), workspace=self.root)
        self.addAsyncCleanup(session.close)
        original = session.process_manager._collect
        async def collect(*args):
            started.set()
            return await original(*args)
        session.process_manager._collect = collect
        await session.post_user_message("run")
        worker = asyncio.create_task(session.run())
        await asyncio.wait_for(started.wait(), 2)
        entry = next(iter(session.process_manager.processes.values()))
        worker.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await worker
        self.assertEqual(session.state.status, "failed")
        self.assertIsNotNone(entry.process.returncode)
        self.assertTrue(entry.monitor.done())
        self.assertFalse(session.process_manager.processes)

    async def test_cancellation_during_spawn_does_not_orphan_child(self):
        spawned, release = asyncio.Event(), asyncio.Event()
        children = []
        original = asyncio.create_subprocess_exec
        async def delayed_spawn(*args, **kwargs):
            child = await original(*args, **kwargs)
            children.append(child)
            spawned.set()
            await release.wait()
            return child
        with patch("asyncio.create_subprocess_exec", side_effect=delayed_spawn):
            task = asyncio.create_task(self.manager.exec_command("sleep 30", login=False))
            await asyncio.wait_for(spawned.wait(), 2)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNotNone(children[0].returncode)
        self.assertFalse(self.manager.processes)
