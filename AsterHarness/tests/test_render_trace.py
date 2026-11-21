import json
import tempfile
import unittest
from pathlib import Path

from agent_harness.render_trace import block, render


class RenderTraceTests(unittest.TestCase):
    def test_every_event_is_preserved_as_complete_json(self):
        rows = [
            {"event": "model_request", "step": 1,
             "prompt": {"base_instructions": "rules", "input": [], "tools": []}},
            {"event": "tool_result", "step": 1,
             "result": {"type": "function_call_output", "call_id": "abc",
                        "output": "第一行\n第二行\n```\n"}},
            {"event": "unknown_future_event", "extra": {"keep_this": [1, 2]}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows))
            report = render(path)
        for row in rows:
            self.assertIn(json.dumps(row, ensure_ascii=False, indent=2), report)
        self.assertIn("第一行\n第二行", report)
        self.assertIn("事件 03 · Step 1", report)

    def test_markdown_fence_cannot_be_closed_by_file_contents(self):
        self.assertTrue(block("```\ntext").startswith("````text\n"))

    def test_old_and_new_non_waking_receipts_remain_readable(self):
        rows = [{"event": "history_updated", "reason": reason, "items": []}
                for reason in ("non_waking_send_receipt", "non_waking_tool_receipt")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows))
            report = render(path)
        self.assertIn("追加发送回执（不产生唤醒事件）", report)
        self.assertIn("追加工具回执（不产生唤醒事件）", report)
