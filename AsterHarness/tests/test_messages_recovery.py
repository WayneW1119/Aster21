import asyncio
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent_harness.core.messages_client import MessagesClient, collect_message
from agent_harness.core.thread_manager import ThreadManager
from agent_harness.core.session.step import run_step


class MessagesRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_lost_completion_retries_then_checkpoint_delivers_receipt_once(self):
        requests = []
        call = {"type": "tool_use", "name": "list_directory", "id": "once", "input": {"path": "."}}
        class Endpoint(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                if len(requests) == 1:
                    events = [{"type": "message_start", "message": {"type": "message", "usage": {}}},
                              {"type": "content_block_start", "index": 0, "content_block": call},
                              {"type": "content_block_stop", "index": 0}]
                    data = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
                    mime = "text/event-stream"
                else:
                    answer = {"type": "tool_use", "name": "send_user_message", "id": "finish",
                              "input": {"text": "done", "wake_model": False}}
                    data = json.dumps({"type": "message", "stop_reason": "tool_use",
                        "content": [call if len(requests) == 2 else answer],
                        "usage": {"input_tokens": 20, "output_tokens": 10}})
                    mime = "application/json"
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.end_headers()
                self.wfile.write(data.encode())

        with tempfile.TemporaryDirectory() as directory, ThreadingHTTPServer(("127.0.0.1", 0), Endpoint) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            client = MessagesClient(f"http://127.0.0.1:{server.server_port}/messages", "example", "unused", allow_http=True)
            manager = ThreadManager(Path(directory) / "threads")
            first = manager.create(client, Path(directory))
            try:
                await first.post_user_message("list")
                await run_step(first, await first.input_queue.drain(wait=False))
                self.assertEqual(len(requests), 2)
                self.assertEqual(requests[0], requests[1])
                self.assertEqual(len(first.input_queue.snapshot()), 1)
                await first.suspend()
            finally:
                await first.close()
            resumed = manager.resume(client, first.thread_id)
            try:
                await asyncio.wait_for(resumed.run_until_idle(), 10)
                self.assertEqual(len(requests), 3)
                self.assertEqual(resumed.state.sent_messages, ["done"])
                receipts = [b for m in requests[-1]["messages"] for b in m["content"] if b["type"] == "tool_result"]
                self.assertEqual([b["tool_use_id"] for b in receipts], ["once"])
            finally:
                await resumed.close()
                await asyncio.to_thread(server.shutdown)
                thread.join()

    def test_malformed_stream_order_and_delta_types(self):
        start = {"type": "message_start", "message": {"type": "message", "usage": {}}}
        block = {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
        delta = {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}
        stop = {"type": "message_stop"}
        wrong = {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{}"}}
        for sequence in ([start, stop], [start, block, delta], [start, delta, block], [start, block, wrong],
                         [start, delta, delta], [start, block, block]):
            with self.subTest(sequence=sequence), self.assertRaises(RuntimeError):
                collect_message(sequence)
