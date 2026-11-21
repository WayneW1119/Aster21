"""World-state rendering, persistence and step-boundary evidence regressions."""

from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import unittest
from uuid import uuid4

from agent_harness.core.context.world_state import WorldState, WorldStateSnapshot, WorldStateUpdate
from agent_harness.core.context.world_state.memory import MemoryState
from agent_harness.core.context_manager.agent_context import AgentContext, DEFAULT_MEMORY
from agent_harness.core.rollout import RolloutRecorder
from agent_harness.core.session import SessionConfiguration
from agent_harness.core.session.step import run_step
from agent_harness.core.thread_manager import ThreadManager
from agent_harness.core.trace import JsonlTrace
from agent_harness.render_trace import render
from test_memory_context import RecordingClient, call, memory_patch, memory_items

ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = None


def setUpModule():
    global RUN_ROOT
    RUN_ROOT = ROOT/'test_runs'/(datetime.now().strftime('%Y%m%d-%H%M%S')+'-world-state-'+uuid4().hex[:8])
    RUN_ROOT.mkdir(parents=True)
    print(f'World-state regression artifacts: {RUN_ROOT}', flush=True)


def tearDownModule():
    for path in RUN_ROOT.glob('*/trace.jsonl'):
        path.with_suffix('.md').write_text(render(path), encoding='utf-8')


class ExampleSection:
    def __init__(self, identifier, value):
        self.ID, self.value = identifier, value

    def render_diff(self, previous, items):
        current = {'value': self.value}
        if previous == current:
            return None
        item = {'type':'message','role':'user','text':f'{self.ID}: {self.value}'}
        return WorldStateUpdate(self.ID, current, (item,))


class WorldStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = RUN_ROOT/self._testMethodName
        self.workspace = self.root/'workspace'
        self.workspace.mkdir(parents=True)
        self.manager = ThreadManager(self.root/'threads')
        self.trace = JsonlTrace(self.root/'trace.jsonl')

    async def drive(self, session):
        try:
            await session.post_user_message('Run the world-state check.')
            await session.run_until_settled()
        finally:
            await session.close()

    def test_sections_render_independently_and_preserve_previous_snapshot(self):
        world = WorldState()
        first, second = ExampleSection('first',1), ExampleSection('second',2)
        world.add_section(first)
        world.add_section(second)
        snapshot, updates = world.render_full()
        self.assertEqual([u.section_id for u in updates], ['first','second'])
        items = [item for update in updates for item in update.items]
        unchanged, updates = world.render_history_diff(snapshot, items)
        self.assertEqual(unchanged,snapshot)
        self.assertEqual(updates,[])
        second.value=3
        changed, updates = world.render_history_diff(snapshot,items)
        self.assertEqual([u.section_id for u in updates],['second'])
        self.assertEqual(snapshot.sections['second'],{'value':2})
        self.assertEqual(changed.sections['second'],{'value':3})
        exported=changed.to_dict()
        restored=WorldStateSnapshot.from_dict(exported)
        exported['sections']['second']['value']=99
        self.assertEqual(restored,changed)
        with self.assertRaises(ValueError):
            world.add_section(ExampleSection('second',4))

    def test_memory_full_render_is_explicit_and_disk_edits_do_not_refresh_history(self):
        (self.root/'context').mkdir()
        context=AgentContext(self.root/'context')
        context.initialize()
        world=WorldState()
        world.add_section(MemoryState(context))
        old, updates=world.render_full()
        items=list(updates[0].items)
        context.write_memory('index.md','External new contents')
        retained, changes=world.render_history_diff(old,items)
        self.assertEqual(retained,old)
        self.assertEqual(changes,[])
        refreshed, updates=world.render_full()
        self.assertNotEqual(refreshed,old)
        self.assertIn('External new contents',updates[0].items[0]['text'])
        self.assertIn(DEFAULT_MEMORY,items[0]['text'])

    async def test_tool_receipts_advance_metadata_without_duplicate_model_messages(self):
        client=RecordingClient([
            [call('write_memory',path='index.md',text='state-one\n')],
            [call('apply_memory_patch',patch=memory_patch('*** Update File: index.md','@@','-state-one','+state-two'))],
            [call('send_user_message',text='done', wake_model=False)],
        ])
        session=self.manager.create(client,self.workspace,trace=self.trace)
        await self.drive(session)
        events=[json.loads(line) for line in (self.root/'trace.jsonl').read_text().splitlines()]
        requests=[e for e in events if e['event']=='model_request']
        self.assertEqual([e['world_state']['sections']['memory']['observations'] for e in requests],[0,1,2])
        self.assertTrue(all(len(memory_items(prompt.input))==0 and prompt.memory for prompt in client.prompts))
        for previous,current in zip(client.prompts,client.prompts[1:]):
            self.assertEqual(current.input[:len(previous.input)],previous.input)
        for item in client.prompts[-1].input:
            self.assertNotIn('world_state',item)
            self.assertNotIn('sections',item)
        rows=[json.loads(line) for line in session.rollout.path.read_text().splitlines()]
        snapshots=[WorldStateSnapshot.from_dict(r['payload']) for r in rows if r['type']=='world_state']
        self.assertEqual(len(snapshots),3)
        self.assertEqual(snapshots[-1],session.state.world_state_baseline)
        self.assertEqual(session.agent_context.read_memory('index.md'),'state-two\n')

    async def test_pending_receipt_advances_baseline_only_when_consumed_after_resume(self):
        first=RecordingClient([[call('write_memory',path='index.md',text='pending-write')]])
        session=self.manager.create(first,self.workspace,trace=self.trace)
        await session.post_user_message('Write then suspend before consuming the receipt.')
        batch=await session.input_queue.drain(wait=False)
        await run_step(session,batch)
        self.assertEqual(session.state.world_state_baseline.sections['memory']['observations'],0)
        self.assertEqual(len(session.input_queue.snapshot()),1)
        original_baseline=session.state.world_state_baseline
        await session.close()
        client=RecordingClient([[call('send_user_message',text='receipt-observed', wake_model=False)]])
        resumed=self.manager.resume(client,session.thread_id,trace=self.trace)
        self.assertEqual(resumed.state.world_state_baseline,original_baseline)
        try:
            await resumed.run_until_settled()
        finally:
            await resumed.close()
        self.assertEqual(resumed.state.world_state_baseline.sections['memory']['observations'],1)
        self.assertEqual(len(first.prompts),1)
        self.assertEqual(len(client.prompts),1)
        self.assertEqual(resumed.agent_context.read_memory('index.md'),'pending-write')
        self.assertEqual(len(memory_items(client.prompts[0].input)),0)
        self.assertIsNotNone(client.prompts[0].memory)

    async def test_resume_reconstructs_legacy_baseline_without_reinjecting_memory(self):
        recorder, meta=RolloutRecorder.create(self.root/'threads',self.workspace,asdict(SessionConfiguration()))
        context=AgentContext(recorder.path.parent)
        context.initialize()
        initial=context.memory_item()
        context.write_memory('index.md','legacy-written-value')
        write=call('write_memory',path='index.md',text='legacy-written-value')
        recorder.append('session_opened',{'session_id':'legacy'})
        for item in (initial,write,{'type':'function_call_output','call_id':write['call_id'],'output':'Memory saved.'}):
            recorder.append('response_item',item)
        recorder.append('session_closed',{'session_id':'legacy','step_number':1,'clean':True})
        recorder.close()
        client=RecordingClient([[call('send_user_message',text='restored', wake_model=False)]])
        resumed=self.manager.resume(client,meta['thread_id'],trace=self.trace)
        self.assertEqual(resumed.state.world_state_baseline.sections['memory']['observations'],1)
        await self.drive(resumed)
        self.assertEqual(memory_items(client.prompts[0].input),[])
        self.assertIn('legacy-written-value', client.prompts[0].memory['text'])
        rows=[json.loads(line) for line in recorder.path.read_text().splitlines()]
        self.assertEqual(sum(r['type']=='world_state' for r in rows),1)

    async def test_error_and_partial_patch_evidence_does_not_claim_current_disk_snapshot(self):
        partial=memory_patch('*** Add File: topics/committed.md','+kept',
                             '*** Update File: index.md','@@','-missing','+bad')
        client=RecordingClient([
            [call('write_memory',path='../outside.md',text='rejected'),call('apply_memory_patch',patch=partial)],
            [call('send_user_message',text='errors-observed', wake_model=False)],
        ])
        session=self.manager.create(client,self.workspace,trace=self.trace)
        await self.drive(session)
        memory=session.state.world_state_baseline.sections['memory']
        self.assertEqual(memory['observations'],2)
        self.assertEqual(session.agent_context.read_memory('index.md'),DEFAULT_MEMORY)
        self.assertEqual(session.agent_context.read_memory('topics/committed.md'),'kept\n')
        self.assertEqual(len(memory_items(client.prompts[-1].input)),0)
        self.assertIsNotNone(client.prompts[-1].memory)
        self.assertEqual(set(memory),{'initial_hash','evidence_hash','observations'})

    async def test_invalid_saved_snapshot_is_rejected_without_mutating_rollout(self):
        session=self.manager.create(RecordingClient([]),self.workspace,trace=self.trace)
        await session.close()
        original=session.rollout.path.read_text()
        rows=[json.loads(line) for line in original.splitlines()]
        for row in rows:
            if row['type']=='world_state':
                row['payload']={'version':2,'sections':{}}
        invalid='\n'.join(json.dumps(row) for row in rows)+'\n'
        (self.root/'invalid-rollout.jsonl').write_text(invalid)
        with self.assertRaisesRegex(ValueError,'world-state snapshot'):
            RolloutRecorder._read(invalid,session.thread_id)
        self.assertEqual(session.rollout.path.read_text(),original)

    async def test_timer_still_wakes_idle_session_without_memory_updates(self):
        client=RecordingClient([
            [call('create_timer',delay_ms=150,message='world-state-timer')],
            [call('send_user_message',text='waiting', wake_model=False)],
            [call('send_user_message',text='timer-observed', wake_model=False)],
        ])
        session=self.manager.create(client,self.workspace,trace=self.trace)
        await self.drive(session)
        self.assertEqual(len(client.prompts),3)
        self.assertIn('timer_fired',json.dumps(client.prompts[-1].input))
        self.assertEqual(session.state.world_state_baseline.sections['memory']['observations'],0)
        rows=[json.loads(line) for line in session.rollout.path.read_text().splitlines()]
        states = [r['payload']['sections'] for r in rows if r['type'] == 'world_state']
        self.assertEqual(len(states), 3)
        self.assertEqual([s['memory'] for s in states], [states[0]['memory']] * 3)
        self.assertEqual(states[-1]['generators']['registrations'], [])


if __name__=='__main__':
    unittest.main(verbosity=2)
