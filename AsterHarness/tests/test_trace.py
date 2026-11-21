import io
import json
import unittest
from unittest.mock import patch

from agent_harness.core.client import ResponsesClient
from agent_harness.core.client_common import Prompt
from agent_harness.core.trace import CompositeTrace, ConsoleTrace


class TraceTests(unittest.TestCase):
    def test_console_prints_full_history(self):
        stream = io.StringIO()
        text = "完整文件内容" * 3000
        trace = ConsoleTrace(stream)
        trace("history_updated", {"step": 2, "items": [{"text": text}]})
        self.assertIn("Step 2", stream.getvalue())
        self.assertIn("当前模型有效历史", stream.getvalue())
        self.assertIn(text, stream.getvalue())

    def test_api_bodies_are_logged_but_auth_header_is_not(self):
        events = []
        trace = CompositeTrace([lambda event, data: events.append({"event": event, **data})])
        body = {"status": "completed", "output": [], "usage": {"input_tokens": 5}}
        with patch("agent_harness.core.client.urlopen",
                   return_value=io.BytesIO(json.dumps(body).encode())):
            client = ResponsesClient("https://example.invalid/v1/responses", "test-model",
                                     "fake-secret-not-to-log", trace=trace)
            client.respond(Prompt("rules", [{"role": "user", "content": "hi"}], []))
        self.assertEqual(events[0]["body"]["model"], "test-model")
        self.assertEqual(next(e["body"] for e in events if e["event"] == "api_response"), body)
        self.assertNotIn("fake-secret-not-to-log", json.dumps(events))
        self.assertNotIn("Authorization", json.dumps(events))
