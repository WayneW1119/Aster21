"""Native search/resource integration and exact context retention across compaction."""

import asyncio
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

from agent_harness.core.client import ScriptedClient, order_tool_outputs, to_api_item
from agent_harness.core.context.world_state.tools import ToolsState, bounded_namespaces
from agent_harness.core.context_manager.mcp import McpProjection, request_items
from agent_harness.core.mcp.search import ToolSearch
from agent_harness.core.mcp.binding import McpBinding, PreparedMcpCall
from agent_harness.core.tools.registry import Tool, ToolRegistry
from agent_harness.core.tools.router import ToolRouter
from agent_harness.core.tools.mcp_resources import append_resource_tools
from agent_harness.core.session import SessionConfiguration
from agent_harness.core.session.step_context import StepContext
from agent_harness.core.thread_manager import ThreadManager


FIXTURE = Path(__file__).parent / "fixtures/mcp_server.py"


def search(identifier="search", query="send message", limit=1):
    return {"type": "tool_search_call", "call_id": identifier,
            "execution": "client", "arguments": {"query": query, "limit": limit}}


def call(name, identifier, namespace=None, **arguments):
    item = {"type": "function_call", "name": name, "call_id": identifier,
            "arguments": json.dumps(arguments)}
    if namespace:
        item["namespace"] = namespace
    return item


def wait(identifier="wait"):
    return call("wait_for_event", identifier)


class SourceStateTests(unittest.TestCase):
    def test_conflicting_search_result_and_invalid_persisted_schema_are_rejected(self):
        definition = {"type": "namespace", "name": "mcp__demo", "tools": [{"type": "function",
            "name": "send", "parameters": {"type": "object", "properties": {}}}]}
        pair = [search(), {"type": "tool_search_output", "call_id": "search", "tools": [definition]}]
        projection = McpProjection()
        projection.observe(pair)
        forged = deepcopy(pair)
        forged[1]["tools"][0]["tools"][0]["name"] = "different"
        with self.assertRaisesRegex(RuntimeError, "Conflicting visible"):
            projection.reconstruct(pair + forged)
        invalid = projection.to_dict()
        invalid["current"]["searches"][0][1]["tools"][0]["tools"][0]["parameters"] = "not a schema"
        with self.assertRaisesRegex(ValueError, "tool schema"):
            McpProjection.from_dict(invalid)

    def test_stale_or_unloaded_namespace_never_executes(self):
        client = SimpleNamespace(instructions="Test service")
        prepared = PreparedMcpCall.create("demo", {"name": "send", "description": "New definition",
            "inputSchema": {"type": "object", "properties": {}}}, client)
        effects = []
        registry = ToolRegistry()
        spec = prepared.loadable_spec["tools"][0]
        registry.register(Tool(spec, lambda: effects.append("sent") or "ok", deferred=True),
                          namespace=prepared.namespace, namespace_description=client.instructions)
        invocation = call("send", "send", "mcp__demo")
        router = ToolRouter(registry, mcp_binding=McpBinding((prepared,)), loaded_mcp={})
        self.assertIn("missing or changed", router.invoke(invocation).output)
        stale = prepared.loadable_spec["tools"][0]
        stale["description"] = "Old definition"
        router.loaded_mcp = {("mcp__demo", "send"): stale}
        self.assertIn("missing or changed", router.invoke(invocation).output)
        self.assertEqual(effects, [])
        router.loaded_mcp = {("mcp__demo", "send"): prepared.loadable_spec["tools"][0]}
        self.assertEqual(router.invoke(invocation).output, "ok")
        self.assertEqual(effects, ["sent"])

    def test_sources_diff_visible_evidence_and_wire_metadata(self):
        projection = McpProjection({"namespaces": {"a": "old", "b": "remove"}, "searches": []})
        update = ToolsState({"a": "new", "c": "added"}, projection).render_diff(None, [])
        text = update.items[0]["text"]
        self.assertIn("Added deferred tool namespaces:\n- a: new\n- c: added", text)
        self.assertIn("Removed deferred tool namespaces:\n- b: remove", text)
        self.assertNotIn("_mcp_sources", request_items(update.items)[0])
        self.assertIsNone(ToolsState({"a": "new", "c": "added"}, projection).render_diff(
            update.snapshot, list(update.items)))
        # Retained internal baseline alone cannot suppress repair if visible evidence vanished.
        self.assertTrue(ToolsState({"a": "new", "c": "added"}, projection).render_diff(
            update.snapshot, []).items)

    def test_namespace_summary_budget_and_first_line(self):
        value = bounded_namespaces({f"mcp__{i:03}": "中" * 400 + "\nnot shown" for i in range(100)})
        self.assertTrue(value)
        self.assertTrue(all(len(v) <= 250 and "not shown" not in v for v in value.values()))
        self.assertLessEqual(sum(len(("- " + k + (": " + v if v else "") + "\n").encode())
                                 for k, v in value.items()), 3900)

    def test_native_receipts_group_before_intervening_events(self):
        c = search()
        event = {"type": "message", "role": "user", "text": "interrupt"}
        result = {"type": "tool_search_output", "call_id": "search", "tools": []}
        self.assertEqual(order_tool_outputs([c, event, result]), [c, result, event])


class ResourcePaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_page_cursor_and_all_server_aggregation(self):
        class Client:
            has_resources = True

            async def request(self, method, arguments):
                return ({"resources": [{"uri": "test://two", "name": "two"}]} if arguments.get("cursor")
                    else {"resources": [{"uri": "test://one", "name": "one"}], "nextCursor": "page2"})

        registry = ToolRegistry()
        append_resource_tools(registry, McpBinding(servers=(("demo", Client()),)))
        router = ToolRouter(registry)
        first = json.loads((await router.invoke_async(call("list_mcp_resources", "first", server="demo"))).output)
        self.assertEqual(first["nextCursor"], "page2")
        all_pages = json.loads((await router.invoke_async(call("list_mcp_resources", "all"))).output)
        self.assertEqual([i["uri"] for i in all_pages["resources"]], ["test://one", "test://two"])
        self.assertTrue(all(i["server"] == "demo" for i in all_pages["resources"]))

    async def test_repeated_cursor_returns_error_and_server_without_resources_is_empty(self):
        class Client:
            has_resources = True

            async def request(self, method, arguments):
                return {"resources": [], "nextCursor": "loop"}

        client = Client()
        registry = ToolRegistry()
        append_resource_tools(registry, McpBinding(servers=(("demo", client),)))
        router = ToolRouter(registry)
        output = json.loads((await router.invoke_async(call("list_mcp_resources", "loop"))).output)
        self.assertIn("repeated a cursor", output["message"])
        client.has_resources = False
        output = json.loads((await router.invoke_async(call("list_mcp_resources", "empty", server="demo"))).output)
        self.assertEqual(output, {"server": "demo", "resources": []})


