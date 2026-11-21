import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path

from agent_harness.core.client import ScriptedClient
from agent_harness.core.client_common import ModelResponse
from agent_harness.core.event_generators import EventGeneratorManager
from agent_harness.core.event_generators.timer import TimerGenerator
from agent_harness.core.session import Session
from agent_harness.core.tools.handlers import build_file_tools


def call(name, arguments, ident):
    return {"type": "function_call", "name": name, "arguments": json.dumps(arguments), "call_id": ident}


def send(text, ident):
    return call("send_user_message", {"text": text, "wake_model": False}, ident)


class SilentGenerator:
    kind = "silent_test"

    def describe(self):
        return {}

    async def run(self, emit):
        await asyncio.sleep(.01)


class FailedGenerator(SilentGenerator):
    async def run(self, emit):
        raise ValueError("source failed")


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = asyncio.Queue()
        self.changed = asyncio.Event()
        self.trace = []
        async def changed():
            self.changed.set()
        self.manager = EventGeneratorManager(self.events.put,
            lambda e, d: self.trace.append((e, d)), changed)
        self.addAsyncCleanup(self.manager.close)

    async def test_named_inventory_reprojects_after_compaction(self):
        from agent_harness.core.context.world_state.generators import GeneratorsState
        state = GeneratorsState(self.manager)
        entry = self.manager.register(TimerGenerator(60000, "private task"), name="news", notes="poll")
        initial = state.render_diff(None, [])
        self.assertEqual(initial.snapshot["registrations"][0]["name"], "news")
        self.assertNotIn("private task", str(initial.items))
        self.assertIsNone(state.render_diff(initial.snapshot, initial.items))
        self.assertEqual(state.render_diff(initial.snapshot, []).items, initial.items)
        self.assertNotIn(state.marker, state.project_items(initial.items)[0])
        await self.manager.cancel(entry["generator_id"])
        self.assertEqual(state.render_diff(initial.snapshot, initial.items).snapshot["registrations"], [])

    async def test_one_shot_and_immutable_registry_snapshot(self):
        record = self.manager.register(TimerGenerator(1, "wake"))
        record["configuration"]["message"] = "changed"
        event = await asyncio.wait_for(self.events.get(), 1)
        self.assertEqual(event["kind"], "timer_fired")
        self.assertEqual(event["data"]["message"], "wake")
        self.assertEqual(event["generator_id"], record["generator_id"])
        await asyncio.wait_for(self.changed.wait(), 1)
        self.assertFalse(self.manager.has_active)
        self.assertEqual(self.manager.list(), [])
        finished = self.manager.list(True)[0]
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["events_emitted"], 1)

    async def test_periodic_finite_and_infinite_cancel(self):
        self.manager.register(TimerGenerator(1, "tick", interval_ms=1000, repeat_count=2))
        first = await asyncio.wait_for(self.events.get(), 1)
        second = await asyncio.wait_for(self.events.get(), 2)
        self.assertEqual([first["sequence"], second["sequence"]], [1, 2])
        self.assertEqual(second["data"]["occurrence"], 2)
        record = self.manager.register(TimerGenerator(1, "forever", 1000, 0))
        await asyncio.wait_for(self.events.get(), 1)
        result = await self.manager.cancel(record["generator_id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(self.manager.has_active)
        self.assertTrue(self.events.empty())
        # Cancellation is idempotent, and does not retract already emitted events.
        self.assertEqual(await self.manager.cancel(record["generator_id"]), result)

    async def test_cancel_before_task_starts_and_close_fence(self):
        record = self.manager.register(TimerGenerator(1, "never"))
        await self.manager.cancel(record["generator_id"])
        self.assertFalse(self.manager.has_active)
        self.assertFalse(self.manager._tasks)
        self.assertTrue(self.events.empty())
        self.manager.register(TimerGenerator(1, "also never"))
        await self.manager.close()
        await asyncio.sleep(0)
        self.assertTrue(self.events.empty())
        with self.assertRaisesRegex(ValueError, "closed"):
            self.manager.register(TimerGenerator(1, "no"))

    async def test_failure_emits_one_error_event_and_finishes(self):
        self.manager.register(FailedGenerator())
        event = await asyncio.wait_for(self.events.get(), 1)
        self.assertEqual(event["kind"], "generator_failed")
        await asyncio.wait_for(self.changed.wait(), 1)
        self.assertEqual(self.manager.list(True)[0]["status"], "failed")
        self.assertTrue(self.events.empty())

    async def test_capacity_retention_and_validation(self):
        self.manager.MAX_ACTIVE = 1
        self.manager.MAX_FINISHED = 1
        record = self.manager.register(TimerGenerator(10000, "a"))
        with self.assertRaisesRegex(ValueError, "limit"):
            self.manager.register(TimerGenerator(10000, "b"))
        await self.manager.cancel(record["generator_id"])
        record = self.manager.register(TimerGenerator(10000, "c"))
        await self.manager.cancel(record["generator_id"])
        self.assertEqual(len(self.manager.list(True)), 1)
        with self.assertRaises(ValueError):
            await self.manager.cancel("missing")
        with self.assertRaises(ValueError):
            self.manager.list("yes")
        for args in [(0, "m"), (True, "m"), (1, ""), (1, "m", None, 0),
                     (1, "m", 10, 2), (1, "m", 1000, True)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                TimerGenerator(*args)


class GeneratorSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.events = []

    def session(self, responses):
        session = Session(ScriptedClient(responses), build_file_tools(self.root), workspace=self.root,
            trace=lambda e, d: self.events.append((e, d)))
        self.addAsyncCleanup(session.close)
        return session

    async def dispatch(self, session, name, arguments):
        result = await session.router.dispatch_async(call(name, arguments, "direct_test"))
        return json.loads(result["output"])

    async def test_timer_wakes_from_idle_and_enters_history_as_notification(self):
        session = self.session([
            [call("create_timer", {"delay_ms": 100, "message": "wake me"}, "timer")],
            [send("scheduled", "send1")], [send("awakened", "send2")]])
        await session.post_user_message("schedule")
        await asyncio.wait_for(session.run_until_settled(), 2)
        self.assertEqual(session.state.sent_messages, ["scheduled", "awakened"])
        self.assertEqual(session.state.step_number, 3)
        requests = [d["prompt"] for e, d in self.events if e == "model_request"]
        notice = next(item for item in reversed(requests[2]["input"])
                      if item.get("text", "").startswith("<harness_event>"))
        self.assertEqual(notice["role"], "user")
        self.assertTrue(notice["text"].startswith("<harness_event>"))
        self.assertIn('"kind": "timer_fired"', notice["text"])
        self.assertTrue(session.input_queue.empty())
        self.assertEqual(session.state.status, "waiting")

    async def test_exec_completion_creates_one_notification_without_consuming_output(self):
        session = self.session([
            [call("exec_command", {"cmd": "printf START; sleep .7; printf DONE", "login": False,
                    "yield_time_ms": 250, "notify_on_completion": True}, "exec")],
            [send("waiting", "send1")], [send("completed", "send2")]])
        await session.post_user_message("run")
        await asyncio.wait_for(session.run_until_settled(), 3)
        queued = [d["queued_event"] for e, d in self.events if e == "event_enqueued"]
        self.assertEqual([e["kind"] for e in queued], ["user_message", "tool_result", "command_completed"])
        initial = json.loads(queued[1]["item"]["output"])
        self.assertIn("generator_id", initial)
        self.assertEqual(initial["output"], "START")
        notice = json.loads(queued[2]["item"]["text"].split("\n")[1])
        self.assertEqual(notice["generator_id"], initial["generator_id"])
        self.assertEqual(notice["data"]["exit_code"], 0)
        self.assertNotIn("output", notice["data"])
        final = await session.process_manager.write_stdin(initial["session_id"])
        self.assertEqual(final["output"], "DONE")
        self.assertEqual(final["exit_code"], 0)
        self.assertEqual(sum(i.get("call_id") == "exec" and i["type"] == "function_call_output"
                             for i in session.state.history.snapshot()), 1)

    async def test_fast_command_or_notification_disabled_creates_no_generator(self):
        session = self.session([])
        result = await self.dispatch(session, "exec_command", {"cmd": "true", "notify_on_completion": True})
        self.assertEqual(result["exit_code"], 0)
        self.assertNotIn("generator_id", result)
        result = await self.dispatch(session, "exec_command", {"cmd": "sleep 30", "yield_time_ms": 250})
        self.assertIn("session_id", result)
        self.assertFalse(session.generator_manager.has_active)
        self.assertEqual(session.generator_manager.list(True), [])

    async def test_manual_poll_and_notification_do_not_steal_each_others_output(self):
        session = self.session([])
        completed = asyncio.Event()
        old_trace = session.trace
        def trace(e, d):
            old_trace(e, d)
            if e == "generator_completed":
                completed.set()
        session.trace = trace
        initial = await self.dispatch(session, "exec_command", {"cmd": "sleep .5; printf FINAL",
            "yield_time_ms": 250, "notify_on_completion": True, "login": False})
        final = await self.dispatch(session, "write_stdin", {"session_id": initial["session_id"]})
        await asyncio.wait_for(completed.wait(), 2)
        self.assertEqual(final["output"], "FINAL")
        self.assertEqual(final["exit_code"], 0)
        pending = session.input_queue.snapshot()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["kind"], "command_completed")
        self.assertNotIn(initial["session_id"], session.process_manager.processes)

    async def test_cancel_command_notification_does_not_kill_command(self):
        session = self.session([])
        initial = await self.dispatch(session, "exec_command", {"cmd": "sleep .5; printf FINAL",
            "yield_time_ms": 250, "notify_on_completion": True, "login": False})
        await self.dispatch(session, "cancel_event_generator", {"generator_id": initial["generator_id"]})
        final = await self.dispatch(session, "write_stdin", {"session_id": initial["session_id"]})
        self.assertEqual(final["exit_code"], 0)
        self.assertEqual(final["output"], "FINAL")
        self.assertTrue(session.input_queue.empty())

    async def test_close_cancels_sources_before_terminating_processes(self):
        session = self.session([])
        await self.dispatch(session, "create_timer", {"delay_ms": 10000, "message": "late"})
        initial = await self.dispatch(session, "exec_command", {"cmd": "sleep 30",
            "yield_time_ms": 250, "notify_on_completion": True})
        entry = session.process_manager.processes[initial["session_id"]]
        await session.close()
        self.assertIsNotNone(entry.process.returncode)
        self.assertFalse(session.generator_manager.has_active)
        self.assertTrue(session.input_queue.empty())
        self.assertFalse(session.generator_manager._tasks)

    async def test_capacity_failure_before_spawn_and_invalid_notification_flag(self):
        session = self.session([])
        session.generator_manager.MAX_ACTIVE = 1
        await self.dispatch(session, "create_timer", {"delay_ms": 10000, "message": "occupy"})
        result = await self.dispatch(session, "exec_command", {"cmd": "sleep 30", "notify_on_completion": True})
        self.assertIn("error", result)
        self.assertFalse(session.process_manager.processes)
        result = await self.dispatch(session, "exec_command", {"cmd": "true", "notify_on_completion": "yes"})
        self.assertIn("error", result)

    async def test_capacity_changes_during_exec_wait_preserve_command_handle(self):
        session = self.session([])
        session.generator_manager.MAX_ACTIVE = 1
        collecting = asyncio.Event()
        original = session.process_manager._collect
        async def collect(*args):
            collecting.set()
            return await original(*args)
        session.process_manager._collect = collect
        command = asyncio.create_task(self.dispatch(session, "exec_command", {
            "cmd": "sleep 30", "yield_time_ms": 250, "notify_on_completion": True}))
        await asyncio.wait_for(collecting.wait(), 1)
        session.generator_manager.register(TimerGenerator(10000, "occupy"))
        result = await asyncio.wait_for(command, 2)
        self.assertIn("notification_error", result)
        self.assertIn(result["session_id"], session.process_manager.processes)
        self.assertNotIn("error", result)

    async def test_shutdown_during_exec_wait_does_not_register_late_notification(self):
        session = self.session([
            [call("exec_command", {"cmd": "sleep 30", "yield_time_ms": 250,
                                    "notify_on_completion": True}, "exec")],
            [send("closing", "send")]])
        collecting = asyncio.Event()
        original = session.process_manager._collect
        async def collect(*args):
            collecting.set()
            return await original(*args)
        session.process_manager._collect = collect
        await session.post_user_message("A")
        worker = asyncio.create_task(session.run())
        await asyncio.wait_for(collecting.wait(), 1)
        await session.close()
        await asyncio.wait_for(worker, 2)
        outputs = [json.loads(d["result"]["output"]) for e, d in self.events if e == "tool_result"]
        self.assertIn("notification_error", outputs[0])
        self.assertIn("session_id", outputs[0])
        self.assertFalse(session.generator_manager.has_active)
        self.assertFalse(session.process_manager.processes)
        self.assertEqual(session.state.status, "stopped")
        self.assertFalse(any(e == "generator_created" for e, _ in self.events))

    async def test_generator_registries_are_session_local(self):
        first, second = self.session([]), self.session([])
        record = first.generator_manager.register(TimerGenerator(10000, "first only"))
        self.assertEqual(second.generator_manager.list(True), [])
        with self.assertRaisesRegex(ValueError, "Unknown"):
            await second.generator_manager.cancel(record["generator_id"])
        await second.close()
        self.assertTrue(first.generator_manager.has_active)

    async def test_generator_fires_during_model_request_without_interrupting_step(self):
        session = self.session([])
        entered, release = threading.Event(), threading.Event()
        emitted = asyncio.Event()
        requests = []
        old_trace = session.trace
        def trace(e, d):
            old_trace(e, d)
            if e == "generator_fired":
                emitted.set()
        session.trace = trace
        def respond(prompt):
            requests.append(prompt)
            if len(requests) == 1:
                return ModelResponse([call("create_timer", {"delay_ms": 100, "message": "during model"}, "timer")])
            if len(requests) == 2:
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("test barrier timed out")
                return ModelResponse([send("scheduled", "send1")])
            return ModelResponse([send("awakened", "send2")])
        session.client.respond = respond
        await session.post_user_message("A")
        worker = asyncio.create_task(session.run_until_settled())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            await asyncio.wait_for(emitted.wait(), 1)
            await session.post_user_message("B")
            self.assertEqual(session.state.step_number, 2)
        finally:
            release.set()
        await asyncio.wait_for(worker, 2)
        batch = [d for e, d in self.events if e == "step_started"][2]["events"]
        self.assertEqual([event["kind"] for event in batch], ["timer_fired", "user_message"])

    async def test_batch_waiter_unblocks_when_last_generator_is_cancelled_or_silent(self):
        session = self.session([])
        session.generator_manager.register(SilentGenerator())
        await asyncio.wait_for(session.run_until_settled(), 1)
        record = session.generator_manager.register(TimerGenerator(10000, "later"))
        worker = asyncio.create_task(session.run_until_settled())
        await asyncio.sleep(0)
        self.assertFalse(worker.done())
        await session.generator_manager.cancel(record["generator_id"])
        await asyncio.wait_for(worker, 1)
        self.assertEqual(session.state.step_number, 0)

    async def test_run_until_idle_does_not_cancel_future_generators(self):
        session = self.session([])
        record = session.generator_manager.register(TimerGenerator(10000, "later"))
        await session.run_until_idle()
        self.assertTrue(session.generator_manager.has_active)
        self.assertEqual(session.generator_manager.list()[0]["generator_id"], record["generator_id"])
