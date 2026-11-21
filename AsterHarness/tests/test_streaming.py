import asyncio
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_harness.core.client import ResponsesClient
from agent_harness.core.client_common import Prompt, RetryableModelError, retry_after_seconds
from agent_harness.core.response_stream import collect_response, normalize_event
from agent_harness.core.sse import iter_sse
from agent_harness.core.session import Session
from agent_harness.core.tools.handlers import build_file_tools
from agent_harness.core.thread_manager import ThreadManager


CALL = {"type": "function_call", "id": "fc_1", "call_id": "call_1",
        "name": "send_user_message", "arguments": '{"text": "你好", "wake_model": false}'}
REASONING = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque"}


def wire(events):
    return b"".join(("data: " + json.dumps(e, ensure_ascii=False) + "\n\n").encode() for e in events)


def completed(output=None):
    body = {"id": "resp_1", "status": "completed", "usage": {"input_tokens": 42}}
    if output is not None:
        body["output"] = output
    return {"type": "response.completed", "response": body}


def item_event(kind, item, index=0):
    return {"type": "response.output_item." + kind, "output_index": index, "item": item}


class SSEBody(io.BytesIO):
    headers = {"Content-Type": "text/event-stream; charset=utf-8"}


class SSETests(unittest.TestCase):
    def test_comments_crlf_bom_multiline_and_other_fields(self):
        body = io.BytesIO('\ufeff: ping\r\nevent: example\r\nid: 1\r\ndata: {"type":\r\ndata: "response.created"}\r\n\r\n'.encode())
        self.assertEqual(list(iter_sse(body)), [{"type": "response.created"}])

    def test_does_not_commit_unterminated_frame(self):
        self.assertEqual(list(iter_sse(io.BytesIO(b'data: {"type":"response.completed"}\n'))), [])

    def test_done_marker_does_not_mean_response_completed(self):
        with self.assertRaisesRegex(RuntimeError, "before response.completed"):
            collect_response(map(normalize_event, iter_sse(io.BytesIO(b'data: [DONE]\n\n'))))

    def test_invalid_utf8_is_retryable(self):
        with self.assertRaisesRegex(RetryableModelError, "not UTF-8"):
            list(iter_sse(io.BytesIO(b"data: \xff\n\n")))

    def test_malformed_json_and_non_object(self):
        for data in (b'data: {\n\n', b'data: []\n\n'):
            with self.subTest(data=data), self.assertRaises(RuntimeError):
                list(iter_sse(io.BytesIO(data)))

    def test_bounded_frame(self):
        with self.assertRaisesRegex(RuntimeError, "byte limit"):
            list(iter_sse(io.BytesIO(b'data: ' + b'x' * 100), max_event_bytes=32))

    def test_heartbeats_cannot_extend_response_duration_forever(self):
        body = io.BytesIO(b': ping\n: ping\n: ping\n')
        with patch('agent_harness.core.sse.time.monotonic', side_effect=[0, 0, 601]):
            with self.assertRaisesRegex(TimeoutError, 'duration limit'):
                list(iter_sse(body))