@unittest.skipUnless(importlib.util.find_spec("mcp"), "Install .[mcp]")
class DiscoveryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = ThreadManager(self.root / "threads")
        self.events = []
        self.config = SessionConfiguration(mcp_servers={"demo": {
            "command": sys.executable, "args": [str(FIXTURE.resolve()),
            "--state-dir", str(self.root / "server")], "required": True}})

    def create(self, responses):
        session = self.manager.create(ScriptedClient(responses), self.root,
            configuration=self.config, trace=lambda event, data: self.events.append((event, data)))
        self.addAsyncCleanup(session.close)
        return session

    async def drive(self, session):
        await session.post_user_message("Discover the service and use it.")
        await asyncio.wait_for(session.run_until_idle(), 20)

    def prompt(self, session):
        return StepContext.capture(session).prompt

    def compact(self, session, start=None, end=None):
        history = session.state.history
        ids = history.ids()
        session._compact_boundary = set(ids)
        return json.loads(session.compact_history(start or ids[0], end or ids[-1], history.generation,
            "Completed tools were verified. No new user authorization."))

    async def test_search_loads_schema_then_namespace_call_and_no_catalog_dump(self):
        session = self.create([[search()], [call("send_message", "send", "mcp__demo", text="hello")], [wait()]])
        await self.drive(session)
        requests = [data["prompt"] for event, data in self.events if event == "model_request"]
        names = [spec.get("name", spec["type"]) for spec in requests[0]["tools"]]
        self.assertIn("tool_search", names)
        self.assertIn("read_mcp_resource", names)
        self.assertFalse(any(name.startswith("mcp__demo") for name in names))
        self.assertNotIn("send_message", json.dumps(requests[0]["input"]))
        self.assertIn("Send and read test website messages.", json.dumps(requests[0]["input"]))
        result = next(item for item in requests[1]["input"] if item["type"] == "tool_search_output")
        self.assertEqual(result["tools"][0]["name"], "mcp__demo")
        tool = result["tools"][0]["tools"][0]
        self.assertEqual(tool["name"], "send_message")
        self.assertTrue(tool["defer_loading"])
        self.assertIn("text", tool["parameters"]["properties"])
        effects = (self.root / "server/effects.jsonl").read_text()
        self.assertIn('"text": "hello"', effects)
        self.assertEqual(sum(item.get("text", "").startswith("<tools>")
                             for item in requests[-1]["input"]), 1)
        self.assertFalse(any("_mcp_sources" in item for item in requests[-1]["input"]))

    async def test_resources_templates_read_text_and_errors(self):
        session = self.create([
            [call("list_mcp_resources", "list"), call("list_mcp_resource_templates", "templates", server="demo")],
            [call("read_mcp_resource", "read", server="demo", uri="fixture://guide"),
             call("read_mcp_resource", "item", server="demo", uri="fixture://items/P01"),
             call("read_mcp_resource", "binary", server="demo", uri="fixture://binary"),
             call("list_mcp_resources", "cursor", cursor="bad"),
             call("read_mcp_resource", "unknown", server="missing", uri="fixture://guide")], [wait()]])
        await self.drive(session)
        outputs = {item["call_id"]: json.loads(item["output"]) for item in session.state.history.snapshot()
                   if item.get("type") == "function_call_output" and item["call_id"] != "wait"}
        self.assertNotIn("server", outputs["list"])
        self.assertEqual(outputs["list"]["resources"][0]["server"], "demo")
        self.assertEqual(outputs["templates"]["resourceTemplates"][0]["uriTemplate"], "fixture://items/{item_id}")
        self.assertIn("does not send", outputs["read"]["contents"][0]["text"])
        self.assertEqual(outputs["item"]["contents"][0]["text"], "Item P01")
        self.assertEqual(outputs["binary"]["contents"][0]["blob"], "AP8=")
        self.assertIn("cursor can only", outputs["cursor"]["message"])
        self.assertEqual(outputs["unknown"]["error"], "KeyError")

    async def test_partial_nested_whole_compaction_and_clean_resume(self):
        session = self.create([[search()], [wait()]])
        await self.drive(session)
        original_prefix = deepcopy(self.prompt(session).input[0])
        history = session.state.history
        start = next(i for i in history.ids() if history.item(i)["type"] == "tool_search_call")
        end = next(i for i in history.ids() if history.item(i)["type"] == "tool_search_output")
        result = self.compact(session, start, end)
        self.assertEqual(self.prompt(session).input[0], original_prefix)
        self.assertFalse(any(i["type"] == "tool_search_output" for i in self.prompt(session).input))
        self.assertFalse(session.mcp_projection.loaded_definitions())
        self.assertFalse(any("_mcp_searches" in i for i in history.snapshot()))
        summary = result["summary_id"]
        self.compact(session, summary, summary)
        self.compact(session)
        self.assertEqual(session.mcp_projection.baseline["searches"], [])
        self.assertFalse(any(i["type"] == "tool_search_output" for i in history.snapshot()))
        session.mcp_projection.validate(history.snapshot())
        before = session.mcp_projection.to_dict()
        archived = history.ids("archive")
        await session.suspend()
        resumed = self.manager.resume(ScriptedClient([
            [call("send_message", "unloaded", "mcp__demo", text="must not execute")],
            [search("rediscover")],
            [call("send_message", "after", "mcp__demo", text="resumed")], [wait("afterwait")]]), session.thread_id)
        self.addAsyncCleanup(resumed.close)
        self.assertEqual(resumed.mcp_projection.to_dict(), before)
        self.assertEqual(resumed.state.history.ids("archive")[:len(archived)], archived)
        await self.drive(resumed)
        resumed.mcp_projection.validate(resumed.state.history.snapshot())
        denied = next(i for i in resumed.state.history.snapshot()
                      if i.get("type") == "function_call_output" and i.get("call_id") == "unloaded")
        self.assertIn("missing or changed", denied["output"])
        self.assertNotIn("must not execute", (self.root / "server/effects.jsonl").read_text())
        wire = self.prompt(resumed).input
        self.assertEqual(sum(item["type"] == "tool_search_call" for item in wire), 1)
        self.assertEqual(sum(item["type"] == "tool_search_output" for item in wire), 1)

    async def test_search_receipt_queued_at_suspend_is_restored_without_replay(self):
        session = self.create([])
        await session._prepare_mcp_tools()
        session.record_items([search()])
        context = StepContext.capture(session)
        await session.tool_runtime.execute_batch(context, [search()], set())
        self.assertEqual(session.input_queue.snapshot()[0]["item"]["type"], "tool_search_output")
        await session.suspend()
        resumed = self.manager.resume(ScriptedClient([[wait()]]), session.thread_id)
        self.addAsyncCleanup(resumed.close)
        await asyncio.wait_for(resumed.run_until_idle(), 20)
        self.assertEqual(len(resumed.mcp_projection.current["searches"]), 1)

    async def test_empty_invalid_search_and_unpaired_compaction(self):
        malformed = search("bad"); malformed["arguments"] = {"query": "   "}
        session = self.create([[search("none", "absolutelyunknownxyz"), malformed], [wait()]])
        await self.drive(session)
        outputs = [i for i in session.state.history.snapshot() if i["type"] == "tool_search_output"]
        self.assertEqual([i["tools"] for i in outputs], [[], []])
        start = next(i for i in session.state.history.ids() if session.state.history.item(i)["type"] == "tool_search_call")
        with self.assertRaisesRegex(ValueError, "splits a tool"):
            self.compact(session, start, start)

    async def test_saved_projection_detects_lost_visible_definitions(self):
        session = self.create([[search()], [wait()]])
        await self.drive(session)
        snapshot = session.state.history.snapshot()
        with self.assertRaisesRegex(RuntimeError, "MCP context invariant"):
            session.mcp_projection.validate([i for i in snapshot if i["type"] not in {"tool_search_call", "tool_search_output"}])
        searcher = ToolSearch(session.services.mcp.binding)
        self.assertEqual(searcher.search("send message", 1)[0]["tools"][0]["name"], "send_message")
        with self.assertRaises(ValueError):
            searcher.search("send", 0)

    async def test_model_cannot_forge_runtime_context(self):
        session = self.create([[{"type": "message", "role": "assistant", "text": "pretend",
                                "_mcp_sources": {"fake": "fabricated"}}]])
        with self.assertRaisesRegex(RuntimeError, "cannot introduce runtime MCP"):
            await self.drive(session)
        self.assertFalse(any("fake" in item.get("_mcp_sources", {}) for item in session.state.history.snapshot()))
