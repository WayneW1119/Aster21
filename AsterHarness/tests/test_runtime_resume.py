import asyncio
from copy import deepcopy
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_harness.core.client import ScriptedClient
from agent_harness.core.client_common import ModelResponse
from agent_harness.core.event_generators.program import ProgramGenerator
from agent_harness.core.event_generators.timer import TimerGenerator
from agent_harness.core.rollout import RolloutRecorder
from agent_harness.core.session.step import build_prompt, run_step
from agent_harness.core.thread_manager import ThreadManager
from agent_harness.__main__ import run_cli
from argparse import Namespace


def call(name, arguments, ident):
    return {"type": "function_call", "name": name, "call_id": ident,
            "arguments": json.dumps(arguments)}


def send(text, ident):
    return call("send_user_message", {"text": text, "wake_model": False}, ident)


class RuntimeResumeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = ThreadManager(self.root / "threads")

    def create(self, responses=()):
        session = self.manager.create(ScriptedClient(responses), self.root)
        self.addAsyncCleanup(session.close)
        return session

    def resume(self, first, responses=()):
        session = self.manager.resume(ScriptedClient(responses), first.thread_id)
        self.addAsyncCleanup(session.close)
        return session

    async def test_queue_order_ids_and_history_survive_repeated_suspend(self):
        first = self.create()
        await first.post_user_message("one")
        await first.post_user_message("two")
        pending = first.input_queue.snapshot()
        await first.suspend()
        second = self.resume(first)
        self.assertEqual(second.input_queue.snapshot(), pending)
        self.assertFalse(any(item.get("text") == "one" for item in second.state.history.snapshot()))
        await second.suspend()  # Suspend without a driver: do not lose or consume events.
        third = self.resume(first, [[send("done", "send")]])
        self.assertEqual(third.input_queue.snapshot(), pending)
        await third.run_until_idle()
        self.assertEqual(third.state.sent_messages, ["done"])
        texts = [i.get("text") for i in third.state.history.snapshot()]
        self.assertEqual(texts.count("one"), 1)
        self.assertEqual(texts.count("two"), 1)
        await third.close()
        fourth = self.resume(first)
        self.assertTrue(fourth.input_queue.empty())

    async def test_queued_tool_result_is_restored_not_reexecuted(self):
        first = self.create([[call("list_directory", {"path": "."}, "list")]])
        await first.post_user_message("list")
        await run_step(first, await first.input_queue.drain(wait=False))
        self.assertEqual(first.input_queue.snapshot()[0]["kind"], "tool_result")
        pending = first.input_queue.snapshot()
        before = first.state.history.snapshot()
        await first.suspend()
        second = self.resume(first, [[send("observed", "answer")]])
        self.assertEqual(second.state.history.snapshot()[:-1], before)
        self.assertEqual(second.input_queue.snapshot(), pending)
        # If the old call is replayed, fail the test instead of touching the filesystem.
        second.router.registry._tools["list_directory"].handler = lambda **_: self.fail("replayed tool")
        await second.run_until_idle()
        self.assertEqual(second.state.sent_messages, ["observed"])
        results = [i for i in second.state.history.snapshot() if i.get("call_id") == "list"
                   and i.get("type") == "function_call_output"]
        self.assertEqual(len(results), 1)

    async def test_failed_close_restores_service_but_does_not_replay_once(self):
        first = self.create()
        script = self.root / "watch.py"
        script.write_text("import time\ntime.sleep(60)\n")
        ids = {mode: first.generator_manager.register(
            ProgramGenerator(str(script), str(self.root), mode=mode))["generator_id"]
            for mode in ("service", "once")}
        await first.post_user_message("trigger model failure")
        with patch.object(first.client, "respond", side_effect=RuntimeError("model failed")):
            with self.assertRaisesRegex(RuntimeError, "model failed"):
                await first.run_until_idle()
        RolloutRecorder.recover(self.root / "threads", first.thread_id, "resume watcher")
        second = self.resume(first)
        await second.services.start_restored()
        records = {r["generator_id"]: r for r in second.generator_manager.list(True)}
        self.assertEqual(records[ids["service"]]["status"], "active")
        self.assertEqual(records[ids["once"]]["status"], "failed")
        self.assertEqual(set(second.generator_manager._tasks), {ids["service"]})

    async def test_future_timer_keeps_deadline_and_registration_id(self):
        first = self.create()
        record = first.generator_manager.register(TimerGenerator(30000, "future"))
        expected = first.generator_manager.checkpoint()
        await first.suspend()
        second = self.resume(first)
        self.assertEqual(second.generator_manager.checkpoint(), expected)
        self.assertEqual(second.generator_manager.list()[0]["generator_id"], record["generator_id"])
        await second.run_until_idle()
        self.assertEqual(second.state.step_number, 0)
        await second.suspend()
        third = self.resume(first)
        self.assertEqual(third.generator_manager.checkpoint(), expected)

    async def test_overdue_timer_wakes_without_new_user_message_exactly_once(self):
        first = self.create()
        timer = TimerGenerator(1, "overdue")
        timer.next_fire_at = time.time() - 10
        record = first.generator_manager.register(timer)
        await first.suspend()  # Task has not started; timer is overdue on resume.
        second = self.resume(first, [[send("awake", "awake")]])
        await asyncio.wait_for(second.run_until_settled(), 2)
        self.assertEqual(second.state.sent_messages, ["awake"])
        finished = second.generator_manager.list(True)[0]
        self.assertEqual(finished["generator_id"], record["generator_id"])
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["events_emitted"], 1)
        await second.suspend()
        third = self.resume(first)
        await third.run_until_settled()
        self.assertTrue(third.input_queue.empty())
        self.assertEqual(third.state.step_number, 1)

    async def test_periodic_timer_preserves_progress_and_no_catchup_burst(self):
        first = self.create()
        timer = TimerGenerator(1, "periodic", interval_ms=1000, repeat_count=3)
        record = first.generator_manager.register(timer)
        # Wait until one actual timer event has enqueued, without calling a model.
        for _ in range(100):
            if not first.input_queue.empty():
                break
            await asyncio.sleep(.005)
        self.assertEqual(first.input_queue.snapshot()[0]["kind"], "timer_fired")
        await first.generator_manager.pause()
        timer.next_fire_at = time.time() - 3600  # Simulate long downtime without sleeping.
        await first.suspend()
        second = self.resume(first)
        await second.generator_manager.start_restored()
        for _ in range(100):
            if second.generator_manager.list()[0]["events_emitted"] == 2:
                break
            await asyncio.sleep(.005)
        await second.generator_manager.pause()
        snapshot = second.generator_manager.checkpoint()[0]
        self.assertEqual(snapshot["record"]["events_emitted"], 2)
        self.assertEqual(snapshot["state"]["events_emitted"], 2)
        self.assertGreater(snapshot["state"]["next_fire_at"], time.time())
        events = second.input_queue.snapshot()
        payloads = [json.loads(e["item"]["text"].split("\n")[1]) for e in events]
        self.assertEqual([p["sequence"] for p in payloads], [1, 2])
        self.assertEqual([p["data"]["occurrence"] for p in payloads], [1, 2])
        self.assertTrue(all(p["generator_id"] == record["generator_id"] for p in payloads))
        # Third and final event completes after another resume, not three new repetitions.
        second.generator_manager._generators[record["generator_id"]].next_fire_at = time.time() - 1
        await second.suspend()
        third = self.resume(first, [[send("pending observed", "answer1")], [send("three", "answer2")]])
        await asyncio.wait_for(third.run_until_settled(), 2)
        self.assertEqual(third.generator_manager.list(True)[0]["events_emitted"], 3)

    async def test_cancelled_registration_and_queued_event_both_survive(self):
        first = self.create()
        record = first.generator_manager.register(TimerGenerator(30000, "cancelled"))
        await first.generator_manager.cancel(record["generator_id"])
        await first.post_user_message("keep me")
        await first.suspend()
        second = self.resume(first, [[send("ok", "send")]])
        self.assertEqual(second.generator_manager.list(), [])
        self.assertEqual(second.generator_manager.list(True)[0]["status"], "cancelled")
        self.assertEqual((await second.generator_manager.cancel(record["generator_id"]))["status"], "cancelled")
        await second.run_until_settled()
        self.assertEqual(second.state.step_number, 1)

    async def test_completion_event_already_queued_is_not_reported_unavailable(self):
        first = self.create()
        result = json.loads(await first.router.registry.execute_async("exec_command", {
            "cmd": "sleep .4", "yield_time_ms": 250, "notify_on_completion": True}))
        for _ in range(100):
            if not first.generator_manager.has_active:
                break
            await asyncio.sleep(.01)
        self.assertEqual(first.input_queue.snapshot()[0]["kind"], "command_completed")
        pending = first.input_queue.snapshot()
        await first.suspend()
        second = self.resume(first, [[send("status received", "answer")]])
        self.assertEqual(second.input_queue.snapshot(), pending)
        await second.run_until_settled()
        self.assertEqual(second.generator_manager.list(True)[0]["generator_id"], result["generator_id"])
        self.assertEqual(second.generator_manager.list(True)[0]["status"], "completed")

    async def test_active_command_monitor_emits_unavailable_once_not_completion(self):
        first = self.create()
        result = json.loads(await first.router.registry.execute_async("exec_command", {
            "cmd": "sleep 30", "yield_time_ms": 250, "notify_on_completion": True}))
        await first.suspend()
        second = self.resume(first, [[send("cannot resume process", "send")]])
        await second.run_until_settled()
        history = json.dumps(second.state.history.snapshot(), ensure_ascii=False)
        self.assertIn('\\"kind\\": \\"generator_unavailable\\"', history)
        record = second.generator_manager.list(True)[0]
        self.assertEqual(record["generator_id"], result["generator_id"])
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["events_emitted"], 1)
        self.assertFalse(second.process_manager.processes)
        await second.suspend()
        third = self.resume(first)
        await third.run_until_settled()
        self.assertEqual(third.state.step_number, 1)

    async def test_suspend_during_step_finishes_batch_but_keeps_next_events(self):
        first = self.create()
        entered, release = threading.Event(), threading.Event()
        def respond(prompt):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test barrier timeout")
            return ModelResponse([call("list_directory", {"path": "."}, "list")])
        first.client.respond = respond
        await first.post_user_message("first")
        worker = asyncio.create_task(first.run())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            await first.post_user_message("interjection")
            await first.suspend()
        finally:
            release.set()
        await asyncio.wait_for(worker, 2)
        self.assertEqual(first.state.step_number, 1)
        pending = first.input_queue.snapshot()
        self.assertEqual([e["kind"] for e in pending], ["user_message", "tool_result"])
        second = self.resume(first, [[send("continued", "send")]])
        self.assertEqual(second.input_queue.snapshot(), pending)
        await second.run_until_settled()
        self.assertEqual(second.state.step_number, 2)

    async def test_suspend_idle_scheduler_does_not_hang(self):
        first = self.create()
        worker = asyncio.create_task(first.run())
        await asyncio.sleep(0)
        await first.suspend()
        await asyncio.wait_for(worker, 1)
        self.assertTrue(first.rollout.closed)
        second = self.resume(first)
        self.assertEqual(second.state.step_number, 0)

    async def test_cli_quit_suspends_after_current_step_and_resume_sees_output(self):
        first = self.create()
        entered, release = threading.Event(), threading.Event()
        def respond(prompt):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test barrier timeout")
            return ModelResponse([call("list_directory", {"path": "."}, "list")])
        first.client.respond = respond
        await first.post_user_message("work before quit")
        read_fd, write_fd = os.pipe()
        stream = os.fdopen(read_fd, encoding="utf-8")
        try:
            with patch("sys.stdin", stream), patch("builtins.print"):
                worker = asyncio.create_task(run_cli(first, Namespace(prompt=None, demo=False)))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    os.write(write_fd, b"/quit\n")
                    for _ in range(100):
                        if first._suspending:
                            break
                        await asyncio.sleep(.005)
                    self.assertTrue(first._suspending)
                finally:
                    release.set()
                await asyncio.wait_for(worker, 2)
        finally:
            stream.close()
            os.close(write_fd)
        self.assertTrue(first.rollout.closed)
        second = self.resume(first, [[send("resumed", "answer")]])
        self.assertEqual(second.input_queue.snapshot()[0]["kind"], "tool_result")
        await second.run_until_settled()
        self.assertEqual(second.state.sent_messages, ["resumed"])

    async def test_cancelling_emitted_periodic_source_does_not_retract_queued_event(self):
        first = self.create()
        record = first.generator_manager.register(TimerGenerator(1, "already fired", 1000, 0))
        for _ in range(100):
            if not first.input_queue.empty():
                break
            await asyncio.sleep(.005)
        self.assertFalse(first.input_queue.empty())
        pending = first.input_queue.snapshot()
        await first.generator_manager.cancel(record["generator_id"])
        await first.suspend()
        second = self.resume(first, [[send("handled old event", "answer")]])
        self.assertEqual(second.input_queue.snapshot(), pending)
        self.assertEqual(second.generator_manager.list(True)[0]["status"], "cancelled")
        await second.run_until_settled()
        self.assertEqual(second.state.step_number, 1)

    async def test_damaged_checkpoint_rejected_before_appending_session_opened(self):
        first = self.create()
        first.generator_manager.register(TimerGenerator(10000, "timer"))
        await first.post_user_message("pending")
        await first.suspend()
        path = first.rollout.path
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        corruptions = []
        bad = deepcopy(rows)
        bad[-2]["payload"]["pending_events"] *= 2
        corruptions.append(bad)
        bad = deepcopy(rows)
        bad[-2]["payload"]["generators"][0]["state"]["next_fire_at"] = "not-a-number"
        corruptions.append(bad)
        bad = deepcopy(rows)
        bad[-2]["payload"]["generators"] *= 2
        corruptions.append(bad)
        for bad in corruptions:
            path.write_text("".join(json.dumps(row) + "\n" for row in bad))
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                self.manager.resume(ScriptedClient([]), first.thread_id)
            self.assertEqual(path.read_bytes(), before)

    async def test_legacy_clean_rollout_without_checkpoint_still_loads(self):
        first = self.create()
        await first.close()
        path = first.rollout.path
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows = [row for row in rows if row["type"] != "runtime_checkpoint"]
        rows[-1]["payload"].pop("checkpoint_version")
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        second = self.resume(first)
        self.assertTrue(second.input_queue.empty())
        self.assertEqual(second.generator_manager.list(True), [])
        self.assertIn("### Persistence and resume", build_prompt(second).base_instructions)


