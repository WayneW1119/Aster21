"""Archive/view isolation, bounded pagination, durable IDs and actual tool flow."""

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import unittest
from uuid import uuid4

from agent_harness.core.context_manager import ContextManager
from agent_harness.core.rollout import RolloutRecorder
from agent_harness.core.thread_manager import ThreadManager
from agent_harness.core.tools.history import HistoryReader
from agent_harness.core.trace import JsonlTrace
from agent_harness.render_trace import render
from test_memory_context import RecordingClient, call

ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = None


def setUpModule():
    global RUN_ROOT
    RUN_ROOT = ROOT/'test_runs'/(datetime.now().strftime('%Y%m%d-%H%M%S')+'-history-query-'+uuid4().hex[:8])
    RUN_ROOT.mkdir(parents=True)
    print(f'History query regression artifacts: {RUN_ROOT}', flush=True)


def tearDownModule():
    for path in RUN_ROOT.glob('*/trace.jsonl'):
        path.with_suffix('.md').write_text(render(path), encoding='utf-8')


def message(text):
    return {'type': 'message', 'role': 'user', 'text': text}


class HistoryQueryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = RUN_ROOT/self._testMethodName
        self.workspace = self.root/'workspace'
        self.workspace.mkdir(parents=True)
        self.manager = ThreadManager(self.root/'threads')
        self.trace = JsonlTrace(self.root/'trace.jsonl')

    def save(self, name, data):
        (self.root/name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

    def test_archive_and_effective_view_are_detached_from_mutable_inputs(self):
        history = ContextManager()
        items = [message('one'), message('two')]
        expected = deepcopy(items)
        history.record_items(items)
        ids = history.ids()
        items[0]['text'] = 'mutated'
        history.snapshot()[1]['text'] = 'mutated'
        history.item(ids[0])['text'] = 'mutated'
        ids.clear()
        self.assertEqual(history.snapshot(), expected)
        self.assertEqual(history.ids('archive'), history.ids('effective'))
        # Internal view references can be changed without modifying the archive.
        # No public replacement API/tool is introduced by this change.
        history._effective_ids = history.ids()[1:]
        self.assertEqual(history.snapshot(), expected[1:])
        self.assertEqual([history.item(i) for i in history.ids('archive')], expected)
        history.record_items([message('three')])
        self.assertEqual(history.ids()[-1], 'h000000000003')
        self.assertEqual(history.ids('archive')[0], 'h000000000001')
        self.save('views.json', {'archive': history.ids('archive'), 'effective': history.ids()})

    def test_pagination_is_lossless_bounded_and_frozen_while_history_grows(self):
        history = ContextManager()
        originals = [message('first'), message(('中文\n"\\🙂\t'*600)), message('last')]
        history.record_items(originals)
        reader = HistoryReader(history, 'test-thread')
        pages, pieces = [], {}
        cursor = None
        for number in range(100):
            raw = reader.query('archive', cursor, None, 2, 1024)
            self.assertLessEqual(len(raw), 1024)
            page = json.loads(raw)
            pages.append(page)
            self.assertEqual(page['snapshot_revision'], 3)
            self.assertLessEqual(len(page['entries']), 2)
            for entry in page['entries']:
                previous = pieces.get(entry['id'], '')
                self.assertEqual(entry['offset'], len(previous))
                pieces[entry['id']] = previous + entry['item_json']
                if entry['complete']:
                    self.assertEqual(len(pieces[entry['id']]), entry['total_chars'])
            history.record_items([message(f'new arrival {number}')])
            cursor = page['next_cursor']
            if cursor is None:
                break
        else:
            self.fail('Pagination chased newly appended history')
        self.assertEqual([json.loads(text) for text in pieces.values()], originals)
        self.assertGreater(len(pages), 3)
        self.save('pages.json', pages)

    def test_inclusive_id_lookup_views_and_empty_history(self):
        history = ContextManager()
        reader = HistoryReader(history, 'test-thread')
        empty = json.loads(reader.query('effective', None, None, 10, 1024))
        self.assertEqual(empty['entries'], [])
        self.assertIsNone(empty['next_cursor'])
        history.record_items([message('a'), message('b'), message('c')])
        start = history.ids()[1]
        archive = json.loads(reader.query('archive', None, start, 1, 24000))
        effective = json.loads(reader.query('effective', None, start, 1, 24000))
        self.assertEqual(archive['entries'], effective['entries'])
        self.assertEqual(json.loads(archive['entries'][0]['item_json']), message('b'))
        self.assertIsNotNone(archive['next_cursor'])
        self.save('lookup.json', {'archive': archive, 'effective': effective})

    def test_invalid_arguments_and_cross_thread_view_cursors_are_rejected(self):
        history = ContextManager()
        history.record_items([message('a'), message('b')])
        reader = HistoryReader(history, 'scope-one')
        args = dict(view='archive', cursor=None, start_id=None, limit=1, max_chars=1024)
        for change in ({'view':'invalid'}, {'limit':True}, {'limit':0}, {'limit':51},
                       {'max_chars':1023}, {'max_chars':24001}, {'start_id':'unknown'},
                       {'start_id':[]}, {'cursor':123}, {'cursor':'!!!'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                reader.query(**(args | change))
        first = json.loads(reader.query(**args))
        cursor = first['next_cursor']
        self.assertIsNotNone(cursor)
        with self.assertRaises(ValueError):
            reader.query(**(args | {'view':'effective', 'cursor':cursor}))
        with self.assertRaises(ValueError):
            HistoryReader(history, 'scope-two').query(**(args | {'cursor':cursor}))
        with self.assertRaises(ValueError):
            reader.query(**(args | {'cursor':cursor, 'start_id':history.ids()[0]}))
        for revision, index, offset in ((99,0,0), (2,2,0), (2,1,999), (-1,0,0)):
            bad = reader._cursor('archive', revision, index, offset)
            with self.assertRaises(ValueError):
                reader.query(**(args | {'cursor':bad}))

    async def test_ids_survive_resume_and_never_leak_into_prompt_items(self):
        client = RecordingClient([[call('send_user_message', text='first', wake_model=False)]])
        session = self.manager.create(client, self.workspace, trace=self.trace)
        await session.post_user_message('ID stability')
        await session.run_until_settled()
        await session.close()
        ids, items = session.state.history.ids(), session.state.history.snapshot()
        resumed_client = RecordingClient([[call('send_user_message', text='second', wake_model=False)]])
        resumed = self.manager.resume(resumed_client, session.thread_id, trace=self.trace)
        try:
            self.assertEqual(resumed.state.history.ids()[:len(ids)], ids)
            self.assertEqual(resumed.state.history.snapshot()[:len(items)], items)
            await resumed.post_user_message('Continue')
            await resumed.run_until_settled()
        finally:
            await resumed.close()
        rows = [json.loads(line) for line in session.rollout.path.read_text().splitlines()]
        stored = [r for r in rows if r['type']=='response_item']
        self.assertEqual([r['history_id'] for r in stored], resumed.state.history.ids('archive'))
        for prompt in client.prompts + resumed_client.prompts:
            self.assertTrue(any(t['name']=='query_history' for t in prompt.tools))
            for item in prompt.input:
                self.assertNotIn('history_id', item)
                self.assertNotIn('revision', item)
        self.save('id-audit.json', {'before':ids, 'after':resumed.state.history.ids()})

    async def test_legacy_thread_replay_assigns_same_ids_and_rejects_corrupt_new_ids(self):
        session = self.manager.create(RecordingClient([]), self.workspace, trace=self.trace)
        session.record_items([message('legacy')])
        await session.close()
        rows = [json.loads(line) for line in session.rollout.path.read_text().splitlines()]
        expected = session.state.history.ids()
        for row in rows:
            row.pop('history_id', None)
        legacy = ''.join(json.dumps(row)+'\n' for row in rows)
        session.rollout.path.write_text(legacy)
        resumed = self.manager.resume(RecordingClient([]), session.thread_id, trace=self.trace)
        self.assertEqual(resumed.state.history.ids()[:len(expected)], expected)
        await resumed.close()
        replay = self.manager.resume(RecordingClient([]), session.thread_id, trace=self.trace)
        self.assertEqual(replay.state.history.ids()[:len(expected)], expected)
        await replay.close()
        original = session.rollout.path.read_text()
        mixed = [json.loads(line) for line in original.splitlines()]
        next(r for r in mixed if r['type']=='response_item')['history_id'] = 'h000000009999'
        corrupt = ''.join(json.dumps(row)+'\n' for row in mixed)
        with self.assertRaisesRegex(ValueError, 'history ID'):
            RolloutRecorder._read(corrupt, session.thread_id)
        self.assertEqual(session.rollout.path.read_text(), original)
        self.save('legacy-audit.json', {'expected':expected, 'final':replay.state.history.ids()})

    async def test_actual_tool_pagination_and_cursor_survives_clean_resume(self):
        # Real scheduler/router/queue execution, with a pause between query pages.
        first = RecordingClient([
            [call('query_history', view='archive', cursor=None, start_id=None, limit=1, max_chars=1024)],
            [call('send_user_message', text='page-one', wake_model=False)],
        ])
        session = self.manager.create(first, self.workspace, trace=self.trace)
        await session.post_user_message('historical-marker-9D4A')
        await session.run_until_settled()
        await session.close()
        receipts = [i for i in session.state.history.snapshot() if i['type']=='function_call_output']
        page_one = json.loads(receipts[0]['output'])
        cursor = page_one['next_cursor']
        self.assertIsNotNone(cursor)
        second = RecordingClient([
            [call('query_history', view='archive', cursor=cursor, start_id=None, limit=50, max_chars=24000)],
            [call('send_user_message', text='page-two', wake_model=False)],
        ])
        resumed = self.manager.resume(second, session.thread_id, trace=self.trace)
        try:
            await resumed.post_user_message('Continue the frozen historical query.')
            await resumed.run_until_settled()
        finally:
            await resumed.close()
        results = [i for i in resumed.state.history.snapshot() if i['type']=='function_call_output']
        page_two = json.loads(results[2]['output'])
        self.assertEqual(page_two['snapshot_revision'], page_one['snapshot_revision'])
        self.assertIsNone(page_two['next_cursor'])
        recovered = [json.loads(e['item_json']) for p in (page_one,page_two) for e in p['entries']]
        self.assertTrue(any(i.get('text')=='historical-marker-9D4A' for i in recovered))
        self.assertFalse(any('Continue the frozen' in i.get('text','') for i in recovered))
        self.assertEqual(resumed.state.sent_messages, ['page-two'])
        self.save('tool-pages.json', [page_one,page_two])


if __name__ == '__main__':
    unittest.main(verbosity=2)
