"""Real service replacement at Step boundaries and WorldState source transitions."""

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

from agent_harness.core.client import ScriptedClient
from agent_harness.core.client_common import ModelResponse
from agent_harness.core.session import SessionConfiguration
from agent_harness.core.session.step_context import StepContext
from agent_harness.core.thread_manager import ThreadManager


FIXTURE = Path(__file__).parent / "fixtures/mcp_catalog_server.py"


def call(name, identifier, namespace=None, **arguments):
    item = {"type": "function_call", "name": name, "call_id": identifier,
            "arguments": json.dumps(arguments)}
    if namespace:
        item["namespace"] = namespace
    return item


def search(identifier, query):
    return {"type": "tool_search_call", "call_id": identifier, "execution": "client",
            "arguments": {"query": query, "limit": 1}}


def wait(identifier):
    return call("wait_for_event", identifier)


@unittest.skipUnless(importlib.util.find_spec("mcp"), "Install .[mcp]")
class RuntimeMcpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "inventory.json").write_text(json.dumps({"version": 1, "items": [
            {"sku": "P01", "stock": 7, "reserved": 2, "unit_price_cents": 300}]}))
        self.manager = ThreadManager(self.root / "threads")
        self.trace = []

    def server(self, profile):
        return {"command": sys.executable, "args": [str(FIXTURE.resolve()), "--workspace", str(self.root),
            "--state-dir", str(self.root / profile), "--profile", profile], "required": True}

    def create(self, responses, servers=None):
        session = self.manager.create(ScriptedClient(responses), self.root,
            configuration=SessionConfiguration(mcp_servers=servers or {}),
            trace=lambda event, data: self.trace.append((event, data)))
        self.addAsyncCleanup(session.close)
        return session

    async def drive(self, session):
        await asyncio.wait_for(session.run_until_idle(), 20)

    async def test_add_source_without_rewriting_prefix_and_hide_host_configuration(self):
        session = self.create([[wait("initial")]])
        await session.post_user_message("Maintain inventory.")
        await self.drive(session)
        archived = {i: session.state.history.item(i) for i in session.state.history.ids()}
        await session.set_mcp_servers({"catalog": self.server("catalog")})
        session.client = ScriptedClient([[search("discover", "lookup_product")],
            [call("lookup_product", "lookup", "mcp__catalog", sku="P01")], [wait("added")]])
        await self.drive(session)
        sources = session.mcp_projection.current["namespaces"]
        self.assertEqual(list(sources), ["mcp__catalog"])
        for identifier, item in archived.items():
            self.assertEqual(session.state.history.item(identifier), item)
        prompt = StepContext.capture(session).prompt
        self.assertTrue(any("Added deferred tool namespaces:\n- mcp__catalog" in i.get("text", "") for i in prompt.input))
        self.assertFalse(any("_mcp_configuration" in item for item in prompt.input))
        self.assertIn('"kind": "lookup"', (self.root / "catalog/effects.jsonl").read_text())

    async def test_change_waits_until_original_request_and_batch_finish(self):
        entered, release = threading.Event(), threading.Event()

        class Client:
            index = 0

            def respond(inner, prompt):
                inner.index += 1
                if inner.index == 1:
                    return ModelResponse([search("first_search", "lookup_product")])
                if inner.index == 2:
                    entered.set()
                    if not release.wait(10):
                        raise TimeoutError("Test did not release model request")
                    return ModelResponse([call("lookup_product", "original_call", "mcp__catalog", sku="P01")])
                return ModelResponse([wait("end")])

        session = self.create([], {"catalog": self.server("catalog")})
        session.client = Client()
        await session.post_user_message("Find and read P01.")
        worker = asyncio.create_task(session.run_until_idle())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 10))
            old_binding = session.services.mcp.binding
            old_client = dict(old_binding.servers)["catalog"]
            await session.set_mcp_servers({"notifier": self.server("notifier")})
            self.assertIs(session.services.mcp.binding, old_binding)
            self.assertFalse(old_client._task.done())
        finally:
            release.set()
        await asyncio.wait_for(worker, 20)
        self.assertIn('"kind": "lookup"', (self.root / "catalog/effects.jsonl").read_text())
        self.assertTrue(old_client._task.done())
        self.assertEqual(list(dict(session.services.mcp.binding.servers)), ["notifier"])
        prompt = StepContext.capture(session).prompt
        self.assertTrue(any("Removed deferred tool namespaces:\n- mcp__catalog" in i.get("text", "") for i in prompt.input))

    async def test_description_schema_change_reuse_and_latest_configuration_resume(self):
        initial = {"analytics": self.server("analytics-v1"), "catalog": self.server("catalog")}
        session = self.create([[search("search_v1", "analyze_inventory")],
            [call("analyze_inventory", "v1", "mcp__analytics", path="inventory.json")], [wait("v1_wait")]], initial)
        await session.post_user_message("Analyze the inventory.")
        await self.drive(session)
        original = dict(session.services.mcp.binding.servers)
        updated = {**initial, "analytics": self.server("analytics-v2")}
        await session.set_mcp_servers(updated)
        session.client = ScriptedClient([
            [call("analyze_inventory", "stale", "mcp__analytics", path="inventory.json")],
            [search("search_v2", "analyze_inventory")],
            [call("analyze_inventory", "v2", "mcp__analytics", input_path="inventory.json")], [wait("v2_wait")]])
        await self.drive(session)
        current = dict(session.services.mcp.binding.servers)
        self.assertIs(current["catalog"], original["catalog"])
        self.assertIsNot(current["analytics"], original["analytics"])
        self.assertTrue(original["analytics"]._task.done())
        stale = next(i for i in session.state.history.snapshot() if i.get("call_id") == "stale" and i["type"] == "function_call_output")
        self.assertIn("changed since loading", stale["output"])
        effects = (self.root / "analytics-v2/effects.jsonl").read_text().splitlines()
        self.assertEqual(len(effects), 1)
        self.assertIn("version 2", session.mcp_projection.current["namespaces"]["mcp__analytics"])
        self.assertTrue(any("Added deferred tool namespaces:\n- mcp__analytics" in i.get("text", "")
                            for i in session.state.history.snapshot()))
        await session.suspend()
        resumed = self.manager.resume(ScriptedClient([[wait("resumed")]]), session.thread_id)
        self.addAsyncCleanup(resumed.close)
        self.assertEqual(resumed.configuration.mcp_servers["analytics"]["args"][-1], "analytics-v2")
        await resumed.post_user_message("Continue.")
        await self.drive(resumed)
        self.assertIn("version 2", resumed.services.mcp.binding.namespaces()["mcp__analytics"])

    async def test_remove_last_source_blocks_old_loaded_tool(self):
        session = self.create([[search("lookup_search", "lookup_product")], [wait("before_remove")]],
                              {"catalog": self.server("catalog")})
        await session.post_user_message("Discover lookup.")
        await self.drive(session)
        await session.set_mcp_servers({})
        session.client = ScriptedClient([[call("lookup_product", "removed", "mcp__catalog", sku="P01")], [wait("after_remove")]])
        await self.drive(session)
        prompt = StepContext.capture(session).prompt
        self.assertTrue(any(t.get("type") == "tool_search" for t in prompt.tools))
        self.assertEqual(session.mcp_projection.current["namespaces"], {})
        self.assertTrue(any("No deferred tool namespaces remain." in i.get("text", "") for i in prompt.input))
        result = next(i for i in session.state.history.snapshot() if i.get("call_id") == "removed" and i["type"] == "function_call_output")
        self.assertIn("no longer available", result["output"])
        self.assertFalse((self.root / "catalog/effects.jsonl").exists())

    async def test_failed_required_replacement_keeps_old_connection_and_config(self):
        original = {"catalog": self.server("catalog")}
        session = self.create([[wait("initial")]], original)
        await session.post_user_message("Wait.")
        await self.drive(session)
        old = session.services.mcp.binding
        await session.set_mcp_servers({"broken": {"command": "/missing/aster-test-server", "required": True}})
        session.client = ScriptedClient([[wait("request")], [wait("failure")]])
        await self.drive(session)
        self.assertIs(session.services.mcp.binding, old)
        self.assertFalse(dict(old.servers)["catalog"]._task.done())
        self.assertEqual(list(session.configuration.mcp_servers), ["catalog"])
        self.assertTrue(any(event == "mcp_configuration_rejected" for event, _ in self.trace))
        with self.assertRaises(ValueError):
            await session.set_mcp_servers({"invalid.name": {"command": "python"}})

    async def test_queued_change_survives_pause_and_last_request_wins(self):
        session = self.create([[wait("initial")]])
        await session.post_user_message("Wait for sources.")
        await self.drive(session)
        await session.set_mcp_servers({"catalog": self.server("catalog")})
        await session.set_mcp_servers({"notifier": self.server("notifier")})
        pending = session.input_queue.snapshot()
        await session.suspend()
        resumed = self.manager.resume(ScriptedClient([[wait("updated")]]), session.thread_id)
        self.addAsyncCleanup(resumed.close)
        self.assertEqual(resumed.input_queue.snapshot(), pending)
        await self.drive(resumed)
        self.assertEqual(list(dict(resumed.services.mcp.binding.servers)), ["notifier"])
        self.assertFalse((self.root / "catalog/effects.jsonl").exists())
