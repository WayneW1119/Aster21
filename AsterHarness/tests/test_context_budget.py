"""Context reminders are measured, deduplicated events, never forced compactions."""

import json
from pathlib import Path
import tempfile
import unittest

from agent_harness.core.client_common import ModelResponse
from agent_harness.core.session import Session, SessionConfiguration
from agent_harness.core.session.context_budget import ContextBudget
from agent_harness.core.session.step import run_step
from agent_harness.core.thread_manager import ThreadManager
from agent_harness.core.tools.handlers import build_file_tools


def call(name, identifier, **arguments):
    return {"type": "function_call", "name": name, "call_id": identifier,
            "arguments": json.dumps(arguments)}


class UsageClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.prompts = []

    def respond(self, prompt):
        self.prompts.append(prompt)
        return next(self.responses)


class ContextBudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.events = []

    def session(self, responses, threshold=100):
        client = UsageClient(responses)
        session = Session(client, build_file_tools(self.root), workspace=self.root,
            configuration=SessionConfiguration(context_budget_warning_tokens=threshold),
            trace=lambda event, data: self.events.append((event, data)))
        self.addAsyncCleanup(session.close)
        return session, client

    async def test_warning_wakes_waiting_agent_once_and_does_not_compact(self):
        session, client = self.session([
            ModelResponse([call("wait_for_event", "wait1")], {"input_tokens": 100}),
            ModelResponse([call("wait_for_event", "wait2")], {"input_tokens": 110}),
        ])
        await session.post_user_message("keep working")
        await session.run_until_idle()
        self.assertEqual(len(client.prompts), 2)
        notice = json.loads(client.prompts[1].input[-1]["text"].split("\n")[1])
        self.assertEqual(notice["kind"], "context_budget_warning")
        self.assertEqual(notice["data"]["input_tokens"], 100)
        self.assertEqual(session.state.history.generation, 0)
        self.assertEqual(session.state.status, "waiting")
        self.assertEqual(session.state.sent_messages, [])
        self.assertEqual(sum(e == "context_budget_warning" for e, _ in self.events), 1)
        self.assertEqual(session.state.history.snapshot()[0]["text"], "keep working")

    async def test_warning_follows_every_receipt_and_cached_input_counts(self):
        session, _ = self.session([ModelResponse([
            call("read_file", "read1", path="missing", start_line=1, end_line=1),
            call("list_event_generators", "list1")],
            {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 100}})])
        await session.post_user_message("work")
        await run_step(session, await session.input_queue.drain(wait=False))
        pending = session.input_queue.snapshot()
        self.assertEqual([e["kind"] for e in pending],
                         ["tool_result", "tool_result", "context_budget_warning"])
        self.assertEqual([e["item"]["call_id"] for e in pending[:2]], ["read1", "list1"])

    async def test_disabled_missing_or_invalid_usage_never_wakes_agent(self):
        for threshold, usage in [(None, {"input_tokens": 999}), (100, None),
                (100, {}), (100, {"input_tokens": True}),
                (100, {"input_tokens": "100"}), (100, {"input_tokens": -1}),
                (100, {"input_tokens": 99, "output_tokens": 99999})]:
            with self.subTest(threshold=threshold, usage=usage):
                session, client = self.session([
                    ModelResponse([call("wait_for_event", "wait")], usage)], threshold)
                await session.post_user_message("hi")
                await session.run_until_idle()
                self.assertEqual(len(client.prompts), 1)
                self.assertTrue(session.input_queue.empty())

    def test_milestones_not_each_step_and_new_generation_rearms(self):
        budget = ContextBudget()
        for tokens, generation, expected in [(99, 0, False), (100, 0, True),
                (124, 0, False), (125, 0, True), (120, 0, False),
                (190, 0, True), (190, 0, False), (100, 1, True)]:
            warning = budget.warning({"input_tokens": tokens}, 100, generation)
            self.assertEqual(warning is not None, expected)
            if warning:
                budget.commit(warning)

    async def test_pre_compaction_usage_does_not_enqueue_stale_warning(self):
        session, _ = self.session([])
        session.record_items([{"type": "message", "role": "user", "text": "old work"}])
        identifier = session.state.history.ids()[0]
        session._compact_boundary = {identifier}
        try:
            session.compact_history(identifier, identifier, 0, "old stage complete")
        finally:
            session._compact_boundary = None
        await session.warn_context_budget({"input_tokens": 1000}, 100, 0)
        self.assertTrue(session.input_queue.empty())
        await session.warn_context_budget({"input_tokens": 100}, 100, 1)
        self.assertEqual(session.input_queue.snapshot()[0]["kind"], "context_budget_warning")

    async def test_pending_warning_and_dedup_survive_resume(self):
        manager = ThreadManager(self.root / "threads")
        first = manager.create(UsageClient([
            ModelResponse([call("wait_for_event", "wait1")], {"input_tokens": 100})]),
            self.root, configuration=SessionConfiguration(context_budget_warning_tokens=100))
        self.addAsyncCleanup(first.close)
        await first.post_user_message("hi")
        await run_step(first, await first.input_queue.drain(wait=False))
        pending = first.input_queue.snapshot()
        await first.close()
        client = UsageClient([
            ModelResponse([call("wait_for_event", "wait2")], {"input_tokens": 110}),
            ModelResponse([call("wait_for_event", "wait3")], {"input_tokens": 125}),
            ModelResponse([call("wait_for_event", "wait4")], {"input_tokens": 126}),
        ])
        resumed = manager.resume(client, first.thread_id)
        self.addAsyncCleanup(resumed.close)
        self.assertEqual(resumed.configuration.context_budget_warning_tokens, 100)
        self.assertEqual(resumed.input_queue.snapshot(), pending)
        await resumed.run_until_idle()
        self.assertEqual(len(client.prompts), 1)
        await resumed.post_user_message("continue")
        await resumed.run_until_idle()
        self.assertEqual(len(client.prompts), 3)
        await resumed.close()
        third = manager.resume(UsageClient([]), first.thread_id)
        self.addAsyncCleanup(third.close)
        self.assertEqual(third.context_budget.previous["level"], 1)

    async def test_legacy_thread_configuration_uses_default(self):
        manager = ThreadManager(self.root / "threads")
        first = manager.create(UsageClient([]), self.root)
        self.addAsyncCleanup(first.close)
        path = first.rollout.path
        await first.close()
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        del rows[0]["payload"]["configuration"]["context_budget_warning_tokens"]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        resumed = manager.resume(UsageClient([]), first.thread_id)
        self.addAsyncCleanup(resumed.close)
        self.assertEqual(resumed.configuration.context_budget_warning_tokens, 100_000)

    def test_invalid_configuration_and_saved_state_rejected(self):
        for value in (True, 0, -1, 1.5, "100000"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SessionConfiguration(context_budget_warning_tokens=value)
        with self.assertRaises(ValueError):
            ContextBudget({"generation": 0, "threshold": 100, "level": 9, "input_tokens": 100})
