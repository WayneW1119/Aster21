"""Initial memory, append-only tool history and durable resume regressions."""

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import unittest
from unittest.mock import patch as mock_patch
from uuid import uuid4

from agent_harness.core.client import ScriptedClient
from agent_harness.core.context_manager.agent_context import AgentContext, DEFAULT_MEMORY
from agent_harness.core.rollout import RolloutRecorder
from agent_harness.core.session import SessionConfiguration
from agent_harness.core.thread_manager import ThreadManager
from agent_harness.core.trace import JsonlTrace
from agent_harness.render_trace import render

ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = None


def setUpModule():
    global RUN_ROOT
    RUN_ROOT = ROOT/'test_runs'/(datetime.now().strftime('%Y%m%d-%H%M%S')+'-memory-regression-'+uuid4().hex[:8])
    RUN_ROOT.mkdir(parents=True)
    print(f'Memory regression artifacts: {RUN_ROOT}', flush=True)


def tearDownModule():
    for path in RUN_ROOT.glob('*/trace.jsonl'):
        path.with_suffix('.md').write_text(render(path), encoding='utf-8')


def call(name, **arguments):
    return {'type': 'function_call', 'call_id': 'test_'+uuid4().hex,
            'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}


def memory_patch(*lines):
    return '\n'.join(('*** Begin Patch', *lines, '*** End Patch'))


class RecordingClient(ScriptedClient):
    def __init__(self, responses):
        super().__init__(responses)
        self.prompts = []

    def respond(self, prompt):
        self.prompts.append(deepcopy(prompt))
        return super().respond(prompt)


def memory_items(items):
    return [item for item in items if item.get('type') == 'message'
            and item.get('text', '').startswith('<agent_memory>\n')]


class MemoryContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = RUN_ROOT/self._testMethodName
        self.workspace = self.root/'workspace'
        self.workspace.mkdir(parents=True)
        self.manager = ThreadManager(self.root/'threads')
        self.trace = JsonlTrace(self.root/'trace.jsonl')

    async def drive(self, session, prompt='Run the memory check.'):
        try:
            await session.post_user_message(prompt)
            await session.run_until_settled()
            requests = len(session.client.prompts)
            await session.run_until_idle()
            self.assertEqual(len(session.client.prompts), requests)
        finally:
            await session.close()

    def assert_stable_prefixes(self, prompts):
        for before, after in zip(prompts, prompts[1:]):
            self.assertEqual(after.input[:len(before.input)], before.input)
            self.assertEqual(after.base_instructions, before.base_instructions)
            self.assertEqual(after.tools, before.tools)
            self.assertEqual(after.memory, before.memory)

    async def test_successful_writes_preserve_prefix_and_explicit_reads_return_latest(self):
        client = RecordingClient([
            [call('write_memory', path='index.md', text='version-one')],
            [call('read_memory', path='index.md')],
            [call('write_memory', path='index.md', text='version-two')],
            [call('read_memory', path='index.md')],
            [call('send_user_message', text='verified', wake_model=False)],
        ])
        session = self.manager.create(client, self.workspace, trace=self.trace)
        await self.drive(session)
        self.assertEqual(session.agent_context.read_memory('index.md'), 'version-two')
        self.assertEqual(len(client.prompts), 5)
        self.assert_stable_prefixes(client.prompts)
        for prompt in client.prompts:
            self.assertEqual(len(memory_items(prompt.input)), 0)
            self.assertIn('No saved memory yet.', prompt.memory['text'])
        outputs = [item['output'] for item in client.prompts[-1].input if item['type']=='function_call_output']
        self.assertIn('version-one', outputs)
        self.assertIn('version-two', outputs)

    async def test_resume_preserves_initial_history_and_reads_external_edits_on_demand(self):
        first = RecordingClient([[call('write_memory', path='index.md', text='saved-in-history')],
                                 [call('send_user_message', text='saved', wake_model=False)]])
        session = self.manager.create(first, self.workspace, trace=self.trace)
        await self.drive(session)
        original = session.state.history.snapshot()
        session.agent_context.write_memory('index.md', 'external-edit-after-close')
        resumed_client = RecordingClient([[call('read_memory', path='index.md')],
                                          [call('send_user_message', text='read-current-file', wake_model=False)]])
        with self.assertRaisesRegex(RuntimeError, 'Memory invariant'):
            self.manager.resume(resumed_client, session.thread_id, trace=self.trace)
        session.agent_context.write_memory('index.md', 'saved-in-history')
        resumed = self.manager.resume(resumed_client, session.thread_id, trace=self.trace)
        self.assertEqual(resumed.state.history.snapshot()[:len(original)], original)
        await self.drive(resumed)
        first_input = resumed_client.prompts[0].input
        self.assertNotIn('external-edit-after-close', json.dumps(first_input))
        self.assertIn('saved-in-history', json.dumps(resumed_client.prompts[1].input))
        self.assert_stable_prefixes(resumed_client.prompts)
        self.assertEqual(len(memory_items(resumed.state.history.snapshot())), 0)
        rows = [json.loads(line) for line in resumed.rollout.path.read_text().splitlines()]
        self.assertTrue(rows[-1]['payload']['clean'])

    async def test_failed_writes_do_not_replace_saved_content_or_initial_context(self):
        client = RecordingClient([
            [call('write_memory', path='index.md', text='x'*8001)],
            [call('write_memory', path='../outside.md', text='escape')],
            [call('read_memory', path='index.md')],
            [call('send_user_message', text='expected-errors-observed', wake_model=False)],
        ])
        session = self.manager.create(client, self.workspace, trace=self.trace)
        await self.drive(session)
        self.assertEqual(session.agent_context.read_memory('index.md'), DEFAULT_MEMORY)
        self.assertFalse((session.agent_context.root/'outside.md').exists())
        self.assert_stable_prefixes(client.prompts)
        outputs = [item['output'] for item in client.prompts[-1].input if item['type']=='function_call_output']
        self.assertEqual(sum('"error": "ValueError"' in output for output in outputs), 2)
        self.assertIn(DEFAULT_MEMORY, outputs)
        oversized = json.loads(outputs[0])['message']
        self.assertIn('received 8001 characters', oversized)
        self.assertIn('This file was not changed', oversized)
        self.assertIn('Consolidate memory before retrying', oversized)
        self.assertIn('confirm detail writes succeeded before shortening the index', oversized)

    async def test_detail_file_is_read_on_demand_and_clearing_index_preserves_initial_snapshot(self):
        client = RecordingClient([
            [call('write_memory', path='topics/detail.md', text='detail-value')],
            [call('read_memory', path='topics/detail.md')],
            [call('write_memory', path='index.md', text='')],
            [call('read_memory', path='index.md')],
            [call('send_user_message', text='cleared', wake_model=False)],
        ])
        session = self.manager.create(client, self.workspace, trace=self.trace)
        await self.drive(session)
        self.assertEqual(session.agent_context.read_memory('index.md'), '')
        self.assertEqual(session.agent_context.read_memory('topics/detail.md'), 'detail-value')
        self.assert_stable_prefixes(client.prompts)
        self.assertIn('No saved memory yet.', client.prompts[-1].memory['text'])
        outputs = [i['output'] for i in client.prompts[-1].input if i['type']=='function_call_output']
        self.assertIn('detail-value', outputs)
        self.assertIn('', outputs)

    async def test_legacy_thread_appends_initial_memory_once_without_rewriting_history(self):
        recorder, meta = RolloutRecorder.create(self.root/'threads', self.workspace, asdict(SessionConfiguration()))
        original_item = {'type': 'message', 'role': 'user', 'source': 'terminal', 'text': 'legacy history'}
        recorder.append('session_opened', {'session_id': 'legacy'})
        recorder.append('response_item', original_item)
        recorder.append('session_closed', {'session_id': 'legacy', 'step_number': 1, 'clean': True})
        recorder.close()
        context = AgentContext(recorder.path.parent)
        context.initialize()
        context.write_memory('index.md', 'legacy disk memory')
        session = self.manager.resume(RecordingClient([]), meta['thread_id'], trace=self.trace)
        self.assertEqual(session.state.history.snapshot()[0], original_item)
        self.assertIn('legacy disk memory', session.memory.item()['text'])
        self.assertFalse(memory_items(session.state.history.snapshot()))
        await session.close()
        context.write_memory('index.md', 'later external edit')
        with self.assertRaisesRegex(RuntimeError, 'Memory invariant'):
            self.manager.resume(RecordingClient([]), meta['thread_id'], trace=self.trace)
        context.write_memory('index.md', 'legacy disk memory')
        resumed = self.manager.resume(RecordingClient([]), meta['thread_id'], trace=self.trace)
        try:
            self.assertEqual(len(memory_items(resumed.state.history.snapshot())), 0)
            self.assertIn('legacy disk memory', resumed.memory.item()['text'])
            self.assertNotIn('later external edit', json.dumps(resumed.state.history.snapshot()))
        finally:
            await resumed.close()

    async def test_memory_patch_preserves_unmodified_text_prefix_and_resume(self):
        original = '# Memory\r\n\r\nPreference: concise\r\nKeep: unchanged\r\n'
        edit = memory_patch('*** Update File: index.md', '@@',
                            '-Preference: concise', '+Preference: 中文')
        client = RecordingClient([
            [call('write_memory', path='index.md', text=original)],
            [call('apply_memory_patch', patch=edit)],
            [call('send_user_message', text='patched', wake_model=False)],
        ])
        session = self.manager.create(client, self.workspace, trace=self.trace)
        await self.drive(session)
        expected = original.replace('Preference: concise', 'Preference: 中文')
        self.assertEqual((session.agent_context.memory_root/'index.md').read_bytes(), expected.encode())
        self.assert_stable_prefixes(client.prompts)
        patch_call = next(i for i in client.prompts[-1].input
                          if i.get('name') == 'apply_memory_patch')
        self.assertNotIn('Keep: unchanged', patch_call['arguments'])
        result = next(i for i in client.prompts[-1].input
                      if i.get('type') == 'function_call_output' and i['call_id'] == patch_call['call_id'])
        self.assertTrue(result['output'].startswith('Success. Updated the following memory files:\nM index.md'))
        self.assertIn('<memory_update>', result['output'])
        before = session.state.history.snapshot()
        resumed_client = RecordingClient([[call('read_memory', path='index.md')],
                                          [call('send_user_message', text='restored', wake_model=False)]])
        resumed = self.manager.resume(resumed_client, session.thread_id, trace=self.trace)
        self.assertEqual(resumed.state.history.snapshot()[:len(before)], before)
        await self.drive(resumed)
        # read_memory returns text with universal newlines; the disk retains CRLF.
        outputs = [i['output'] for i in resumed_client.prompts[-1].input
                   if i['type'] == 'function_call_output']
        self.assertIn(expected.replace('\r\n', '\n'), outputs)
        self.assertEqual(len(memory_items(resumed.state.history.snapshot())), 0)

    async def test_memory_patch_add_move_delete_and_multiple_chunks(self):
        edit = memory_patch(
            '*** Add File: topics/note.md', '+first', '+middle', '+last',
            '*** Update File: topics/note.md', '*** Move to: topics/renamed.md',
            '@@', '-first', '+updated', '@@', ' last', '+tail', '*** End of File',
            '*** Add File: topics/temporary.md', '+temporary',
            '*** Delete File: topics/temporary.md')
        client = RecordingClient([[call('apply_memory_patch', patch=edit)],
                                  [call('read_memory', path='topics/renamed.md')],
                                  [call('send_user_message', text='done', wake_model=False)]])
        session = self.manager.create(client, self.workspace, trace=self.trace)
        await self.drive(session)
        root = session.agent_context.memory_root
        self.assertEqual((root/'topics/renamed.md').read_text(), 'updated\nmiddle\nlast\ntail\n')
        self.assertFalse((root/'topics/note.md').exists())
        self.assertFalse((root/'topics/temporary.md').exists())
        self.assertEqual(session.agent_context.read_memory('index.md'), DEFAULT_MEMORY)
        self.assert_stable_prefixes(client.prompts)

    async def test_memory_patch_rejects_bad_context_limits_binary_and_syntax(self):
        edits = [
            memory_patch('*** Update File: index.md', '@@', '-absent line', '+replacement'),
            memory_patch('*** Add File: index.md', '+'+'字'*8001),
            memory_patch('*** Add File: topics/large.md', '+'+'x'*32001),
            memory_patch('*** Add File: topics/binary.md', '+text\x00binary'),
            # The complete patch must parse before an earlier valid addition is written.
            memory_patch('*** Add File: topics/not-created.md', '+valid', '*** Invalid Header'),
        ]
        client = RecordingClient([[call('apply_memory_patch', patch=edit) for edit in edits],
                                  [call('read_memory', path='index.md')],
                                  [call('send_user_message', text='expected-errors', wake_model=False)]])
        session = self.manager.create(client, self.workspace, trace=self.trace)
        await self.drive(session)
        self.assertEqual(session.agent_context.read_memory('index.md'), DEFAULT_MEMORY)
        self.assertFalse((session.agent_context.memory_root/'topics').exists())
        errors = [json.loads(i['output']) for i in client.prompts[1].input
                  if i['type']=='function_call_output']
        self.assertEqual(len(errors), len(edits))
        self.assertTrue(all(e['error']=='ValueError' for e in errors))
        self.assertIn('received 8002 characters', errors[1]['message'])
        self.assertIn('Consolidate memory before retrying', errors[1]['message'])
        self.assertIn('received 32002 characters', errors[2]['message'])
        self.assertNotIn('Consolidate memory before retrying', errors[2]['message'])
        self.assert_stable_prefixes(client.prompts)

    async def test_memory_patch_confines_paths_and_keeps_index_entrypoint(self):
        outside = self.workspace/'outside.md'
        outside.write_text('outside\n')
        edits = [memory_patch('*** Add File: '+str(outside), '+overwrite'),
                 memory_patch('*** Add File: ../instructions.md', '+overwrite'),
                 memory_patch('*** Add File: note.txt', '+invalid extension'),
                 memory_patch('*** Update File: link.md', '@@', '-outside', '+overwrite'),
                 memory_patch('*** Add File: linked/new.md', '+escape'),
                 memory_patch('*** Delete File: index.md'),
                 memory_patch('*** Update File: index.md', '*** Move to: moved.md',
                              '@@', '-No saved memory yet.', '+moved')]
        client = RecordingClient([[call('apply_memory_patch', patch=edit) for edit in edits],
                                  [call('send_user_message', text='expected-errors', wake_model=False)]])
        session = self.manager.create(client, self.workspace, trace=self.trace)
        context = session.agent_context
        (context.memory_root/'link.md').symlink_to(outside)
        (context.memory_root/'linked').symlink_to(self.workspace, target_is_directory=True)
        instructions = context.instructions()
        await self.drive(session)
        self.assertEqual(outside.read_text(), 'outside\n')
        self.assertFalse((self.workspace/'new.md').exists())
        self.assertEqual(context.instructions(), instructions)
        self.assertEqual(context.read_memory('index.md'), DEFAULT_MEMORY)
        errors = [json.loads(i['output']) for i in client.prompts[-1].input
                  if i['type']=='function_call_output']
        self.assertEqual(len(errors), len(edits))
        self.assertTrue(all(e['error']=='ValueError' for e in errors))

    async def test_memory_patch_reports_partial_progress_and_allows_recovery(self):
        edit = memory_patch('*** Add File: topics/committed.md', '+kept',
                            '*** Update File: index.md', '@@', '-missing', '+bad')
        retry = memory_patch('*** Update File: index.md', '@@',
                             '-No saved memory yet.', '+Recovered successfully.')
        client = RecordingClient([[call('apply_memory_patch', patch=edit)],
                                  [call('read_memory', path='topics/committed.md')],
                                  [call('apply_memory_patch', patch=retry)],
                                  [call('send_user_message', text='recovered', wake_model=False)]])
        session = self.manager.create(client, self.workspace, trace=self.trace)
        await self.drive(session)
        self.assertEqual(session.agent_context.read_memory('topics/committed.md'), 'kept\n')
        self.assertIn('Recovered successfully.', session.agent_context.read_memory('index.md'))
        error = next(i['output'] for i in client.prompts[1].input if i['type']=='function_call_output')
        self.assertIn('A topics/committed.md', json.loads(error)['message'])
        self.assert_stable_prefixes(client.prompts)

    async def test_memory_patch_failed_atomic_replace_keeps_original(self):
        edit = memory_patch('*** Update File: index.md', '@@',
                            '-No saved memory yet.', '+Must not be committed.')
        client = RecordingClient([[call('apply_memory_patch', patch=edit)],
                                  [call('send_user_message', text='failure-observed', wake_model=False)]])
        session = self.manager.create(client, self.workspace, trace=self.trace)
        with mock_patch('agent_harness.core.context_manager.agent_context.os.replace',
                        side_effect=OSError('simulated commit failure')):
            await self.drive(session)
        self.assertEqual(session.agent_context.read_memory('index.md'), DEFAULT_MEMORY)
        self.assertEqual(list(session.agent_context.memory_root.glob('.memory-*')), [])
        error = next(i['output'] for i in client.prompts[1].input if i['type']=='function_call_output')
        self.assertIn('simulated commit failure', json.loads(error)['message'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
