"""Invocation isolation and unchanged error/lifecycle boundaries."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from agent_harness.core.client import ScriptedClient
from agent_harness.core.session import Session
from agent_harness.core.session.step_context import StepContext
from agent_harness.core.tools.context import ToolInvocation
from agent_harness.core.tools.registry import Tool, ToolRegistry
from agent_harness.core.tools.router import ToolRouter


def call(name="probe", **arguments):
    return {"type": "function_call", "name": name, "call_id": "probe1",
            "arguments": json.dumps(arguments)}


def spec():
    return {"type": "function", "name": "probe", "description": "probe",
            "parameters": {"type": "object", "properties": {}}}


class ToolRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.session = Session(ScriptedClient([]), ToolRegistry(), workspace=Path(self.temp.name))
        self.addAsyncCleanup(self.session.close)

    async def test_sync_handler_sees_captured_invocation_and_scope_does_not_leak(self):
        session = self.session
        observations = []

        def handler():
            invocation = session.tool_runtime.current_invocation
            observations.append((invocation.step.history_view, session._history_view,
                                 session._compact_boundary))
            return "observed"

        session.router.registry.register(Tool(spec(), handler))
        session.record_items([{"type": "message", "role": "user", "text": "old work"}])
        context = StepContext.capture(session)
        boundary = set(session.state.history.ids())
        await session.tool_runtime.execute_batch(context, [call()], boundary)
        self.assertEqual(observations, [(context.history_view, context.history_view, frozenset(boundary))])
        self.assertIsNone(session.tool_runtime.current_invocation)
        self.assertIsNone(session._history_view)
        self.assertIsNone(session._compact_boundary)
        self.assertEqual(session.input_queue.snapshot()[0]["item"]["output"], "observed")

    async def test_running_tool_context_is_not_visible_in_independent_task(self):
        session = self.session
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler():
            entered.set()
            await release.wait()
            return "done"

        session.router.registry.register(Tool(spec(), handler))
        context = StepContext.capture(session)
        worker = asyncio.create_task(session.tool_runtime.execute_batch(context, [call()], set()))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.assertIsNone(session.tool_runtime.current_invocation)
            self.assertIsNone(session._history_view)
        finally:
            release.set()
            await worker

    async def test_cancellation_clears_invocation_without_inventing_receipt(self):
        session = self.session
        entered = asyncio.Event()

        async def handler():
            entered.set()
            await asyncio.Future()

        session.router.registry.register(Tool(spec(), handler))
        context = StepContext.capture(session)
        worker = asyncio.create_task(session.tool_runtime.execute_batch(context, [call()], set()))
        await asyncio.wait_for(entered.wait(), 2)
        worker.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await worker
        self.assertIsNone(session.tool_runtime.current_invocation)
        self.assertIsNone(session._history_view)
        self.assertTrue(session.input_queue.empty())

    async def test_fatal_runtime_error_still_propagates_and_cleans_scope(self):
        def handler():
            raise RuntimeError("fatal runtime fault")

        session = self.session
        session.router.registry.register(Tool(spec(), handler))
        with self.assertRaisesRegex(RuntimeError, "fatal runtime fault"):
            await session.tool_runtime.execute_batch(StepContext.capture(session), [call()], set())
        self.assertIsNone(session.tool_runtime.current_invocation)
        self.assertTrue(session.input_queue.empty())

    async def test_error_parsing_order_and_error_payload_are_unchanged(self):
        router = ToolRouter(ToolRegistry())
        for payload in ({"arguments": "bad"}, {"arguments": "[]"}, {"arguments": "{}"}):
            for result in (router.invoke(payload), await router.invoke_async(payload)):
                self.assertTrue(result.wake_model)
                self.assertEqual(set(json.loads(result.output)), {"error", "message"})
            expected = "JSONDecodeError" if payload["arguments"] == "bad" else (
                "ValueError" if payload["arguments"] == "[]" else "KeyError")
            self.assertEqual(json.loads(result.output)["error"], expected)

    def test_invocation_keeps_original_call_and_boundary(self):
        item, boundary = call(), {"h000000000001"}
        invocation = ToolInvocation(item, StepContext.capture(self.session), boundary)
        item["name"] = "changed"
        boundary.clear()
        exposed = invocation.call
        exposed["name"] = "changed again"
        self.assertEqual(invocation.call["name"], "probe")
        self.assertEqual(invocation.compact_boundary, frozenset({"h000000000001"}))
