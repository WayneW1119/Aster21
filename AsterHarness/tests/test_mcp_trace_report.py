"""Shared definitions must preserve complete API bodies, not just summaries."""

import json
from pathlib import Path
import tempfile
import unittest

from scripts.mcp_trace_report import build, expand, unpack


class TraceReportTests(unittest.TestCase):
    def test_complete_request_response_roundtrip_with_repeated_prefix(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix = {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "中文\n```code```"}]}
            response_item = {"type": "reasoning", "encrypted_content": "opaque-unchanged", "summary": []}
            request = {"model": "test", "instructions": "Repeated instructions", "input": [prefix],
                       "tools": [{"type": "tool_search", "parameters": {"$ref": "literal-schema-ref"}}], "stream": True}
            response = {"id": "response-1", "status": "completed", "output": [response_item],
                        "instructions": request["instructions"], "tools": request["tools"],
                        "usage": {"input_tokens": 123, "output_tokens": 9}, "extra": {"nullable": None}}
            rows = []
            for step in (1, 2):
                rows.extend([
                    {"event": "step_started", "session_id": "session", "step": step, "events": []},
                    {"event": "model_request", "prompt": {"base_instructions": request["instructions"],
                        "input": [prefix], "tools": request["tools"], "memory": None}, "world_state": {}},
                    {"event": "api_request", "endpoint": "https://example.invalid/responses", "body": request},
                    {"event": "api_response", "body": response},
                    {"event": "step_completed", "step": step}])
            (root / "session-1.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            packed = build(root)
            self.assertEqual(packed["verification"]["bodies_reconstructed_exactly"], 4)
            for step in packed["steps"]:
                self.assertEqual(unpack(step["request"], packed["definitions"]), request)
                self.assertEqual(unpack(step["response"], packed["definitions"]), response)
            self.assertEqual(packed["steps"][0]["request"]["shared"], packed["steps"][1]["request"]["shared"])
            expand(root / "process.json", root / "expanded.jsonl")
            restored = [json.loads(line) for line in (root / "expanded.jsonl").read_text().splitlines()]
            self.assertEqual(restored[1]["response"], response)
            markdown = (root / "process.md").read_text()
            self.assertLess(markdown.index("公共与复用定义"), markdown.index("按 Step 的完整输入"))

    def test_failed_step_is_reported_without_inventing_response(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = [{"event": "step_started", "session_id": "s", "step": 1, "events": []},
                    {"event": "api_request", "endpoint": "https://example.invalid", "body": {"input": [], "tools": []}},
                    {"event": "session_error", "error": "upstream failed"}]
            (root / "session-1.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            packed = build(root)
            self.assertEqual(packed["verification"]["responses"], 0)
            self.assertNotIn("response", packed["steps"][0])
