import unittest

from agent_harness.core.client_common import Prompt, RetryableModelError
from agent_harness.core.model_protocol import ModelRequest
from agent_harness.core.messages_client import collect_message, project


class MessagesAdapterTests(unittest.TestCase):
    def test_pending_parallel_tools_precede_intervening_event(self):
        records = [{"type": "message", "role": "user", "text": "start"}]
        for call in ["a", "b"]:
            records.append({"type": "function_call", "name": "check", "call_id": call,
                            "arguments": '{"path":"."}'})
        records.append({"type": "message", "role": "user", "text": "incoming event"})
        for call in ["b", "a"]:
            records.append({"type": "function_call_output", "call_id": call, "output": "ok"})
        wire, _ = project(ModelRequest.from_prompt(Prompt("instructions", records, [])), "example", [])
        self.assertEqual([m["role"] for m in wire["messages"]], ["user", "assistant", "user"])
        blocks = wire["messages"][-1]["content"]
        self.assertEqual([b.get("tool_use_id") for b in blocks], ["b", "a", None])
        self.assertEqual(blocks[-1]["text"], "incoming event")

    def test_discovered_namespaces_can_advertise_identical_names(self):
        groups = [{"type": "namespace", "name": ns, "tools": [
            {"type": "function", "name": "lookup", "parameters": {"type": "object"}}]} for ns in ("alpha", "beta")]
        items = [{"type": "additional_tools", "tools": groups}]
        wire, names = project(ModelRequest.from_prompt(Prompt("test", items, [])), "example", [])
        self.assertEqual(len(wire["tools"]), 2)
        self.assertEqual(set(names.values()), {("alpha", "lookup", "function"), ("beta", "lookup", "function")})

    def test_stream_commits_tool_only_after_complete_message(self):
        start = {"type": "message_start", "message": {"usage": {"input_tokens": 5}, "type": "message"}}
        events = [start, {"type": "content_block_start", "index": 0, "content_block": {
            "type": "tool_use", "name": "check", "id": "call", "input": {}}}]
        events += [{"type": "content_block_delta", "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": part}} for part in ['{"a":', '42}']]
        events += [{"type": "content_block_stop", "index": 0}, {"type": "message_delta",
                   "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 4}}]
        with self.assertRaises(RetryableModelError):
            collect_message(events)
        response = collect_message([*events, {"type": "message_stop"}])
        self.assertEqual(response["content"][0]["input"], {"a": 42})
        self.assertEqual(response["usage"], {"input_tokens": 5, "output_tokens": 4})

    def test_native_thinking_images_and_cache_usage(self):
        import json
        from email.message import Message
        from io import BytesIO
        from unittest.mock import patch
        from agent_harness.core.messages_client import MessagesClient
        from agent_harness.core.client import ResponsesClient
        seed = {"type": "message", "role": "user", "text": "check",
                "images": [{"type": "input_image", "image_url": "data:image/png;base64,AA=="}]}
        thinking = {"type": "thinking", "thinking": "check first", "signature": "signed"}
        content = [thinking, {"type": "tool_use", "id": "toolu_1", "name": "check", "input": {"x": 1}}]
        usage = {"input_tokens": 5, "output_tokens": 3, "cache_creation_input_tokens": 7, "cache_read_input_tokens": 11}
        body = BytesIO(json.dumps({"type": "message", "stop_reason": "tool_use", "content": content, "usage": usage}).encode())
        body.headers = Message()
        body.headers["Content-Type"] = "application/json"
        tools = [{"type": "function", "name": "check", "parameters": {"type": "object"}}]
        client = MessagesClient("https://example.test/v1/messages", "test", "test-key")
        with patch("agent_harness.core.messages_client.build_opener") as opener:
            opener.return_value.open.return_value = body
            reply = client.respond(Prompt("instructions", [seed], tools))
            sent = opener.return_value.open.call_args.args[0]
            self.assertEqual(sent.get_header("X-api-key"), "test-key")
            image = json.loads(sent.data)["messages"][0]["content"][1]
            self.assertEqual(image["source"], {"type": "base64", "media_type": "image/png", "data": "AA=="})
        self.assertEqual(reply.usage["input_tokens"], 23)
        self.assertEqual(reply.usage["total_tokens"], 26)
        history = [seed, *reply.output, {"type": "function_call_output", "call_id": "toolu_1", "output": "ok"}]
        prompt = Prompt("instructions", history, tools)
        target = [client.endpoint, client.model]
        same, _ = project(ModelRequest.from_prompt(prompt), client.model, target)
        self.assertEqual(same["messages"][1]["content"][0], thinking)
        other, _ = project(ModelRequest.from_prompt(prompt), "other", [client.endpoint, "other"])
        self.assertNotIn("signed", json.dumps(other))
        with patch("agent_harness.core.client.urlopen", side_effect=RuntimeError("captured")) as request:
            with self.assertRaisesRegex(RuntimeError, "captured"):
                ResponsesClient("https://example.test/v1/responses", "other", "unused").respond(prompt)
            self.assertNotIn(b'messages_block', request.call_args.args[0].data)
