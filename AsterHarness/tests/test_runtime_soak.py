"""Long deterministic regressions; optional real-model checks live in scripts/."""

import json
from pathlib import Path
import tempfile
import unittest

from agent_harness.core.client import ScriptedClient
from agent_harness.core.session import Session
from agent_harness.core.session.step import run_step
from agent_harness.core.thread_manager import ThreadManager
from agent_harness.core.tools.registry import Tool, ToolRegistry


def call(name, ident, **arguments):
    return {"type": "function_call", "name": name, "call_id": ident,
            "arguments": json.dumps(arguments)}


class RuntimeSoakTests(unittest.IsolatedAsyncioTestCase):
    async def test_100_work_cycles_with_recoverable_errors_and_no_synthetic_wake(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ToolRegistry()
            executions = []
            def inspect(number):
                executions.append(number)
                if number % 10 == 0:
                    raise ValueError("intentional recoverable test fault")
                return f"observed-{number}"
            registry.register(Tool({"type": "function", "name": "inspect", "parameters": {
                "type": "object", "properties": {"number": {"type": "integer"}}}}, inspect))
            script = []
            for n in range(100):
                script.extend([[call("inspect", f"inspect-{n}", number=n)],
                               [call("send_user_message", f"send-{n}", text=f"cycle-{n}", wake_model=False)]])
            session = Session(ScriptedClient(script), registry, workspace=root)
            try:
                for n in range(100):
                    await session.post_user_message(f"work-{n}")
                    await session.run_until_idle()
                    self.assertEqual(session.state.step_number, (n + 1) * 2)
                    self.assertTrue(session.input_queue.empty())
                    self.assertEqual(session.state.status, "waiting")
                    # Waiting again is not an input and must not consume another response.
                    await session.run_until_idle()
                    self.assertEqual(session.state.step_number, (n + 1) * 2)
                history = session.state.history.snapshot()
                self.assertEqual(executions, list(range(100)))
                self.assertEqual(session.state.sent_messages, [f"cycle-{n}" for n in range(100)])
                calls = [i["call_id"] for i in history if i["type"] == "function_call"]
                results = [i for i in history if i["type"] == "function_call_output"]
                self.assertEqual(calls, [i["call_id"] for i in results])
                self.assertEqual(len(set(calls)), 200)
                self.assertEqual(sum("intentional recoverable test fault" in i["output"]
                                     for i in results), 10)
            finally:
                await session.close()

    async def test_25_resume_cycles_with_pending_tool_outputs_no_reexecution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "note.md"
            fixture.write_text("verified-input\n", encoding="utf-8")
            manager = ThreadManager(root / "threads")
            tool_calls = []
            def trace(event, data):
                if event == "tool_started":
                    tool_calls.append(data["call"])
            session = manager.create(ScriptedClient([]), root, trace=trace)
            thread_id = session.thread_id
            try:
                for n in range(25):
                    session.client = ScriptedClient([[call("read_file", f"read-{n}",
                        path="note.md", start_line=1, end_line=1)]])
                    await session.post_user_message(f"read cycle {n}")
                    await run_step(session, await session.input_queue.drain(wait=False))
                    pending = session.input_queue.snapshot()
                    self.assertEqual(len(pending), 1)
                    self.assertEqual(pending[0]["kind"], "tool_result")
                    old_id = session.session_id
                    await session.suspend()
                    session = manager.resume(ScriptedClient([[call("send_user_message", f"send-{n}",
                        text=f"resumed-{n}", wake_model=False)]]), thread_id, trace=trace)
                    self.assertNotEqual(session.session_id, old_id)
                    self.assertEqual(session.input_queue.snapshot(), pending)
                    await session.run_until_idle()
                    self.assertTrue(session.input_queue.empty())
                    self.assertEqual(session.state.step_number, (n + 1) * 2)
                self.assertEqual([c["name"] for c in tool_calls].count("read_file"), 25)
                self.assertEqual([c["name"] for c in tool_calls].count("send_user_message"), 25)
                history = session.state.history.snapshot()
                outputs = [i for i in history if i["type"] == "function_call_output"]
                self.assertEqual(len(outputs), 50)
                self.assertEqual(len({i["call_id"] for i in outputs}), 50)
                self.assertEqual(fixture.read_text(), "verified-input\n")
            finally:
                await session.close()
