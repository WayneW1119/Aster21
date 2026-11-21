"""SDK import and emission routing boundaries, using real local child processes."""

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_harness.core.client import ScriptedClient
from agent_harness.core.session import Session
from agent_harness.core.session.step import build_prompt
from agent_harness.core.tools.handlers import build_file_tools
from agent_harness.events import emit_event, state_directory


SDK = "from agent_harness.events import emit_event, state_directory\n"


def call(name, arguments, ident="test"):
    return {"type": "function_call", "call_id": ident, "name": name,
            "arguments": json.dumps(arguments)}


class ProgramTestingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.session = Session(ScriptedClient([]), build_file_tools(self.root), workspace=self.root)
        self.addAsyncCleanup(self.session.close)

    def script(self, source):
        path = self.root / "source.py"
        path.write_text(source, encoding="utf-8")
        return path

    async def dispatch(self, name="test_program_generator", **arguments):
        result = await self.session.router.dispatch_async(call(name, arguments))
        return json.loads(result["output"])

    async def until(self, predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(.01)

    def assert_dead(self, pid):
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_sdk_outside_runtime_has_actionable_error(self):
        with patch.dict(os.environ, {}, clear=True):
            for operation in (lambda: emit_event("ready", {}), state_directory):
                with self.assertRaisesRegex(RuntimeError, "test_program_generator"):
                    operation()

    async def test_ordinary_commands_import_sdk_without_inheriting_event_channel(self):
        self.script(SDK + "import os\n"
            "assert 'HARNESS_GENERATOR_SOCKET' not in os.environ\n"
            "assert 'HARNESS_GENERATOR_STATE_DIR' not in os.environ\n"
            "try:\n    emit_event('ready', {})\n"
            "except RuntimeError as error:\n    print(error)\n"
            "else:\n    raise AssertionError('ordinary command emitted')\n")
        # Even a parent with stale IPC configuration must not pass it to commands.
        with patch.dict(os.environ, {"HARNESS_GENERATOR_SOCKET": "/stale/socket",
                                     "HARNESS_GENERATOR_STATE_DIR": "/stale/state"}):
            for login in (False, True):
                result = await self.session.process_manager.exec_command(
                    '"$HARNESS_PYTHON" source.py', login=login)
                self.assertEqual(result["exit_code"], 0, result)
                self.assertIn("No program-generator runtime connected", result["output"])
        self.assertTrue(self.session.input_queue.empty())
        self.assertEqual(self.session.generator_manager.list(True), [])

    async def test_same_script_test_then_registration_routes_only_real_run_to_queue(self):
        self.script(SDK + "import json, os, sys\n"
            "print(json.dumps({'cwd': os.getcwd(), 'python': sys.executable}))\n"
            "print(json.dumps(emit_event('ready', {'value': 42})))\n")
        result = await self.dispatch(script_path="source.py")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["scope"], "test")
        self.assertEqual(len(result["events"]), 1)
        environment, ack = map(json.loads, result["output"].splitlines())
        self.assertEqual(environment["cwd"], str(self.root))
        self.assertEqual(ack["scope"], "test")
        self.assertTrue(ack["event_id"].startswith("test_"))
        self.assertEqual(result["events"][0]["event_id"], ack["event_id"])
        self.assertTrue(self.session.input_queue.empty())
        self.assertEqual(self.session.generator_manager.list(True), [])
        record = await self.dispatch("register_program_generator", script_path="source.py", mode="once")
        manager = self.session.generator_manager
        await self.until(lambda: record["generator_id"] not in manager._tasks)
        self.assertEqual(manager.list(True)[0]["status"], "completed")
        self.assertEqual(len(self.session.input_queue.snapshot()), 1)
        log = Path(record["configuration"]["log_path"]).read_text().splitlines()
        self.assertEqual(json.loads(log[0]), environment)
        self.assertNotIn("scope", json.loads(log[1]))

    async def test_state_is_fresh_discarded_and_not_registered(self):
        self.script(SDK + "p = state_directory() / 'count'\n"
            "count = int(p.read_text()) + 1 if p.exists() else 1\n"
            "p.write_text(str(count))\nprint(state_directory())\nprint(count)\n")
        for _ in range(2):
            result = await self.dispatch(script_path="source.py")
            directory, count = result["output"].splitlines()
            self.assertEqual(count, "1")
            self.assertFalse(Path(directory).exists())
            self.assertFalse(result["state_persisted"])
        self.assertEqual(self.session.generator_manager.checkpoint(), [])

    async def test_nonzero_exit_and_service_exit_are_failures(self):
        for source, mode, code in (("raise SystemExit(7)\n", "once", 7),
                                   ("pass\n", "service", 0)):
            self.script(source)
            result = await self.dispatch(script_path="source.py", mode=mode)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["exit_code"], code)
        self.assertTrue(self.session.input_queue.empty())

    async def test_self_test_does_not_touch_existing_formal_state(self):
        self.script(SDK + "p = state_directory() / 'count'\n"
            "count = int(p.read_text()) + 1 if p.exists() else 1\n"
            "p.write_text(str(count))\nprint(count)\n")
        record = await self.dispatch("register_program_generator", script_path="source.py", mode="once")
        manager = self.session.generator_manager
        await self.until(lambda: record["generator_id"] not in manager._tasks)
        state = Path(record["configuration"]["state_dir"]) / "count"
        before = manager.checkpoint()
        self.assertEqual(state.read_text(), "1")
        result = await self.dispatch(script_path="source.py")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output"].strip(), "1")
        self.assertEqual(state.read_text(), "1")
        self.assertEqual(manager.checkpoint(), before)
        self.assertTrue(self.session.input_queue.empty())

    async def test_timeout_cleans_process_and_reports_not_passed(self):
        self.script("import os, time\nprint(os.getpid(), flush=True)\ntime.sleep(60)\n")
        result = await self.dispatch(script_path="source.py", timeout_ms=200, mode="service")
        self.assertEqual(result["status"], "timed_out")
        self.assertIsNotNone(result["exit_code"])
        self.assert_dead(int(result["output"].strip()))
        self.assertTrue(self.session.input_queue.empty())
        self.assertEqual(self.session.generator_manager.list(True), [])

    async def test_cancellation_cleans_owned_process(self):
        self.script("import os, time\nfrom pathlib import Path\n"
                    "Path('pid').write_text(str(os.getpid()))\ntime.sleep(60)\n")
        task = asyncio.create_task(self.dispatch(script_path="source.py"))
        try:
            await self.until(lambda: (self.root / "pid").exists())
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assert_dead(int((self.root / "pid").read_text()))
        self.assertTrue(self.session.input_queue.empty())

    async def test_rate_limit_and_invalid_events_do_not_generate_control_alerts(self):
        self.script(SDK + "\nfor i in range(6):\n    emit_event('tick', {'i': i})\n")
        result = await self.dispatch(script_path="source.py")
        self.assertEqual(result["status"], "failed")
        self.assertIn("ProgramRateLimited", result["error"])
        self.assertEqual(len(result["events"]), 5)
        self.script(SDK + "emit_event('invalid', [])\n")
        result = await self.dispatch(script_path="source.py")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["events"], [])
        self.assertTrue(self.session.input_queue.empty())

    async def test_log_is_bounded_and_arguments_are_passed(self):
        self.script("import sys\nassert sys.argv[1:] == ['--self-test', 'two words']\n"
                    "print('x' * 100000)\nprint('END')\n")
        result = await self.dispatch(script_path="source.py", args=["--self-test", "two words"])
        self.assertEqual(result["status"], "completed")
        self.assertLessEqual(len(result["output"].encode()), 65536)
        self.assertTrue(result["output"].endswith("END\n"))

    async def test_invalid_tool_parameters_rejected_before_launch(self):
        self.script("raise AssertionError('should not run')\n")
        for args in ({"timeout_ms": True}, {"timeout_ms": 99}, {"timeout_ms": 30001},
                     {"args": [1]}, {"mode": "invalid"}):
            result = await self.dispatch(script_path="source.py", **args)
            self.assertIn("error", result)
        self.assertTrue(self.session.input_queue.empty())

    async def test_only_tool_result_wakes_next_step_and_test_contract_is_documented(self):
        self.script(SDK + "emit_event('ready', {})\n")
        traces = []
        self.session.trace = lambda event, data: traces.append((event, data))
        self.session.client = ScriptedClient([
            [call("test_program_generator", {"script_path": "source.py"}, "probe")],
            [call("send_user_message", {"text": "test done", "wake_model": False}, "reply")],
        ])
        await self.session.post_user_message("test")
        await self.session.run_until_idle()
        queued = [data["queued_event"]["kind"] for event, data in traces if event == "event_enqueued"]
        self.assertEqual(queued, ["user_message", "tool_result"])
        self.assertEqual(self.session.state.step_number, 2)
        self.assertEqual(self.session.state.sent_messages, ["test done"])
        self.assertEqual(self.session.generator_manager.checkpoint(), [])
        self.assertIn("Test receipts have scope=test", build_prompt(self.session).base_instructions)
