"""Real V8 composition and its event-driven boundaries; no model credentials."""

import asyncio
import os
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest

from agent_harness.core.client import ScriptedClient
from agent_harness.core.client_common import ModelResponse
from agent_harness.core.session import Session, SessionConfiguration
from agent_harness.core.session.step_context import StepContext
from agent_harness.core.thread_manager import ThreadManager
from agent_harness.core.tools.handlers import build_file_tools
from agent_harness.core.tools.registry import Tool

AVAILABLE = bool(importlib.util.find_spec('py_mini_racer'))


def call(name, identifier, namespace='functions', **args):
    result = {'type':'function_call','name':name,'call_id':identifier,
              'arguments':json.dumps(args, ensure_ascii=False)}
    if namespace:
        result['namespace'] = namespace
    return result


def metadata(receipt):
    return json.loads(receipt['output'][0]['text'])


class CodeModeConfigurationTests(unittest.TestCase):
    def test_opt_in_is_strict_and_no_runtime_dependency_is_loaded_by_core(self):
        self.assertFalse(SessionConfiguration().code_mode_enabled)
        for value in (1, 'yes', None):
            with self.assertRaises(ValueError):
                SessionConfiguration(code_mode_enabled=value)


@unittest.skipUnless(AVAILABLE, 'Install .[code-mode] for real V8 tests')
class CodeModeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.events = []
        self.count = 0

    def session(self, persistent=False, client=None, budget=10000):
        config = SessionConfiguration(code_mode_enabled=True, tool_output_token_limit=budget)
        client = client or ScriptedClient([])
        if persistent:
            session = ThreadManager(self.root / 'threads').create(client, self.root,
                configuration=config, trace=lambda e,d:self.events.append((e,d)))
        else:
            registry = build_file_tools(self.root)
            async def pause(ms, value='done'):
                await asyncio.sleep(ms/1000)
                return value
            registry.register(Tool({'type':'function','name':'pause','description':'Test asynchronous operation.',
                'parameters':{'type':'object','properties':{'ms':{'type':'integer'},'value':{'type':'string'}},
                              'required':['ms']}}, pause))
            registry.register_namespace('native', 'Host functions', [Tool({'type':'function','name':'echo',
                'description':'Echo a value.', 'parameters':{'type':'object','properties':{'value':{'type':'string'}}}},
                lambda value: value)])
            session = Session(client, registry, workspace=self.root, configuration=config,
                              trace=lambda e,d:self.events.append((e,d)))
        self.addAsyncCleanup(session.close)
        return session

    async def invoke(self, session, name='exec', **args):
        await session._prepare_mcp_tools()
        self.count += 1
        item = call(name, f'outer_{self.count}', **args)
        session.record_items([item])
        context = StepContext.capture(session)
        await session.tool_runtime.execute_batch(context, [item], set(session.state.history.ids()))
        batch = await session.input_queue.drain(wait=False)
        session.record_items([event.item for event in batch])
        result = next(event.item for event in batch if event.item.get('call_id') == item['call_id'])
        self.assertTrue(any(event.kind == 'tool_result' for event in batch))
        return result

    async def test_sequential_and_parallel_calls_filter_results_and_have_no_synthetic_history(self):
        session = self.session()
        result = await self.invoke(session, code='''
const values = await Promise.all([tools.pause({ms:30,value:'A'}), tools.native__echo({value:'B'})]);
text(values.map(v=>v.output).join(','));
''', yield_time_ms=1000)
        self.assertEqual(metadata(result)['status'], 'completed')
        self.assertEqual(result['output'][1]['text'], 'A,B')
        self.assertEqual(metadata(result)['nested_calls'], 2)
        history = session.state.history.snapshot()
        self.assertEqual(sum(i['type']=='function_call' for i in history), 1)
        nested = [d for e,d in self.events if e=='code_mode_nested_result']
        self.assertEqual(len(nested), 2)
        self.assertEqual({d['output'] for d in nested}, {'A','B'})

    async def test_store_is_json_data_across_fresh_isolates_and_not_model_memory(self):
        session = self.session()
        first = await self.invoke(session, code='globalThis.localVariable=99;store("work",{n:3});')
        self.assertEqual(metadata(first)['status'], 'completed')
        second = await self.invoke(session, code='text(typeof localVariable);text(load("work"));')
        self.assertEqual(second['output'][1]['text'], 'undefined')
        self.assertEqual(json.loads(second['output'][2]['text']), {'n':3})
        self.assertIsNone(session.memory)

    async def test_long_cell_completion_is_a_generator_event_and_wait_reads_only_new_output(self):
        session = self.session()
        first = await self.invoke(session, code='text("before");await tools.pause({ms:150});text("after");',
                                  yield_time_ms=0)
        meta = metadata(first)
        self.assertEqual(meta['status'], 'running')
        cell = session.services.code_mode.cells[meta['cell_id']]
        await asyncio.wait_for(cell.done.wait(), 3)
        for _ in range(30):
            if any(e=='generator_fired' and d['generated_event']['kind']=='code_execution_completed'
                   for e,d in self.events):
                break
            await asyncio.sleep(.01)
        queued = session.input_queue.snapshot()
        self.assertTrue(any(e['kind']=='code_execution_completed' for e in queued))
        second = await self.invoke(session, name='wait', cell_id=cell.cell_id)
        self.assertEqual(metadata(second)['status'], 'completed')
        texts = [p['text'] for p in first['output'][1:] + second['output'][1:]]
        self.assertEqual(texts, ['before','after'])
        third = await self.invoke(session, name='wait', cell_id=cell.cell_id)
        self.assertEqual(len(third['output']), 1)
        self.assertEqual(sum(e=='generator_fired' and d['generated_event']['kind']=='code_execution_completed'
                             for e,d in self.events), 1)

    async def test_private_memory_edits_and_harness_control_are_not_exposed(self):
        session = self.session(persistent=True)
        original = session.agent_context.read_memory('index.md')
        result = await self.invoke(session, code='text(ALL_TOOLS.filter(x=>/write_memory|apply_memory_patch|compact_history|manage_custom_tool|tool_search/.test(x.name)));')
        self.assertEqual(json.loads(result['output'][1]['text']), [])
        result = await self.invoke(session, code='await tools.write_memory({path:"index.md",text:"forbidden"});')
        self.assertEqual(metadata(result)['status'], 'failed')
        self.assertEqual(session.agent_context.read_memory('index.md'), original)
        # Normal direct memory tools still use their existing versioned event path.
        direct = call('write_memory','direct-memory',namespace=None,path='index.md',text='authorized direct edit')
        session.record_items([direct])
        await session.tool_runtime.execute_batch(StepContext.capture(session),[direct],set())
        batch = await session.input_queue.drain(wait=False)
        session.record_items([e.item for e in batch])
        self.assertEqual(session.memory.current['text'], 'authorized direct edit')
        self.assertIn('<memory_update>', batch[0].item['output'])
        StepContext.capture(session)

    async def test_isolate_has_no_host_globals_and_infinite_script_is_stopped(self):
        session = self.session()
        result = await self.invoke(session, code='text([typeof process,typeof require,typeof fetch,typeof console]);')
        self.assertEqual(json.loads(result['output'][1]['text']), ['undefined']*4)
        started = time.monotonic()
        result = await self.invoke(session, code='while(true){}',yield_time_ms=2000,timeout_ms=3000)
        self.assertEqual(metadata(result)['status'], 'failed')
        self.assertLess(time.monotonic()-started, 3)
        result = await self.invoke(session, code='text(21*2);')
        self.assertEqual(result['output'][1]['text'], '42')

    async def test_cancel_during_nested_tool_does_not_replay_it(self):
        session = self.session()
        result = await self.invoke(session, code='await tools.pause({ms:2000});text("not reached");',yield_time_ms=0)
        ident = metadata(result)['cell_id']
        for _ in range(50):
            if session.services.code_mode.cells[ident].calls:
                break
            await asyncio.sleep(.01)
        cancelled = await self.invoke(session, name='wait',cell_id=ident,terminate=True)
        self.assertEqual(metadata(cancelled)['status'], 'cancelled')
        self.assertEqual(sum(e=='code_mode_nested_call' for e,d in self.events), 1)
        self.assertTrue(any(e=='code_mode_nested_cancelled' and d['outcome_unknown'] for e,d in self.events))

    async def test_large_output_uses_unified_budget_without_losing_cell_identity(self):
        session = self.session(budget=256)
        result = await self.invoke(session, code='text("x".repeat(30000));',yield_time_ms=1000)
        text = result['output'][0]['text']
        envelope = json.loads(text)
        self.assertTrue(envelope['tool_output_budget']['truncated'])
        self.assertEqual(envelope['code_execution']['status'], 'completed')
        self.assertIn(envelope['code_execution']['cell_id'], session.services.code_mode.cells)
        self.assertLessEqual(len(text.encode()),1024)
        artifact = json.loads(session.tool_outputs.read(envelope['tool_output_budget']['output_id'],token_limit=None))
        self.assertIn('x'*100, artifact['content'])

    async def test_image_output_is_native_and_uses_existing_view_image(self):
        from PIL import Image
        Image.new('RGB',(20,10),'red').save(self.root/'image.png')
        session = self.session()
        result = await self.invoke(session, code='const r=await tools.view_image({path:"image.png"});image(r);')
        self.assertEqual(metadata(result)['status'], 'completed')
        self.assertEqual(result['output'][1]['type'], 'input_image')
        self.assertTrue(result['output'][1]['image_url'].startswith('data:image/png;base64,'))

    async def test_suspend_does_not_resume_cells_or_temporary_store(self):
        session = self.session(persistent=True)
        result = await self.invoke(session, code='store("x",1);await new Promise(r=>setTimeout(r,2000));',yield_time_ms=0)
        ident = metadata(result)['cell_id']
        await asyncio.sleep(.05)
        await session.suspend()
        self.assertTrue(session.services.code_mode.cells[ident].task.done())
        manager = ThreadManager(self.root/'threads')
        resumed = manager.resume(ScriptedClient([[call('wait_for_event','idle',namespace=None)]]),session.thread_id)
        self.addAsyncCleanup(resumed.close)
        self.assertEqual(resumed.services.code_mode.store,{})
        self.assertEqual(resumed.services.code_mode.cells,{})
        await resumed.run_until_idle()
        self.assertTrue(any('generator_unavailable' in i.get('text','') and ident in i.get('text','')
                            for i in resumed.state.history.snapshot()))

    async def test_unawaited_nested_calls_fail_instead_of_claiming_completion(self):
        session=self.session()
        result=await self.invoke(session,code='tools.pause({ms:2000});text("launched");')
        self.assertEqual(metadata(result)['status'],'failed')
        self.assertTrue(metadata(result)['outcome_unknown'])
        self.assertIn('unawaited',metadata(result)['error'])

    async def test_namespace_collisions_do_not_route_to_another_tool(self):
        registry=build_file_tools(self.root)
        spec=lambda name,description:{'type':'function','name':name,'description':description,
            'parameters':{'type':'object','properties':{}}}
        registry.register(Tool(spec('a__b__c','flat collision'),lambda:'flat'))
        registry.register_namespace('a__b','first',[Tool(spec('c','first collision'),lambda:'first')])
        registry.register_namespace('a','second',[Tool(spec('b__c','second collision'),lambda:'second')])
        registry.register_namespace('functions','Existing host functions',
            [Tool(spec('echo','ordinary functions member'),lambda:'host')])
        session=Session(ScriptedClient([]),registry,workspace=self.root,
                        configuration=SessionConfiguration(code_mode_enabled=True))
        self.addAsyncCleanup(session.close)
        result=await self.invoke(session,code="""
const names=ALL_TOOLS.filter(t=>t.description.includes('collision') || t.description==='ordinary functions member');
text(await Promise.all(names.map(async t=>(await tools[t.name]({})).output)));
""")
        self.assertEqual(metadata(result)['status'],'completed',result)
        self.assertEqual(set(json.loads(result['output'][1]['text'])),{'flat','first','second','host'})

    @unittest.skipUnless(importlib.util.find_spec('mcp'),'optional MCP SDK not installed')
    async def test_real_mcp_calls_require_current_history_definitions_after_compact(self):
        config=SessionConfiguration(code_mode_enabled=True,mcp_servers={'demo':{
            'command':__import__('sys').executable,
            'args':[str(Path(__file__).parent/'fixtures/mcp_server.py'),'--state-dir',str(self.root/'server')],
            'required':True}})
        search={'type':'tool_search_call','call_id':'discover','execution':'client',
                'arguments':{'query':'mcp__demo send_message','limit':1}}
        code='const r=await tools.mcp__demo__send_message({text:"nested message"});text(r.output);'
        client=ScriptedClient([[search],[call('exec','composed',code=code)],
                              [call('wait_for_event','idle',namespace=None)]])
        manager=ThreadManager(self.root/'threads')
        session=manager.create(client,self.root,configuration=config)
        self.addAsyncCleanup(session.close)
        await session.post_user_message('Discover and invoke the fixture through JS.')
        await session.run_until_idle()
        effects=(self.root/'server/effects.jsonl').read_text()
        self.assertIn('nested message',effects)
        history=session.state.history
        session._compact_boundary=set(history.ids())
        session.compact_history(history.ids()[0],history.ids()[-1],history.generation,'The demo source is available.')
        denied=await self.invoke(session,code=code)
        self.assertIn('missing or changed',denied['output'][1]['text'])
        self.assertEqual((self.root/'server/effects.jsonl').read_text(),effects)
        item={**search,'call_id':'rediscover'}
        session.record_items([item])
        await session.tool_runtime.execute_batch(StepContext.capture(session),[item],set())
        batch=await session.input_queue.drain(wait=False)
        session.record_items([event.item for event in batch])
        allowed=await self.invoke(session,code=code)
        self.assertNotIn('missing or changed',allowed['output'][1]['text'])
        self.assertEqual((self.root/'server/effects.jsonl').read_text().count('nested message'),2)
        # A cell started while definitions existed cannot keep using a captured
        # loaded set after another Step compacts those definitions away.
        delayed=await self.invoke(session,code='await new Promise(r=>setTimeout(r,200));'+code,
                                  yield_time_ms=0)
        ident=metadata(delayed)['cell_id']
        session._compact_boundary=set(history.ids())
        session.compact_history(history.ids()[0],history.ids()[-1],history.generation,'Definitions removed while JS waits.')
        collected=await self.invoke(session,name='wait',cell_id=ident,yield_time_ms=1000)
        self.assertIn('missing or changed',collected['output'][1]['text'])
        self.assertEqual((self.root/'server/effects.jsonl').read_text().count('nested message'),2)

    async def test_wall_timeout_reports_possible_effects_and_stops_worker(self):
        session=self.session()
        result=await self.invoke(session,code='await tools.pause({ms:2000});',timeout_ms=800 if os.name == 'nt' else 200,yield_time_ms=1000)
        self.assertEqual(metadata(result)['status'],'failed')
        self.assertTrue(metadata(result)['outcome_unknown'])
        self.assertIn('TimeoutError',metadata(result)['error'])
        cell=session.services.code_mode.cells[metadata(result)['cell_id']]
        self.assertIsNotNone(cell.process.returncode)
        self.assertIsNone(cell.invocation)

    async def test_live_scheduler_waits_for_completion_then_model_collects_result(self):
        class Client:
            def __init__(self):self.prompts=[];self.cell=None
            def respond(inner,prompt):
                inner.prompts.append(prompt)
                for item in prompt.input:
                    if item.get('type')=='function_call_output' and isinstance(item.get('output'),list):
                        data=json.loads(item['output'][0]['text'])
                        inner.cell=data.get('cell_id',inner.cell)
                text=json.dumps(prompt.input,ensure_ascii=False)
                if len(inner.prompts)==1:
                    return ModelResponse([call('exec','start',code='await tools.pause({ms:150});text("ready");',yield_time_ms=0)])
                if 'code_execution_completed' in text and not any(i.get('call_id')=='collect' for i in prompt.input):
                    return ModelResponse([call('wait','collect',cell_id=inner.cell)])
                if any(i.get('call_id')=='collect' and i['type']=='function_call_output' for i in prompt.input):
                    return ModelResponse([call('send_user_message','sent',namespace=None,text='ready',wake_model=False)])
                return ModelResponse([call('wait_for_event','idle',namespace=None)])
        client=Client();session=self.session(client=client)
        await session.post_user_message('Run a composed task and keep accepting events.')
        await asyncio.wait_for(session.run_until_settled(),5)
        self.assertEqual(session.state.sent_messages,['ready'])
        self.assertGreaterEqual(len(client.prompts),3)

    async def test_omitted_exec_namespace_and_default_js_names_work_without_memory_bypass(self):
        client = ScriptedClient([[call('exec','plain-exec',namespace=None,
            code='text(await tools.read_file({path:"sample.txt",start_line:1,end_line:1}));text(ALL_TOOLS.some(t=>t.name==="write_memory"));')],
            [call('wait_for_event','quiet',namespace=None)]])
        (self.root/'sample.txt').write_text('default call works')
        session = self.session(client=client)
        await session.post_user_message('Run the default exec tool.')
        await session.run_until_idle()
        result = next(i for i in session.state.history.snapshot() if i.get('call_id')=='plain-exec' and i['type']=='function_call_output')
        self.assertEqual(metadata(result)['status'], 'completed')
        self.assertIn('default call works',result['output'][1]['text'])
        self.assertEqual(result['output'][2]['text'], 'false')
