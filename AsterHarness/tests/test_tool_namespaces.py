"""Native, MCP and self-authored groups share namespace registration and routing."""

import json
from pathlib import Path
import tempfile
import unittest

from agent_harness.core.client import ScriptedClient
from agent_harness.core.session import Session, SessionConfiguration
from agent_harness.core.session.step_context import StepContext
from agent_harness.core.tools.context import ToolCall
from agent_harness.core.tools.discovery import DeferredBinding
from agent_harness.core.tools.handlers import build_file_tools
from agent_harness.core.tools.registry import Tool, ToolRegistry
from agent_harness.core.tools.router import ToolRouter


def member(name="echo", result="ok", deferred=False):
    return Tool({"type": "function", "name": name, "description": "Return a test value.",
        "strict": False, "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
        lambda: result, deferred=deferred)


def call(namespace, name="echo", identifier="call"):
    return {"type": "function_call", "namespace": namespace, "name": name,
            "call_id": identifier, "arguments": "{}"}


class NamespaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_name_in_flat_and_multiple_groups_routes_exactly(self):
        registry = ToolRegistry()
        registry.register(member(result="flat"))
        registry.register_namespace("alpha", "Alpha tools", [member(result="alpha")])
        registry.register_namespace("beta", "Beta tools", [member(result="beta")])
        router = ToolRouter(registry, mcp_binding=DeferredBinding())
        self.assertEqual(router.invoke({"type": "function_call", "name": "echo", "arguments": "{}"}).output, "flat")
        self.assertEqual(router.invoke(call("alpha")).output, "alpha")
        self.assertEqual((await router.invoke_async(call("beta"))).output, "beta")
        self.assertEqual([s["name"] for s in registry.specs()], ["echo", "alpha", "beta"])
        self.assertEqual(registry.specs()[1]["description"], "Alpha tools")
        self.assertEqual(registry.specs()[1]["tools"][0]["name"], "echo")

    async def test_namespace_identities_do_not_use_ambiguous_concatenation(self):
        registry = ToolRegistry()
        registry.register(member("c", "first"), namespace="a__b")
        registry.register(member("b__c", "second"), namespace="a")
        router = ToolRouter(registry)
        self.assertEqual(router.invoke(call("a__b", "c")).output, "first")
        self.assertEqual(router.invoke(call("a", "b__c")).output, "second")
        parsed = ToolCall.from_item(call("a", "b__c"))
        self.assertEqual((parsed.namespace, parsed.name), ("a", "b__c"))

    async def test_bulk_registration_is_atomic_and_descriptions_cannot_conflict(self):
        registry = ToolRegistry()
        registry.register_namespace("alpha", "One", [member()])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            registry.register_namespace("alpha", "One", [member("new"), member()])
        with self.assertRaises(KeyError):
            registry.get_tool("new", "alpha")
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            registry.register_namespace("alpha", "Other", [member("new")])
        for namespace in ("bad.name", "x" * 65):
            with self.assertRaisesRegex(ValueError, "Namespace"):
                registry.register_namespace(namespace, "", [])

    async def test_copy_snapshot_and_explicit_legacy_alias(self):
        registry = ToolRegistry()
        registry.register_namespace("alpha", "Original", [member()])
        registry.register_alias("legacy_echo", "alpha", "echo")
        snapshot = registry.snapshot()
        registry._namespaces["alpha"]["tools"]["echo"].handler = lambda: "changed"
        registry._namespaces["alpha"]["description"] = "Changed"
        self.assertEqual(snapshot.execute("legacy_echo", {}), "ok")
        self.assertEqual(snapshot.specs()[0]["description"], "Original")
        self.assertEqual(len(snapshot.specs()), 1)
        for operation in (lambda: snapshot.register(member()),
                          lambda: snapshot.register_namespace("beta", "", []),
                          lambda: snapshot.register_alias("another", "alpha", "echo")):
            with self.assertRaisesRegex(ValueError, "snapshot"):
                operation()
        exposed = snapshot.specs()
        exposed[0]["tools"][0]["description"] = "corrupt"
        self.assertNotEqual(snapshot.specs(), exposed)

    async def test_deferred_native_member_requires_matching_search_definition(self):
        registry = ToolRegistry()
        registry.register_namespace("alpha", "Mixed group", [member("direct"), member(deferred=True)])
        self.assertEqual([t["name"] for t in registry.specs()[0]["tools"]], ["direct"])
        router = ToolRouter(registry)
        self.assertIn("missing or changed", router.invoke(call("alpha")).output)
        definition = registry.loadable_spec("echo", "alpha")["tools"][0]
        router.loaded_mcp[("alpha", "echo")] = definition
        self.assertEqual(router.invoke(call("alpha")).output, "ok")
        self.assertIn("no longer available", router.invoke(call("unknown")).output)

    async def test_native_deferred_group_uses_shared_search_worldstate_and_compaction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = build_file_tools(root)
            registry.register_namespace("native_math", "Native arithmetic tools", [member(deferred=True)])
            search = {"type": "tool_search_call", "call_id": "search", "execution": "client",
                      "arguments": {"query": "native_math echo"}}
            session = Session(ScriptedClient([[search], [call("native_math", identifier="native_call")],
                [{"type": "function_call", "name": "wait_for_event", "call_id": "wait", "arguments": "{}"}]]),
                registry, workspace=root, configuration=SessionConfiguration(max_consecutive_steps=10))
            try:
                await session.post_user_message("Discover and invoke the native group.")
                await session.run_until_idle()
                self.assertEqual(session.mcp_projection.current["namespaces"],
                                 {"native_math": "Native arithmetic tools"})
                self.assertTrue(any(i.get("call_id") == "native_call" and i.get("output") == "ok"
                                    for i in session.state.history.snapshot()))
                history = session.state.history
                session._compact_boundary = set(history.ids())
                session.compact_history(history.ids()[0], history.ids()[-1], history.generation, "Native tools loaded.")
                session.mcp_projection.validate(history.snapshot())
                prompt = StepContext.capture(session).prompt
                self.assertFalse(any(i["type"] == "tool_search_output" for i in prompt.input))
                self.assertFalse(any(t.get("name") == "native_math" for t in prompt.tools))
                self.assertIn("missing or changed", (await StepContext.capture(session).tool_router.invoke_async(
                    call("native_math", identifier="after_compact"))).output)
                session.client = ScriptedClient([[{**search, "call_id": "rediscover"}],
                    [call("native_math", identifier="after_rediscovery")],
                    [{"type": "function_call", "name": "wait_for_event", "call_id": "wait_again", "arguments": "{}"}]])
                await session.post_user_message("Rediscover before using the native group.")
                await session.run_until_idle()
                self.assertEqual((await StepContext.capture(session).tool_router.invoke_async(
                    call("native_math", identifier="after_rediscovery_probe"))).output, "ok")
            finally:
                await session.close()

    async def test_native_direct_group_uses_normal_step_results_and_ignores_mcp_search_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = build_file_tools(Path(directory))
            registry.register_namespace("functions", "Host-provided functions", [member()])
            session = Session(ScriptedClient([[call("functions", identifier="native")],
                [{"type": "function_call", "name": "wait_for_event", "call_id": "wait", "arguments": "{}"}]]),
                registry, workspace=Path(directory))
            try:
                await session.post_user_message("Use a direct native tool.")
                await session.run_until_idle()
                self.assertTrue(any(i.get("call_id") == "native" and i.get("output") == "ok"
                                    for i in session.state.history.snapshot()))
                self.assertIsNone(session.mcp_projection)
                self.assertIn("functions", [t.get("name") for t in StepContext.capture(session).prompt.tools])
            finally:
                await session.close()


class DefaultNamespaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_flat_and_explicit_functions_share_calls_and_identity(self):
        for registration in (None, '', 'functions'):
            with self.subTest(registration=registration):
                registry = ToolRegistry()
                registry.register(member(result='default'), namespace=registration)
                registry.register_namespace('alpha', 'Other', [member(result='alpha')])
                router = ToolRouter(registry)
                for spelling in (None, '', 'functions'):
                    result = await router.invoke_async(call(spelling))
                    self.assertEqual(result.output, 'default')
                    self.assertEqual(ToolCall.from_item(call(spelling)).namespace, 'functions')
                self.assertEqual(router.invoke(call('alpha')).output, 'alpha')
                self.assertIs(registry.get_tool('echo'), registry.get_tool('echo', 'functions'))
                default = [d for d in registry.all_definitions() if d['namespace']=='functions']
                self.assertEqual(len(default), 1)

    async def test_default_collisions_are_rejected_atomically_in_both_orders(self):
        registry = ToolRegistry()
        registry.register(member())
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            registry.register_namespace('functions', '', [member('new'), member()])
        with self.assertRaises(KeyError):
            registry.get_tool('new')
        self.assertEqual([t['type'] for t in registry.specs()], ['function'])
        for spelling in ('', 'functions'):
            reverse = ToolRegistry()
            reverse.register_namespace(spelling, '', [member()])
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                reverse.register(member())
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                reverse.register(member(), namespace='functions')
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                reverse.register_alias('echo', 'functions', 'echo')

    async def test_default_aliases_snapshots_and_wire_forms(self):
        registry = ToolRegistry()
        registry.register(member())
        registry.register_alias('old_echo', 'functions', 'echo')
        self.assertEqual(registry.execute('old_echo', {}, namespace='functions'), 'ok')
        frozen = registry.snapshot()
        self.assertEqual(frozen.execute('echo', {}, namespace='functions'), 'ok')
        self.assertEqual(frozen.specs()[0]['type'], 'function')
        grouped = ToolRegistry()
        grouped.register_namespace('', 'Default group', [member()])
        self.assertEqual(grouped.specs()[0]['name'], 'functions')
        self.assertEqual(grouped.specs()[0]['type'], 'namespace')

    async def test_default_deferred_member_cannot_bypass_search_by_omitting_namespace(self):
        for registration in (None, 'functions'):
            registry = ToolRegistry()
            registry.register(member(deferred=True), namespace=registration)
            router = ToolRouter(registry)
            self.assertEqual(registry.deferred_definitions()[0].namespace, 'functions')
            for spelling in (None, '', 'functions'):
                self.assertIn('missing or changed', router.invoke(call(spelling)).output)
            router.loaded_mcp[('functions','echo')] = registry.loadable_spec('echo', 'functions')['tools'][0]
            for spelling in (None, '', 'functions'):
                self.assertEqual(router.invoke(call(spelling)).output, 'ok')

    async def test_nondefault_tools_are_not_used_as_implicit_fallbacks(self):
        registry = ToolRegistry()
        registry.register_namespace('alpha', '', [member()])
        router = ToolRouter(registry)
        self.assertIn('error', router.invoke(call(None)).output)
        for invalid in (False, 3, []):
            self.assertIn('namespace must', router.invoke(call(invalid)).output)

    async def test_explicit_default_memory_call_keeps_versions_compact_and_resume(self):
        from agent_harness.core.thread_manager import ThreadManager
        from agent_harness.core.session.step import build_prompt
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            manager=ThreadManager(root/'threads')
            write={'type':'function_call','namespace':'functions','name':'write_memory','call_id':'write',
                   'arguments':json.dumps({'path':'index.md','text':'verified default namespace memory'})}
            wait={'type':'function_call','name':'wait_for_event','call_id':'wait','arguments':'{}'}
            session=manager.create(ScriptedClient([[write],[wait]]),root)
            try:
                await session.post_user_message('Save a note.')
                await session.run_until_idle()
                self.assertEqual(session.memory.current['version'], 1)
                session.memory.validate(session.state.history.snapshot(), session.agent_context)
                history=session.state.history
                session._compact_boundary=set(history.ids())
                session.compact_history(history.ids()[0],history.ids()[-1],history.generation,'Verified note saved.')
                await session.suspend()
                restored=manager.resume(ScriptedClient([]),session.thread_id)
                try:
                    build_prompt(restored)
                    self.assertEqual(restored.memory.current['text'],'verified default namespace memory')
                finally:
                    await restored.close()
            finally:
                await session.close()
