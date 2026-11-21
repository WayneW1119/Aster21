import json
import unittest

from agent_harness.core.client_common import Prompt
from agent_harness.core.model_protocol import ModelItem, ModelReply, invoke


class ModelProtocolTests(unittest.TestCase):
    def test_semantic_adapter_receives_history_and_preserves_opaque_records(self):
        records = [
            {"role": "user", "type": "message", "text": "read both"},
            {"type": "function_call", "call_id": "a", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "a", "output": "ok"},
            {"type": "reasoning", "encrypted_content": "opaque"},
            {"data": [1, 2], "type": "future_item"},
        ]
        prompt = Prompt("instructions", records, [], memory=records[0], cache_session_id="thread")

        class Adapter:
            def generate(self, request):
                self.request = request
                return ModelReply([ModelItem("message", {"role": "assistant", "text": "done"})], {})

        adapter = Adapter()
        reply = invoke(adapter, prompt)
        self.assertEqual([item.kind for item in adapter.request.history],
                         ["message", "tool_call", "tool_result", "reasoning", "extension"])
        restored = adapter.request.to_prompt()
        self.assertEqual(json.dumps(restored.input), json.dumps(records))
        self.assertEqual(restored.memory, prompt.memory)
        self.assertEqual(restored.cache_session_id, "thread")
        self.assertEqual(reply.to_response().output, [{"type": "message", "role": "assistant", "text": "done"}])


class AdapterResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_tool_receipts_survive_resume_into_semantic_adapter(self):
        import tempfile
        from pathlib import Path
        from agent_harness.core.thread_manager import ThreadManager
        from agent_harness.core.session.step import run_step

        class ListTwice:
            def generate(self, request):
                return ModelReply([ModelItem("tool_call", {
                    "name": "list_directory", "arguments": '{"path":"."}', "call_id": ident,
                }) for ident in ("first", "second")], None)

        class Answer:
            def generate(self, request):
                self.receipts = [item.fields["call_id"] for item in request.history if item.kind == "tool_result"]
                return ModelReply([ModelItem("tool_call", {
                    "name": "send_user_message", "arguments": '{"text":"done","wake_model":false}',
                    "call_id": "answer",
                })], None)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = ThreadManager(root / "threads")
            first = manager.create(ListTwice(), root)
            try:
                await first.post_user_message("list twice")
                await run_step(first, await first.input_queue.drain(wait=False))
                await first.suspend()
            finally:
                await first.close()
            adapter = Answer()
            resumed = manager.resume(adapter, first.thread_id)
            try:
                await resumed.run_until_idle()
                self.assertEqual(adapter.receipts, ["first", "second"])
                self.assertEqual(resumed.state.sent_messages, ["done"])
            finally:
                await resumed.close()


class ExistingClientCompatibilityTests(unittest.TestCase):
    def test_custom_stream_and_prompt_labels_survive_protocol_boundary(self):
        from agent_harness.core.client import ResponsesClient
        from agent_harness.core.client_common import ResponseEvent

        class ExistingClient(ResponsesClient):
            def stream_response(self, prompt):
                self.labels = prompt.history_labels
                yield ResponseEvent("completed", {"response": {"status": "completed", "output": [
                    {"type": "message", "role": "assistant", "content": [{"text": "ok"}]},
                ]}})

        client = ExistingClient("https://example.test/responses", "test", "unused")
        reply = invoke(client, Prompt("", [], [], history_labels=["m1"]))
        self.assertEqual(client.labels, ["m1"])
        self.assertEqual(reply.items[0].fields["text"], "ok")