class CollectorTests(unittest.TestCase):
    def collect(self, events):
        return collect_response(map(normalize_event, events))

    def test_done_items_order_and_opaque_reasoning_without_final_output(self):
        result = self.collect([item_event("added", CALL, 1), item_event("done", CALL, 1),
                               item_event("done", REASONING, 0), completed()])
        self.assertEqual(result["output"], [REASONING, CALL])
        self.assertEqual(result["usage"]["input_tokens"], 42)

    def test_partial_arguments_not_used_and_unknown_events_tolerated(self):
        result = self.collect([{"type": "future.extension", "data": "ignored"},
                               {"type": "response.function_call_arguments.delta", "delta": '{"text":'},
                               item_event("done", CALL), completed([CALL])])
        self.assertEqual(result["output"], [CALL])

    def test_completion_only_gateway(self):
        self.assertEqual(self.collect([completed([CALL])])["output"], [CALL])

    def test_opt_in_empty_terminal_output_uses_only_complete_ordered_items(self):
        for empty in (None, []):
            terminal = completed([])
            terminal['response']['output'] = empty
            events = [item_event('added', CALL, 1), item_event('done', CALL, 1),
                      item_event('done', REASONING, 0), terminal]
            result = collect_response(map(normalize_event, events), allow_empty_output=True)
            self.assertEqual(result['output'], [REASONING, CALL])
            self.assertEqual(terminal['response']['output'], empty)

    def test_empty_terminal_compatibility_still_refuses_unfinished_or_conflicting_items(self):
        for events in ([item_event('added', CALL), completed([])],
                       [item_event('done', CALL, 1), completed([])],
                       [item_event('done', CALL), item_event('done', CALL), completed([])],
                       [item_event('done', CALL), completed([{**CALL, 'arguments': '{}'}])],
                       [item_event('done', CALL)],
                       [item_event('done', CALL), {'type': 'response.failed'}]):
            with self.subTest(events=events), self.assertRaises(RuntimeError):
                collect_response(map(normalize_event, events), allow_empty_output=True)

    def test_disconnect_after_completed_call_still_fails(self):
        with self.assertRaisesRegex(RuntimeError, "no tools executed"):
            self.collect([item_event("done", CALL)])

    def test_failure_and_incomplete_even_after_completed_item(self):
        for kind in ("response.failed", "response.incomplete", "error"):
            with self.subTest(kind=kind), self.assertRaisesRegex(RuntimeError, "failure or incomplete"):
                self.collect([item_event("done", CALL), {"type": kind, "error": "secret"}])

    def test_transient_stream_failures_are_retryable_and_hide_upstream_body(self):
        for event_type in ('error', 'response.failed'):
            for code in ('server_is_overloaded', 'server_error', 'rate_limit_exceeded'):
                error = {'code': code, 'message': 'secret upstream detail',
                         'headers': {'retry-after': '328', 'retry-after-ms': '328000'}}
                event = {'type': event_type}
                event['response' if event_type == 'response.failed' else 'error'] = (
                    {'error': error} if event_type == 'response.failed' else error)
                with self.subTest(event_type=event_type, code=code), self.assertRaises(RetryableModelError) as caught:
                    self.collect([item_event('done', CALL), event])
                self.assertEqual(caught.exception.retry_after_seconds, 328)
                self.assertNotIn('secret', str(caught.exception))

    def test_quota_auth_invalid_and_incomplete_streams_stay_fatal(self):
        for error in ({'code': 'insufficient_quota', 'type': 'server_error'},
                      {'code': 'invalid_api_key'}, {'code': 'context_length_exceeded'},
                      {'code': 'unknown'}, {'code': []}, 'secret', None):
            with self.subTest(error=error), self.assertRaises(RuntimeError) as caught:
                self.collect([{'type': 'response.failed', 'response': {'error': error}}])
            self.assertNotIsInstance(caught.exception, RetryableModelError)
        with self.assertRaises(RuntimeError) as caught:
            self.collect([{'type': 'response.incomplete',
                           'response': {'incomplete_details': {'reason': 'max_output_tokens'}}}])
        self.assertNotIsInstance(caught.exception, RetryableModelError)

    def test_retry_hints_are_numeric_finite_and_bounded(self):
        for headers, expected in [({'retry-after-ms': '328000'}, 328),
                                  ({'Retry-After': '12'}, 12),
                                  ({'retry-after': '999999'}, 3600),
                                  ({'retry-after': 0}, 0),
                                  ({'retry-after': '-1'}, None),
                                  ({'retry-after': 'NaN'}, None),
                                  ({'retry-after': 'Infinity'}, None),
                                  ({'retry-after': True}, None),
                                  ({'retry-after': {}}, None), (None, None)]:
            with self.subTest(headers=headers):
                self.assertEqual(retry_after_seconds(headers), expected)

    def test_missing_item_gap_duplicate_mismatch_and_invalid_output(self):
        cases = [
            [item_event("added", CALL), completed()],
            [item_event("done", CALL, 2), completed()],
            [item_event("done", CALL), item_event("done", CALL), completed()],
            [item_event("done", CALL), completed([{**CALL, "arguments": "{}"}])],
            [item_event("done", CALL), completed([])],
            [item_event("done", CALL, True), completed()],
            [{"type": "response.completed", "response": {"output": "invalid"}}],
            [completed([42])],
        ]
        for events in cases:
            with self.subTest(events=events), self.assertRaises(RuntimeError):
                self.collect(events)


