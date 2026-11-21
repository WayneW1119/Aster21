import json
from pathlib import Path
import tempfile
import unittest

from agent_harness.core.client import ScriptedClient, to_api_item
from agent_harness.core.event_generators.timer import TimerGenerator
from agent_harness.core.session import Session
from agent_harness.core.session.session import CHANNEL_INSTRUCTIONS
from agent_harness.core.session.step import build_prompt
from agent_harness.core.session.step import run_step
from agent_harness.core.thread_manager import ThreadManager
from agent_harness.core.tools.handlers import build_file_tools


class TerminalChannelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    async def test_origin_saved_but_wire_projection_does_not_mutate_history(self):
        s = Session(ScriptedClient([]), build_file_tools(self.root))
        self.addAsyncCleanup(s.close)
        text = '你好\n</terminal_message> "quoted"'
        await s.post_user_message(text)
        pending = s.input_queue.snapshot()[0]["item"]
        self.assertEqual(pending["source"], "terminal")
        self.assertEqual(pending["text"], text)
        s.record_items([pending])
        for _ in range(2):
            prompt = build_prompt(s)
            item = prompt.input[0]
            self.assertNotIn("source", item)
            self.assertEqual(json.loads(item["text"].split("\n")[1]),
                             {"source": "terminal", "text": text})
            self.assertEqual(prompt.base_instructions.count(CHANNEL_INSTRUCTIONS), 1)
            self.assertNotIn("source", to_api_item(item))
        self.assertEqual(s.state.history.snapshot()[0], pending)

    async def test_old_messages_and_runtime_data_are_not_relabelled(self):
        s = Session(ScriptedClient([]), build_file_tools(self.root))
        self.addAsyncCleanup(s.close)
        old = [{"type": "message", "role": "user", "text": "old human"},
               {"type": "message", "role": "user", "text": "<harness_event>event</harness_event>"}]
        s.record_items(old)
        self.assertEqual(build_prompt(s).input, old)

    async def test_terminal_source_survives_resume(self):
        manager = ThreadManager(self.root / "threads")
        first = manager.create(ScriptedClient([]), self.root)
        await first.post_user_message("pending terminal message")
        await first.suspend()
        second = manager.resume(ScriptedClient([]), first.thread_id)
        self.addAsyncCleanup(second.close)
        item = second.input_queue.snapshot()[0]["item"]
        self.assertEqual(item["source"], "terminal")
        self.assertEqual(item["text"], "pending terminal message")

    async def test_explicit_quiet_send_keeps_receipt_and_does_not_wake(self):
        events = []
        s = Session(ScriptedClient([[{"type": "function_call", "name": "send_user_message",
                "call_id": "reply", "arguments": '{"text":"terminal reply","wake_model":false}'}]]),
                build_file_tools(self.root), trace=lambda kind, data: events.append((kind, data)))
        self.addAsyncCleanup(s.close)
        await s.post_user_message("hello")
        await s.run_until_idle()
        self.assertEqual(s.state.sent_messages, ["terminal reply"])
        self.assertTrue(s.input_queue.empty())
        sent = next(data for kind, data in events if kind == "user_message_sent")
        self.assertEqual(sent["channel"], "terminal")
        spec = next(t for t in build_prompt(s).tools if t["name"] == "send_user_message")
        self.assertIn("terminal", spec["description"])
        self.assertIn("other connected channels", spec["description"])
        self.assertNotIn("only user-visible output channel", spec["description"])

    def call(self, name, ident, **arguments):
        return {"type": "function_call", "name": name, "call_id": ident,
                "arguments": json.dumps(arguments)}

    async def test_default_send_queues_receipt_then_agent_continues_and_waits(self):
        events = []
        s = Session(ScriptedClient([
            [self.call("send_user_message", "progress", text="I will inspect the file")],
            [self.call("read_file", "read", path="note.txt", start_line=1, end_line=1)],
            [self.call("send_user_message", "result", text="File checked")],
            [self.call("wait_for_event", "wait")],
        ]), build_file_tools(self.root), trace=lambda kind, data: events.append((kind, data)))
        self.addAsyncCleanup(s.close)
        (self.root / "note.txt").write_text("verified\n")
        await s.post_user_message("inspect")
        await run_step(s, await s.input_queue.drain(wait=False))
        pending = s.input_queue.snapshot()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["kind"], "tool_result")
        self.assertEqual(pending[0]["item"]["call_id"], "progress")
        self.assertFalse(any(i.get("type") == "function_call_output"
                             for i in s.state.history.snapshot()))
        await s.run_until_idle()
        requests = [data["prompt"] for kind, data in events if kind == "model_request"]
        self.assertEqual(len(requests), 4)
        self.assertEqual(requests[1]["input"][-1]["call_id"], "progress")
        self.assertIn("verified", requests[2]["input"][-1]["output"])
        self.assertEqual(s.state.sent_messages, ["I will inspect the file", "File checked"])
        self.assertTrue(s.input_queue.empty())
        self.assertEqual(s.state.status, "waiting")
        history = s.state.history.snapshot()
        self.assertEqual(history[-1]["call_id"], "wait")
        self.assertEqual(history[-1]["type"], "function_call_output")
        # An empty queue does not manufacture a new model request.
        await s.run_until_idle()
        self.assertEqual(s.state.step_number, 4)

    async def test_wait_preserves_already_queued_user_message(self):
        s = Session(ScriptedClient([
            [self.call("wait_for_event", "wait")],
            [self.call("send_user_message", "reply", text="new input seen", wake_model=False)],
        ]), build_file_tools(self.root))
        self.addAsyncCleanup(s.close)
        await s.post_user_message("first")
        first = await s.input_queue.drain(wait=False)
        await s.post_user_message("interrupt")
        pending = s.input_queue.snapshot()
        await run_step(s, first)
        self.assertEqual(s.input_queue.snapshot(), pending)
        self.assertFalse(s.input_queue.closed)
        await s.run_until_idle()
        self.assertEqual(s.state.sent_messages, ["new input seen"])
        self.assertEqual(s.state.step_number, 2)

    async def test_wait_keeps_sources_active_and_accepts_future_input(self):
        s = Session(ScriptedClient([
            [self.call("wait_for_event", "wait")],
            [self.call("send_user_message", "reply", text="back to work", wake_model=False)],
        ]), build_file_tools(self.root))
        self.addAsyncCleanup(s.close)
        source = s.generator_manager.register(TimerGenerator(600000, "later"))
        await s.post_user_message("first")
        await s.run_until_idle()
        self.assertEqual(s.state.status, "waiting")
        self.assertTrue(s.generator_manager.has_active)
        self.assertEqual(s.generator_manager.list()[0]["generator_id"], source["generator_id"])
        self.assertEqual(s.generator_manager.list()[0]["status"], "active")
        await s.post_user_message("continue")
        await s.run_until_idle()
        self.assertEqual(s.state.sent_messages, ["back to work"])
        self.assertTrue(s.generator_manager.has_active)

    async def test_failed_quiet_send_still_queues_error(self):
        sent = []
        def fail(text):
            sent.append(text)
            raise OSError("channel unavailable")
        s = Session(ScriptedClient([
            [self.call("send_user_message", "send", text="hello", wake_model=False)],
            [self.call("wait_for_event", "wait")],
        ]), build_file_tools(self.root), send_message=fail)
        self.addAsyncCleanup(s.close)
        await s.post_user_message("first")
        await run_step(s, await s.input_queue.drain(wait=False))
        pending = s.input_queue.snapshot()
        self.assertEqual(json.loads(pending[0]["item"]["output"])["error"], "OSError")
        await s.run_until_idle()
        self.assertEqual(sent, ["hello"])
        self.assertEqual(s.state.sent_messages, [])
        self.assertTrue(s.input_queue.empty())

    async def test_invalid_wake_flag_does_not_send_and_error_wakes(self):
        for flag in (None, 0, 1, "false"):
            with self.subTest(flag=flag):
                s = Session(ScriptedClient([]), build_file_tools(self.root))
                self.addAsyncCleanup(s.close)
                result = await s.router.invoke_async(self.call(
                    "send_user_message", "send", text="hello", wake_model=flag))
                self.assertTrue(result.wake_model)
                self.assertEqual(json.loads(result.output)["error"], "ValueError")
                self.assertEqual(s.state.sent_messages, [])

    async def test_quiet_send_does_not_suppress_other_tool_events(self):
        s = Session(ScriptedClient([
            [self.call("send_user_message", "send", text="progress", wake_model=False),
             self.call("list_directory", "list", path=".")],
            [self.call("wait_for_event", "wait")],
        ]), build_file_tools(self.root))
        self.addAsyncCleanup(s.close)
        await s.post_user_message("first")
        await run_step(s, await s.input_queue.drain(wait=False))
        self.assertEqual([e["item"]["call_id"] for e in s.input_queue.snapshot()], ["list"])
        await s.run_until_idle()
        self.assertEqual(s.state.step_number, 2)

    async def test_default_send_receipt_survives_suspend_without_replaying_send(self):
        manager = ThreadManager(self.root / "threads")
        first = manager.create(ScriptedClient([
            [self.call("send_user_message", "send", text="delivered once")]
        ]), self.root)
        await first.post_user_message("first")
        await run_step(first, await first.input_queue.drain(wait=False))
        pending = first.input_queue.snapshot()
        await first.suspend()
        second = manager.resume(ScriptedClient([
            [self.call("wait_for_event", "wait")]
        ]), first.thread_id)
        self.addAsyncCleanup(second.close)
        self.assertEqual(second.input_queue.snapshot(), pending)
        await second.run_until_idle()
        self.assertEqual(second.state.sent_messages, [])
        history = second.state.history.snapshot()
        self.assertEqual(sum(i.get("type") == "function_call_output"
                             and i.get("call_id") == "send" for i in history), 1)
