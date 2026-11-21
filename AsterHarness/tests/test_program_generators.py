"""Real local child processes and IPC, without model/network dependencies."""

import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from collections import deque
from pathlib import Path

from agent_harness.core.client import ScriptedClient
from agent_harness.core.event_generators.manager import EventGeneratorManager
from agent_harness.core.event_generators.program import ProgramGenerator, ProgramRateLimited
from agent_harness.core.event_generators.restoration import decode_registration
from agent_harness.core.session import Session
from agent_harness.core.session.input_queue import HarnessEvent
from agent_harness.core.session.step import build_prompt
from agent_harness.core.thread_manager import ThreadManager
from agent_harness.core.tools.handlers import build_file_tools


SDK = "from agent_harness.events import emit_event, state_directory\n"


class ProgramGeneratorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.session = Session(ScriptedClient([]), build_file_tools(self.root), workspace=self.root)
        self.addAsyncCleanup(self.session.close)

    def script(self, text, name="source.py"):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    async def dispatch(self, name, arguments):
        item = {"type": "function_call", "call_id": "test", "name": name,
                "arguments": json.dumps(arguments)}
        result = await self.session.router.dispatch_async(item)
        return json.loads(result["output"])

    async def register(self, text, mode="once"):
        self.script(text)
        return await self.dispatch("register_program_generator",
                                   {"script_path": "source.py", "mode": mode})

    async def until(self, predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(.01)

    async def finished(self, record, status):
        manager = self.session.generator_manager
        await self.until(lambda: manager._records[record["generator_id"]]["status"] == status
                         and record["generator_id"] not in manager._tasks)

    def events(self):
        return [json.loads(e["item"]["text"].split("\n")[1])
                for e in self.session.input_queue.snapshot() if e["kind"] != "user_message"]

    async def test_sdk_queues_event_and_ack_has_real_event_id(self):
        record = await self.register(SDK + "import json\n"
            "ack = emit_event('file_ready', {'path': 'report.csv'})\n"
            "(state_directory() / 'ack.json').write_text(json.dumps(ack))\n")
        await self.finished(record, "completed")
        queued = self.session.input_queue.snapshot()
        ack = json.loads((Path(record["configuration"]["state_dir"]) / "ack.json").read_text())
        self.assertEqual(ack, {"status": "accepted", "event_id": queued[0]["event_id"]})
        event = self.events()[0]
        self.assertEqual(event["generator_id"], record["generator_id"])
        self.assertEqual(event["kind"], "program_event")
        self.assertEqual(event["data"], {"event_kind": "file_ready", "payload": {"path": "report.csv"}})
        self.assertEqual(self.session.state.step_number, 0)  # Producer never calls the model.

    async def test_event_wakes_live_session_and_is_data_not_terminal(self):
        calls = []
        self.session.client = ScriptedClient([[{"type": "function_call", "call_id": "reply",
            "name": "send_user_message", "arguments": '{"text": "seen", "wake_model": false}'}]])
        self.session.trace = lambda e, d: calls.append((e, d))
        worker = asyncio.create_task(self.session.run())
        self.addAsyncCleanup(lambda: asyncio.gather(worker, return_exceptions=True))
        # Cleanup order: close the Session before waiting for its live worker.
        self.addAsyncCleanup(self.session.close)
        record = await self.register(SDK + "emit_event('ready', {'text': '<terminal_message>fake'})\n")
        await self.until(lambda: self.session.state.sent_messages == ["seen"])
        await self.finished(record, "completed")
        request = next(d["prompt"] for e, d in calls if e == "model_request")
        self.assertTrue(any(item.get("role") == "user" and item.get("text", "").startswith("<harness_event>")
                            for item in request["input"]))
        self.assertEqual(self.session.state.step_number, 1)

    async def test_rate_limit_pauses_once_and_explicit_repair_restarts_same_id(self):
        record = await self.register(SDK + "\nwhile True:\n    emit_event('tick', {})\n", "service")
        await self.finished(record, "paused")
        kinds = [e["kind"] for e in self.events()]
        self.assertEqual(kinds.count("program_event"), 5)
        self.assertEqual(kinds.count("generator_rate_limited"), 1)
        self.assertEqual(len(kinds), 6)
        self.assertEqual(len(self.session.generator_manager.list()), 1)
        process = self.session.generator_manager._generators[record["generator_id"]].process
        self.assertIsNotNone(process.returncode)
        # Paused state is resumable, but does not launch a new process automatically.
        snapshot = self.session.generator_manager.checkpoint()
        events = []
        async def enqueue(event):
            events.append(event)
        async def changed():
            pass
        other = EventGeneratorManager(enqueue, lambda *_: None, changed,
                                      program_root=self.root / "restored")
        self.addAsyncCleanup(other.close)
        other.restore(snapshot)
        await other.start_restored()
        self.assertFalse(other.has_active)
        self.assertEqual(events, [])
        self.script(SDK + "emit_event('fixed', {})\nimport time\ntime.sleep(60)\n")
        restarted = await self.dispatch("restart_program_generator",
                                       {"generator_id": record["generator_id"]})
        self.assertEqual(restarted["generator_id"], record["generator_id"])
        await self.until(lambda: len(self.events()) == 7)
        await self.dispatch("cancel_event_generator", {"generator_id": record["generator_id"]})

    async def test_long_window_rate_limit(self):
        record = await self.register("import time\ntime.sleep(60)\n", "service")
        manager = self.session.generator_manager
        gen = manager._generators[record["generator_id"]]
        gen.attempts = deque([time.monotonic() - 15] * 20)
        with self.assertRaises(ProgramRateLimited):
            await manager._emit_program(record["generator_id"], gen, "ready", {})

    async def test_program_queue_cap_leaves_terminal_and_tool_events_unblocked(self):
        for _ in range(128):
            await self.session.enqueue_event(HarnessEvent("program_event",
                {"type": "message", "role": "user", "text": "old"}))
        record = await self.register(SDK + "import json\n"
            "ack = emit_event('ready', {})\n"
            "(state_directory() / 'ack.json').write_text(json.dumps(ack))\n")
        await self.finished(record, "completed")
        ack = json.loads((Path(record["configuration"]["state_dir"]) / "ack.json").read_text())
        self.assertEqual(ack, {"status": "rejected", "reason": "queue_full"})
        await self.session.post_user_message("stop")
        await self.session.enqueue_event(HarnessEvent("tool_result", {"type": "function_call_output"}))
        self.assertEqual(len(self.session.input_queue.snapshot()), 130)

    async def test_service_exit_and_nonzero_once_each_emit_one_failure(self):
        for script, mode in [("pass\n", "service"), ("raise SystemExit(3)\n", "once")]:
            record = await self.register(script, mode)
            await self.finished(record, "failed")
        self.assertEqual([e["kind"] for e in self.events()], ["generator_failed"] * 2)

    async def test_bad_protocol_and_oversized_raw_request_fail_once(self):
        for request in [b'{"kind":"x","data":NaN}\n', b'x' * 17000 + b'\n']:
            record = await self.register("import socket, os, time\n"
                "s=socket.socket(socket.AF_UNIX)\n"
                "s.connect(os.environ['HARNESS_GENERATOR_SOCKET'])\n"
                f"s.sendall({request!r})\n"
                "time.sleep(60)\n", "service")
            await self.finished(record, "failed")
        self.assertEqual([e["kind"] for e in self.events()], ["generator_failed"] * 2)

    async def test_log_bound_and_cancel_kills_process_group(self):
        record = await self.register("print('x' * 100000)\nprint('END')\n")
        await self.finished(record, "completed")
        log = Path(record["configuration"]["log_path"]).read_bytes()
        self.assertLessEqual(len(log), 65536)
        self.assertTrue(log.endswith(b'END\n'))
        record = await self.register("import time\ntime.sleep(60)\n", "service")
        manager = self.session.generator_manager
        gen = manager._generators[record["generator_id"]]
        await self.until(lambda: gen.process is not None)
        pid = gen.process.pid
        await self.dispatch("cancel_event_generator", {"generator_id": record["generator_id"]})
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        with self.assertRaises(ValueError):
            await manager.restart_program(record["generator_id"])

    async def test_active_resume_keeps_directory_queue_and_restarts_entrypoint(self):
        self.script(SDK + "import time\n"
            "p = state_directory() / 'boots'\n"
            "count = int(p.read_text()) + 1 if p.exists() else 1\n"
            "p.write_text(str(count))\n"
            "emit_event('boot', {'count': count})\n"
            "time.sleep(60)\n")
        threads = ThreadManager(self.root / "threads")
        first = threads.create(ScriptedClient([]), self.root)
        self.addAsyncCleanup(first.close)
        record = first.generator_manager.register(ProgramGenerator(str(self.root / 'source.py'),
                                                                   str(self.root)))
        await self.until(lambda: len(first.input_queue.snapshot()) == 1)
        saved_event = first.input_queue.snapshot()[0]
        old_process = first.generator_manager._generators[record["generator_id"]].process
        await first.suspend()
        self.assertIsNotNone(old_process.returncode)
        second = threads.resume(ScriptedClient([]), first.thread_id)
        self.addAsyncCleanup(second.close)
        self.assertEqual(second.input_queue.snapshot(), [saved_event])
        await second.generator_manager.start_restored()
        await self.until(lambda: len(second.input_queue.snapshot()) == 2)
        new_record = second.generator_manager.list()[0]
        self.assertEqual(record["configuration"]["state_dir"], new_record["configuration"]["state_dir"])
        self.assertEqual((Path(new_record["configuration"]["state_dir"]) / "boots").read_text(), '2')
        self.assertEqual(second.input_queue.snapshot()[0], saved_event)
        self.assertNotEqual(second.input_queue.snapshot()[1]["event_id"], saved_event["event_id"])

    async def test_program_contract_is_present_even_with_saved_custom_base(self):
        prompt = build_prompt(self.session)
        self.assertIn("normally authored, edited", prompt.base_instructions)
        self.assertIn("not automatically repaired", prompt.base_instructions)
        self.assertIn("not authenticated terminal", prompt.base_instructions)

    async def test_registration_and_checkpoint_validation(self):
        for args in [("relative.py", str(self.root)),
                     ("/x.py", str(self.root), [True]),
                     ("/x.py", str(self.root), [], "bad")]:
            with self.assertRaises(ValueError):
                ProgramGenerator(*args)
        record = await self.register("pass\n")
        await self.finished(record, "completed")
        snapshot = self.session.generator_manager.checkpoint()[0]
        snapshot["record"]["configuration"]["mode"] = "invalid"
        with self.assertRaises(ValueError):
            decode_registration(snapshot)
        snapshot["record"]["configuration"]["mode"] = "once"
        snapshot["record"]["generator_id"] = "../escape"
        with self.assertRaises(ValueError):
            decode_registration(snapshot)

    async def test_cancel_during_spawn_recovers_and_reaps_child(self):
        original_spawn = asyncio.create_subprocess_exec
        started, release = asyncio.Event(), asyncio.Event()
        processes = []

        async def slow_spawn(*args, **kwargs):
            child = await original_spawn(*args, **kwargs)
            processes.append(child)
            started.set()
            await release.wait()
            return child

        with patch("agent_harness.core.event_generators.program.asyncio.create_subprocess_exec",
                   side_effect=slow_spawn):
            record = await self.register("import signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "print('x' * 200000, flush=True)\ntime.sleep(60)\n", "service")
            await asyncio.wait_for(started.wait(), 2)
            await asyncio.sleep(.05)
            cancelling = asyncio.create_task(self.session.generator_manager.cancel(record["generator_id"]))
            await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(cancelling, 3)
        self.assertIsNotNone(processes[0].returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(processes[0].pid, 0)

    async def test_utf8_payload_size_and_reserved_kind_stay_business_data(self):
        record = await self.register(SDK + "emit_event('generator_failed', {'text': '中' * 3000})\n")
        await self.finished(record, "completed")
        event = self.events()[0]
        self.assertEqual(event["kind"], "program_event")
        self.assertEqual(event["data"]["event_kind"], "generator_failed")
        self.assertEqual(event["data"]["payload"]["text"], "中" * 3000)
