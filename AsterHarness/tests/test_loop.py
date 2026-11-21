import asyncio
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_harness.core.client import ResponsesClient, ScriptedClient, from_api_item, to_api_item
from agent_harness.core.client_common import ModelResponse, Prompt
from agent_harness.core.session import Session, SessionConfiguration
from agent_harness.core.session.step import run_step
from agent_harness.core.tools.handlers import build_file_tools
from agent_harness.core.tools.registry import Tool
from agent_harness.core.tools.router import ToolRouter
from agent_harness.core.trace import JsonlTrace


def call(name="read_file", arguments=None, call_id="call_1"):
    if arguments is None:
        arguments = {"path": "note.md", "start_line": 1, "end_line": 2}
    return {"type": "function_call", "name": name, "call_id": call_id,
            "arguments": json.dumps(arguments)}


def message(text="完成", call_id="send_1"):
    # These fixtures explicitly wait after sending; default waking sends are
    # exercised with follow-up work in test_terminal_channel.
    return call("send_user_message", {"text": text, "wake_model": False}, call_id)


class LoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "note.md").write_text("hello\nworld\n", encoding="utf-8")
        self.registry = build_file_tools(self.root)
        self.events = []

    def session(self, responses, **kwargs):
        return Session(ScriptedClient(responses), self.registry,
                       trace=lambda event, data: self.events.append((event, data)), **kwargs)

    async def drive(self, session, text="hi"):
        await session.post_user_message(text)
        await session.run_until_idle()

    async def test_real_tool_result_reasoning_and_schemas_in_next_request(self):
        reasoning = {"type": "reasoning", "summary": [], "encrypted_content": "opaque"}
        session = self.session([[reasoning, call()], [message()]])
        await self.drive(session)
        requests = [d["prompt"] for e, d in self.events if e == "model_request"]
        self.assertEqual(len(requests), 2)
        self.assertEqual(len(requests[0]["input"]), 1)
        self.assertEqual(requests[1]["input"][1], reasoning)
        self.assertEqual(requests[1]["input"][-1]["output"], "1: hello\n2: world\n")
        self.assertEqual(len(requests[1]["tools"]), 28)
        self.assertEqual(session.state.sent_messages, ["完成"])
        self.assertEqual(session.state.status, "waiting")

    async def test_tool_result_only_enters_history_after_queue_drain(self):
        session = self.session([[call()], [message()]])
        await session.post_user_message("read")
        await run_step(session, await session.input_queue.drain(wait=False))
        self.assertEqual(len(session.state.history.snapshot()), 2)
        self.assertEqual(session.input_queue.snapshot()[0]["kind"], "tool_result")
        await session.run_until_idle()
        self.assertEqual(session.state.history.snapshot()[2]["call_id"], "call_1")

    async def test_all_pending_user_events_batched_and_snapshots_immutable(self):
        session = self.session([[message()]])
        await session.post_user_message("A")
        await session.post_user_message("B")
        snapshot = session.input_queue.snapshot()
        snapshot[0]["item"]["text"] = "changed"
        await session.run_until_idle()
        history = session.state.history.snapshot()
        self.assertEqual([i["text"] for i in history[:2]], ["A", "B"])
        history[0]["text"] = "changed"
        self.assertEqual(session.state.history.snapshot()[0]["text"], "A")
        self.assertEqual(session.state.step_number, 1)

    async def test_idle_batch_does_not_close_session_history_continues(self):
        session = self.session([[message("first")], [message("second", "send_2")]])
        await session.run_until_idle()
        self.assertEqual(session.state.step_number, 0)
        await self.drive(session, "A")
        await self.drive(session, "B")
        self.assertEqual(len(session.state.history.snapshot()), 6)
        self.assertEqual(session.state.history.snapshot()[3]["text"], "B")
        self.assertEqual(session.state.sent_messages, ["first", "second"])

    async def test_live_scheduler_waits_and_accepts_later_input(self):
        sent = asyncio.Queue()
        session = self.session([[message("first")], [message("second", "send_2")]],
                               send_message=sent.put_nowait)
        worker = asyncio.create_task(session.run())
        await session.post_user_message("A")
        self.assertEqual(await asyncio.wait_for(sent.get(), 2), "first")
        await asyncio.sleep(0)
        self.assertFalse(worker.done())
        await session.post_user_message("B")
        self.assertEqual(await asyncio.wait_for(sent.get(), 2), "second")
        await session.close()
        await asyncio.wait_for(worker, 2)
        self.assertEqual(session.state.status, "stopped")
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await session.post_user_message("C")

    async def test_ingress_during_model_and_tool_does_not_overlap_steps(self):
        for slow_model in (True, False):
            with self.subTest(slow_model=slow_model):
                entered, release = threading.Event(), threading.Event()
                def slow():
                    entered.set()
                    if not release.wait(2):
                        raise RuntimeError("test timed out")
                    return "done"
                self.registry.register(Tool({"type": "function", "name": f"slow_{slow_model}",
                    "parameters": {"type": "object", "properties": {}}}, slow))
                session = self.session([[call(f"slow_{slow_model}", {}, "slow")], [message()]])
                if slow_model:
                    original = session.client.respond
                    first = True
                    def respond(prompt):
                        nonlocal first
                        if first:
                            first = False
                            slow()
                        return original(prompt)
                    session.client.respond = respond
                await session.post_user_message("A")
                worker = asyncio.create_task(session.run_until_idle())
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    await session.post_user_message("B")
                    self.assertEqual(session.state.step_number, 1)
                finally:
                    release.set()
                await asyncio.wait_for(worker, 3)
                history = session.state.history.snapshot()
                self.assertEqual(history[2]["text"], "B")
                self.assertEqual(history[3]["type"], "function_call_output")

    async def test_mixed_send_and_tools_allowed(self):
        session = self.session([[message("working"), call(call_id="a"), call(call_id="b")],
                                [message("done", "send_2")]])
        await self.drive(session)
        self.assertEqual(session.state.sent_messages, ["working", "done"])
        queued = [d["queued_event"]["item"] for e, d in self.events if e == "event_enqueued"]
        self.assertEqual([i.get("call_id") for i in queued], [None, "a", "b"])
        self.assertEqual(session.state.history.snapshot()[-1]["call_id"], "send_2")

    async def test_errors_are_events_including_failed_send(self):
        attempts = []
        def sink(text):
            attempts.append(text)
            if len(attempts) == 1:
                raise OSError("temporarily unavailable")
        session = self.session([[call("missing")], [message("retry", "bad")],
                                [message("ok")]], send_message=sink)
        await self.drive(session)
        self.assertEqual(session.state.sent_messages, ["ok"])
        outputs = [i for i in session.state.history.snapshot() if i["type"] == "function_call_output"]
        self.assertIn("error", outputs[0]["output"])
        self.assertIn("error", outputs[1]["output"])

    async def test_invalid_send_returns_error_then_correction(self):
        for args in ({"text": ""}, {"text": 123}, {}, {"text": "ok", "extra": True}):
            session = self.session([[call("send_user_message", args, "bad")], [message()]])
            await self.drive(session)
            self.assertIn("error", session.state.history.snapshot()[2]["output"])

    async def test_plain_text_is_recorded_without_delivery_or_synthetic_wake(self):
        item = {"type": "message", "role": "assistant", "text": "not delivered"}
        session = self.session([[item]])
        await self.drive(session)
        self.assertEqual(session.state.history.snapshot()[-1], item)
        self.assertEqual(session.state.sent_messages, [])
        self.assertTrue(session.input_queue.empty())

    async def test_safety_limit_retains_pending_events_and_failure_blocks_restart(self):
        session = self.session([[call()]], configuration=SessionConfiguration(max_consecutive_steps=1))
        with self.assertRaisesRegex(RuntimeError, "step limit"):
            await self.drive(session)
        self.assertEqual(session.state.status, "failed")
        self.assertEqual(session.input_queue.snapshot()[0]["kind"], "tool_result")
        with self.assertRaises(RuntimeError):
            await session.run_until_idle()

    async def test_duplicate_missing_and_reused_call_ids_rejected(self):
        for responses in ([[call(), call()]], [[call(call_id="")]], [[call()], [call()]]):
            session = self.session(responses)
            with self.assertRaisesRegex(RuntimeError, "call_id"):
                await self.drive(session)
            self.assertEqual(session.state.status, "failed")

    async def test_model_error_retains_in_flight_input(self):
        session = self.session([])
        with self.assertRaisesRegex(RuntimeError, "exhausted"):
            await self.drive(session)
        self.assertEqual(session.state.in_flight_events[0]["item"]["text"], "hi")

    async def test_close_drains_tool_chain(self):
        session = self.session([[call()], [message()]])
        await session.post_user_message("read")
        await session.close()
        await session.run()
        self.assertEqual(session.state.sent_messages, ["完成"])
        self.assertEqual(session.state.status, "stopped")

    async def test_close_wakes_empty_live_scheduler(self):
        session = self.session([])
        worker = asyncio.create_task(session.run())
        await asyncio.sleep(0)
        await session.close()
        await asyncio.wait_for(worker, 2)
        self.assertEqual(session.state.step_number, 0)
        self.assertEqual(session.state.status, "stopped")

    async def test_concurrent_scheduler_rejected_and_cancel_recorded(self):
        session = self.session([])
        worker = asyncio.create_task(session.run())
        await asyncio.sleep(0)
        with self.assertRaisesRegex(RuntimeError, "scheduler"):
            await session.run_until_idle()
        worker.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await worker
        self.assertEqual(session.state.status, "failed")

    async def test_jsonl_trace_records_receipt_without_waking_event(self):
        path = self.root / "trace.jsonl"
        session = Session(ScriptedClient([[message()]]), self.registry, trace=JsonlTrace(path))
        await self.drive(session)
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(sum(r["event"] == "event_enqueued" for r in rows), 1)
        self.assertEqual(sum(r["event"] == "step_started" for r in rows), 1)
        self.assertIn("non_waking_tool_receipt", [r.get("reason") for r in rows])

    def test_file_scope_read_budget_and_malformed_arguments(self):
        router = ToolRouter(self.registry)
        for args in ({"path": "../outside", "start_line": 1, "end_line": 2},
                     {"path": "note.md", "start_line": 1, "end_line": 201}):
            self.assertIn("error", router.dispatch(call(arguments=args))["output"])
        invalid = call()
        invalid["arguments"] = "not json"
        self.assertIn("error", router.dispatch(invalid)["output"])
        (self.root / "huge.txt").write_text("x" * 200000)
        result = router.dispatch(call(arguments={"path": "huge.txt", "start_line": 1, "end_line": 1}))
        self.assertLess(len(result["output"]), 12100)
        self.assertIn("truncated", result["output"])

    def test_responses_adapter_and_message_mapping(self):
        body = io.BytesIO(json.dumps({"status": "completed", "output": [message()]}).encode())
        with patch("agent_harness.core.client.urlopen", return_value=body) as request:
            response = ResponsesClient("https://example.invalid/v1/responses", "test-model",
                                       "test-key").respond(Prompt("instructions", [], []))
        payload = json.loads(request.call_args.args[0].data)
        self.assertFalse(payload["store"])
        self.assertEqual(payload["tool_choice"], "auto")
        self.assertEqual(response.output, [message()])
        internal = {"type": "message", "role": "user", "text": "你好"}
        self.assertEqual(from_api_item(to_api_item(internal)), internal)
        self.assertNotIn("content", internal)
        self.assertEqual(to_api_item({**internal, "role": "assistant"})["content"][0]["type"], "output_text")

    def test_registry_not_mutated_and_configuration_validated(self):
        self.session([])
        self.session([])
        self.assertEqual(len(self.registry.specs()), 2)
        with self.assertRaises(ValueError):
            SessionConfiguration(max_consecutive_steps=0)