class StreamingClientTests(unittest.TestCase):
    def test_stream_request_trace_history_shape_and_completion_closes_socket(self):
        events = [{"type": "response.created", "response": {"id": "resp_1"}},
                  item_event("added", {**CALL, "arguments": ""}),
                  {"type": "response.function_call_arguments.delta", "delta": CALL["arguments"]},
                  item_event("done", CALL), completed([CALL])]
        response = SSEBody(wire(events) + b"data: broken trailing event\n\n")
        trace = []
        client = ResponsesClient("https://example.invalid/v1/responses", "model", "not-logged",
                                 trace=lambda event, data: trace.append({"event": event, **data}))
        with patch("agent_harness.core.client.urlopen", return_value=response) as request:
            result = client.respond(Prompt("instructions", [], []))
        self.assertTrue(json.loads(request.call_args.args[0].data)["stream"])
        self.assertEqual(result.output, [CALL])
        self.assertEqual(result.usage, {"input_tokens": 42})
        self.assertTrue(response.closed)
        self.assertEqual([r["data"] for r in trace if r["event"] == "api_stream_event"], events)
        self.assertEqual(trace[-1]["body"], events[-1]["response"])
        self.assertNotIn("not-logged", json.dumps(trace))

    def test_events_are_delivered_before_entire_response_is_read(self):
        response = SSEBody(wire([{"type": "response.created"}, completed([])]))
        client = ResponsesClient("https://example.invalid/responses", "model", "key")
        with patch("agent_harness.core.client.urlopen", return_value=response):
            stream = client.stream_response(Prompt("", [], []))
            self.assertEqual(next(stream).kind, "created")
            self.assertLess(response.tell(), len(response.getvalue()))
            stream.close()
            self.assertTrue(response.closed)

    def test_json_compatibility_is_visible_no_retry(self):
        trace = []
        body = completed([CALL])["response"]
        with patch("agent_harness.core.client.urlopen", return_value=io.BytesIO(json.dumps(body).encode())) as request:
            result = ResponsesClient("https://example.invalid/responses", "model", "key",
                                     trace=lambda e, d: trace.append((e, d))).respond(Prompt("", [], []))
        self.assertEqual(result.output, [CALL])
        self.assertEqual(request.call_count, 1)
        self.assertIn(("api_transport", {"mode": "json_compatibility"}), trace)

    def test_explicit_nonstream_and_step_snapshot(self):
        client = ResponsesClient("https://example.invalid/responses", "model", "key", stream=False)
        snapshot = client.for_step()
        client.streaming = True
        self.assertFalse(snapshot.streaming)
        with patch("agent_harness.core.client.urlopen", return_value=io.BytesIO(json.dumps(completed([])["response"]).encode())) as request:
            snapshot.respond(Prompt("", [], []))
        self.assertFalse(json.loads(request.call_args.args[0].data)["stream"])

    def test_failed_stream_no_api_response_or_retries(self):
        trace = []
        with patch("agent_harness.core.client.urlopen", return_value=SSEBody(wire([item_event("done", CALL)]))) as request:
            with self.assertRaises(RuntimeError):
                ResponsesClient("https://example.invalid/responses", "model", "key",
                                trace=lambda e, d: trace.append(e)).respond(Prompt("", [], []))
        self.assertEqual(request.call_count, 1)
        self.assertNotIn("api_response", trace)


class StreamingSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_streamed_items_resume_once_and_reasoning_is_replayed(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root) / "workspace"
            workspace.mkdir()
            manager = ThreadManager(Path(root) / "threads")
            client = ResponsesClient("https://example.invalid/responses", "model", "key")
            session = manager.create(client, workspace)
            thread_id = session.thread_id
            try:
                await session.post_user_message("first")
                with patch("agent_harness.core.client.urlopen", return_value=SSEBody(wire([
                    item_event("done", REASONING), item_event("done", CALL, 1),
                    completed([REASONING, CALL])]))):
                    await session.run_until_idle()
                await session.suspend()
            finally:
                session.rollout.close()
            resumed = manager.resume(client, thread_id)
            try:
                history = resumed.state.history.snapshot()
                self.assertEqual(history.count(REASONING), 1)
                self.assertEqual(history.count(CALL), 1)
                self.assertEqual(sum(i.get("type") == "function_call_output" for i in history), 1)
                self.assertFalse(any(i.get("type", "").startswith("response.") for i in history))
                second = {**CALL, "call_id": "call_2", "id": "fc_2"}
                await resumed.post_user_message("second")
                with patch("agent_harness.core.client.urlopen", return_value=SSEBody(wire([completed([second])]))) as request:
                    await resumed.run_until_idle()
                payload = json.loads(request.call_args.args[0].data)
                self.assertEqual(payload["input"].count(REASONING), 1)
                self.assertEqual(payload["input"].count(CALL), 1)
            finally:
                await resumed.suspend()
                resumed.rollout.close()

    async def test_interjection_during_stream_waits_for_next_step_and_no_early_tool(self):
        loop = asyncio.get_running_loop()
        reached_done = asyncio.Event()
        release = threading.Event()
        events = []

        def trace(kind, data):
            events.append((kind, data))
            if kind == "api_stream_event" and data["data"]["type"] == "response.output_item.done":
                loop.call_soon_threadsafe(reached_done.set)
                if not release.wait(3):
                    raise RuntimeError("Test stream release timed out")

        second = {**CALL, "call_id": "call_2", "id": "fc_2"}
        with tempfile.TemporaryDirectory() as root:
            client = ResponsesClient("https://example.invalid/responses", "model", "key", trace=trace)
            session = Session(client, build_file_tools(Path(root)), trace=trace)
            try:
                with patch("agent_harness.core.client.urlopen", side_effect=[
                        SSEBody(wire([item_event("done", CALL), completed([CALL])])),
                        SSEBody(wire([completed([second])]))]):
                    await session.post_user_message("first")
                    worker = asyncio.create_task(session.run_until_idle())
                    try:
                        await asyncio.wait_for(reached_done.wait(), 2)
                        self.assertFalse(session.state.sent_messages)
                        self.assertFalse(any(e == "tool_started" for e, _ in events))
                        await session.post_user_message("interjection")
                        self.assertEqual(session.state.step_number, 1)
                    finally:
                        release.set()
                    await asyncio.wait_for(worker, 3)
                self.assertEqual(session.state.step_number, 2)
                self.assertEqual(len(session.state.sent_messages), 2)
                requests = [d["prompt"] for e, d in events if e == "model_request"]
                self.assertNotIn("interjection", json.dumps(requests[0]))
                self.assertIn("interjection", json.dumps(requests[1]))
                self.assertEqual([d["queued_event"]["kind"] for e, d in events if e == "event_enqueued"],
                                 ["user_message", "user_message"])
            finally:
                release.set()
                await session.close()

    async def test_truncated_stream_does_not_record_or_execute_model_output(self):
        with tempfile.TemporaryDirectory() as root:
            client = ResponsesClient("https://example.invalid/responses", "model", "key")
            session = Session(client, build_file_tools(Path(root)))
            try:
                await session.post_user_message("first")
                with patch("agent_harness.core.client.urlopen", return_value=SSEBody(wire([item_event("done", CALL)]))):
                    with self.assertRaises(RuntimeError):
                        await session.run_until_idle()
                self.assertEqual(session.state.sent_messages, [])
                self.assertEqual(len(session.state.history.snapshot()), 1)
                self.assertEqual(session.state.status, "failed")
            finally:
                await session.close()