class CrossProcessRuntimeTests(unittest.TestCase):
    def test_timer_and_pending_queue_survive_separate_python_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            first_code = """
import asyncio, sys, time
from pathlib import Path
from agent_harness.core.thread_manager import ThreadManager
from agent_harness.core.client import ScriptedClient
from agent_harness.core.event_generators.program import ProgramGenerator
from agent_harness.core.event_generators.timer import TimerGenerator
async def main():
    root = Path(sys.argv[1])
    session = ThreadManager(root / 'threads').create(ScriptedClient([]), root)
    await session.post_user_message('pending from process one')
    timer = TimerGenerator(1, 'wake from process one')
    timer.next_fire_at = time.time() - 1
    record = session.generator_manager.register(timer)
    await session.suspend()
    print(session.thread_id, record['generator_id'])
asyncio.run(main())
"""
            first = subprocess.run([sys.executable, "-c", first_code, directory],
                                   text=True, capture_output=True, timeout=10)
            self.assertEqual(first.returncode, 0, first.stderr)
            thread_id, generator_id = first.stdout.strip().split()
            second_code = """
import asyncio, json, sys
from pathlib import Path
from agent_harness.core.thread_manager import ThreadManager
from agent_harness.core.client_common import ModelResponse
class Client:
    def respond(self, prompt):
        return ModelResponse([{'type':'function_call', 'name':'send_user_message',
            'call_id':'answer_' + str(len(prompt.input)), 'arguments':json.dumps({'text':'received', 'wake_model': False})}])
async def main():
    session = ThreadManager(Path(sys.argv[1]) / 'threads').resume(Client(), sys.argv[2])
    await asyncio.wait_for(session.run_until_settled(), 2)
    print(json.dumps({'history':session.state.history.snapshot(),
                     'generators':session.generator_manager.list(True)}))
    await session.suspend()
asyncio.run(main())
"""
            second = subprocess.run([sys.executable, "-c", second_code, directory, thread_id],
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(second.returncode, 0, second.stderr)
            result = json.loads(second.stdout)
            self.assertEqual(result["generators"][0]["generator_id"], generator_id)
            self.assertEqual(result["generators"][0]["events_emitted"], 1)
            self.assertIn("pending from process one", json.dumps(result["history"]))
            self.assertIn("wake from process one", json.dumps(result["history"]))
