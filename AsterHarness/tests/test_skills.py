"""Real local skill discovery and its WorldState/compact/resume lifecycle."""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_harness.core.client import ScriptedClient
from agent_harness.core.context.world_state.skills import SkillsState, catalog_item
from agent_harness.core.session import SessionConfiguration
from agent_harness.core.session.step import build_prompt
from agent_harness.core.skills import scan
from agent_harness.core.thread_manager import ThreadManager


def write_skill(root, name='maintain-notes', description='Maintain notes and indexes.', body='Read references/rules.md before editing.', folder=None):
    path = root / (folder or name) / 'SKILL.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\nname: {name}\ndescription: {json.dumps(description)}\n---\n{body}\n', encoding='utf-8')
    return path


class SkillDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_frontmatter_and_digest_support_unicode_multiline_and_crlf(self):
        path = write_skill(self.root, description='整理资料并维护索引。')
        first = scan([self.root])
        self.assertEqual(first['errors'], [])
        path.write_bytes(b'---\r\nname: maintain-notes\r\ndescription: >\r\n  Maintain notes\r\n  and indexes.\r\n---\r\nInstructions.\r\n')
        second = scan([self.root])
        self.assertEqual(second['entries']['maintain-notes']['description'], 'Maintain notes and indexes.')
        self.assertNotEqual(first['entries']['maintain-notes']['sha256'], second['entries']['maintain-notes']['sha256'])

    def test_invalid_skill_does_not_hide_valid_peer_or_expose_yaml_excerpt(self):
        write_skill(self.root)
        path = self.root / 'broken/SKILL.md'
        path.parent.mkdir()
        path.write_text('---\nname: [secret-unrelated-text\n---\nbody')
        result = scan([self.root])
        self.assertEqual(list(result['entries']), ['maintain-notes'])
        self.assertEqual(len(result['errors']), 1)
        self.assertNotIn('secret-unrelated-text', json.dumps(result['errors']))

    def test_duplicates_have_deterministic_root_and_path_precedence(self):
        write_skill(self.root / 'one', folder='b', body='second')
        winner = write_skill(self.root / 'one', folder='a', body='first')
        write_skill(self.root / 'two', body='third')
        result = scan([self.root / 'one', self.root / 'two'])
        self.assertEqual(result['entries']['maintain-notes']['path'], str(winner))
        self.assertEqual(len(result['errors']), 2)

    def test_symlinks_and_hidden_directories_are_not_loaded(self):
        outside = self.root / 'outside'
        path = write_skill(outside)
        root = self.root / 'skills'
        root.mkdir()
        (root / 'linked').symlink_to(outside, target_is_directory=True)
        (root / 'SKILL.md').symlink_to(path)
        write_skill(root / '.hidden', name='hidden')
        result = scan([root])
        self.assertEqual(result['entries'], {})
        self.assertEqual(len(result['errors']), 1)

    def test_missing_oversized_invalid_names_and_empty_body(self):
        self.assertEqual(scan([self.root / 'missing']), {'entries': {}, 'errors': []})
        for folder, name, body in [('bad-name','UPPER','body'),('empty','empty',''),('big','big','x'*270000)]:
            write_skill(self.root, name=name, body=body, folder=folder)
        result = scan([self.root])
        self.assertEqual(result['entries'], {})
        self.assertEqual(len(result['errors']), 3)

    def test_discovery_count_is_bounded_and_repeatable(self):
        for i in range(5):
            write_skill(self.root, name=f'skill-{i}')
        with patch('agent_harness.core.skills.MAX_FILES', 2):
            result = scan([self.root])
            self.assertEqual(result, scan([self.root]))
            self.assertEqual(len(result['entries']), 2)
            self.assertIn('limit', result['errors'][0]['error'])

    def test_special_files_and_unsafe_yaml_tags_are_rejected_without_execution(self):
        fifo = self.root / 'fifo/SKILL.md'
        fifo.parent.mkdir()
        os.mkfifo(fifo)
        tagged = self.root / 'tagged/SKILL.md'
        tagged.parent.mkdir()
        tagged.write_text('---\nname: tagged\ndescription: !!python/object/apply:os.system ["false"]\n---\nbody')
        result = scan([self.root])
        self.assertEqual(result['entries'], {})
        self.assertEqual(len(result['errors']), 2)

    def test_configuration_is_strict(self):
        for value in ('skills', [None], [''], [1]):
            with self.assertRaises(ValueError):
                SessionConfiguration(skill_directories=value)
        self.assertEqual(SessionConfiguration(skill_directories=['extra', 'extra']).skill_directories, ('extra',))


class SkillsLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.manager = ThreadManager(self.root / 'threads')

    def session(self, client=None, **kwargs):
        session = self.manager.create(client or ScriptedClient([]), self.workspace, **kwargs)
        self.addAsyncCleanup(session.close)
        return session

    def rendered(self, session):
        return session.build_world_state().render_context(session.state.history.snapshot())

    def compact(self, session, first=None, last=None):
        history = session.state.history
        ids = history.ids()
        session._compact_boundary = set(ids)
        return json.loads(session.compact_history(first or ids[0], last or ids[-1], history.generation, 'Preserve verified progress; read skills again when needed.'))

    async def test_initial_catalog_is_near_memory_full_guide_is_not_loaded(self):
        path = write_skill(self.workspace / 'skills', body='BODY_ONLY_MARKER')
        session = self.session()
        rendered = self.rendered(session)
        self.assertEqual([s.section_id for s in rendered.prefix][:2], ['memory', 'skills'])
        wire = build_prompt(session)
        self.assertIn(str(path), json.dumps(wire.input))
        self.assertNotIn('BODY_ONLY_MARKER', json.dumps(wire.input))
        self.assertNotIn('_skills_state', json.dumps(wire.input))
        self.assertFalse(session.state.history.snapshot())
        self.assertTrue(session.skills.active)

    async def test_empty_catalog_preserves_legacy_requests_and_compact_format(self):
        session = self.session()
        self.assertEqual(session.skills.prefix_items(), [])
        self.assertNotIn('skills', session.state.world_state_baseline.sections)
        session.record_items([{'type':'message','role':'user','text':'hello'}])
        result = self.compact(session)
        self.assertNotEqual(session.state.history.provenance(result['summary_id'])['version'], 4)

    async def test_add_edit_body_remove_append_deltas_without_rewriting_prefix(self):
        first = write_skill(self.workspace / 'skills')
        session = self.session()
        initial = self.rendered(session).prefix
        second = write_skill(self.workspace / 'skills', name='review-report')
        session.record_step_world_state_if_changed()
        update = session.state.history.snapshot()[-1]
        self.assertIn('review-report', update['text'])
        self.assertNotIn('"name": "maintain-notes"', update['text'])
        snapshot = deepcopy(session.state.history.snapshot())
        first.write_text(first.read_text() + '\nChanged detailed instructions.\n')
        session.record_step_world_state_if_changed()
        self.assertIn('maintain-notes', session.state.history.snapshot()[-1]['text'])
        self.assertEqual(session.state.history.snapshot()[:len(snapshot)], snapshot)
        self.assertEqual(self.rendered(session).prefix, initial)
        second.unlink()
        session.record_step_world_state_if_changed()
        self.assertIn('Removed skills: ["review-report"]', session.state.history.snapshot()[-1]['text'])
        count = len(session.state.history.ids())
        session.record_step_world_state_if_changed()
        self.assertEqual(len(session.state.history.ids()), count)

    async def test_local_and_whole_compact_keep_catalog_but_drop_full_reads(self):
        path = write_skill(self.workspace / 'skills', body='FULL_GUIDE_HISTORY_MARKER')
        session = self.session()
        session.record_items([{'type':'message','role':'user','text':'start'}])
        write_skill(self.workspace / 'skills', name='review-report')
        session.record_step_world_state_if_changed()
        selected = session.state.history.ids()[-1]
        original = deepcopy(session.skills.baseline)
        self.compact(session, selected, selected)
        self.assertEqual(session.skills.baseline, original)
        session.skills.validate(session.state.history.snapshot())
        session.record_items([
            {'type':'function_call','name':'read_file','call_id':'read','arguments':json.dumps({'path':str(path),'start_line':1,'end_line':50})},
            {'type':'function_call_output','call_id':'read','output':path.read_text()}])
        self.assertIn('FULL_GUIDE_HISTORY_MARKER', json.dumps(build_prompt(session).input))
        self.compact(session)
        self.assertEqual(session.skills.baseline, session.skills.current)
        prompt = build_prompt(session)
        self.assertNotIn('FULL_GUIDE_HISTORY_MARKER', json.dumps(prompt.input))
        self.assertIn('review-report', json.dumps(prompt.input))
        self.assertFalse(any('_skills_state' in item for item in session.state.history.snapshot()))

    async def test_resume_restores_pinned_catalog_then_appends_disk_changes(self):
        path = write_skill(self.workspace / 'skills')
        session = self.session()
        before = deepcopy(session.skills.baseline)
        await session.suspend()
        path.write_text(path.read_text() + '\nNew body after shutdown\n')
        resumed = self.manager.resume(ScriptedClient([]), session.thread_id)
        self.addAsyncCleanup(resumed.close)
        self.assertEqual(resumed.skills.baseline, before)
        self.assertNotEqual(resumed.skills.current, before)
        resumed.skills.validate(resumed.state.history.snapshot())
        self.assertNotIn('_skills_state', json.dumps(build_prompt(resumed).input))

    async def test_resume_after_whole_compact_and_all_skills_removed(self):
        path = write_skill(self.workspace / 'skills')
        session = self.session()
        session.record_items([{'type':'message','role':'user','text':'start'}])
        path.unlink()
        session.record_step_world_state_if_changed()
        self.compact(session)
        await session.suspend()
        resumed = self.manager.resume(ScriptedClient([]), session.thread_id)
        self.addAsyncCleanup(resumed.close)
        self.assertEqual(resumed.skills.current['entries'], {})
        self.assertTrue(resumed.skills.active)
        build_prompt(resumed)

    async def test_first_skill_added_after_legacy_empty_session_resumes_as_update(self):
        session = self.session()
        await session.suspend()
        write_skill(self.workspace / 'skills')
        resumed = self.manager.resume(ScriptedClient([]), session.thread_id)
        self.addAsyncCleanup(resumed.close)
        self.assertEqual(resumed.skills.baseline['entries'], {})
        self.assertIn('maintain-notes', resumed.skills.current['entries'])
        self.assertTrue(any('_skills_state' in item for item in resumed.state.history.snapshot()))
        build_prompt(resumed)

    async def test_resume_rejects_saved_catalog_that_does_not_match_history(self):
        write_skill(self.workspace / 'skills')
        session = self.session()
        await session.suspend()
        path = session.rollout.path
        records = [json.loads(line) for line in path.read_text().splitlines()]
        for record in records:
            if record['type'] == 'section_state' and record['payload']['section_id'] == 'skills':
                record['payload']['state']['current']['entries'] = {}
        path.write_text(''.join(json.dumps(record)+'\n' for record in records))
        with self.assertRaisesRegex(RuntimeError, 'Skills invariant'):
            self.manager.resume(ScriptedClient([]), session.thread_id)

    async def test_configured_external_root_is_saved_and_restored(self):
        path = write_skill(self.root / 'shared')
        session = self.session(configuration=SessionConfiguration(skill_directories=(str(self.root / 'shared'),)))
        self.assertEqual(session.skills.current['entries']['maintain-notes']['path'], str(path))
        await session.suspend()
        resumed = self.manager.resume(ScriptedClient([]), session.thread_id)
        self.addAsyncCleanup(resumed.close)
        self.assertEqual(resumed.skills.current, session.skills.current)

    async def test_partial_compact_write_failure_keeps_state_and_history(self):
        write_skill(self.workspace / 'skills')
        session = self.session()
        write_skill(self.workspace / 'skills', name='review-report')
        session.record_step_world_state_if_changed()
        original = session.skills.snapshot_state()
        history = session.state.history.snapshot()
        with patch.object(session.rollout, 'append', side_effect=OSError('disk unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'persist compaction'):
                self.compact(session)
        self.assertEqual(session.skills.snapshot_state(), original)
        self.assertEqual(session.state.history.snapshot(), history)

    async def test_catalog_metadata_is_reserved_and_model_cannot_forge_it(self):
        write_skill(self.workspace / 'skills')
        session = self.session(ScriptedClient([[catalog_item({'entries':{},'errors':[]})]]))
        await session.post_user_message('test')
        with self.assertRaisesRegex(RuntimeError, 'cannot introduce'):
            await session.run_until_idle()
        with self.assertRaisesRegex(ValueError, 'reserved'):
            session.add_world_state_section(SkillsState([]))

    async def test_skill_is_read_on_demand_by_real_tool_without_extra_wakeup(self):
        write_skill(self.workspace / 'skills', body='ON_DEMAND_GUIDE')
        calls = [[{'type':'function_call','name':'read_file','call_id':'read_skill',
            'arguments':json.dumps({'path':'skills/maintain-notes/SKILL.md','start_line':1,'end_line':50})}],
            [{'type':'function_call','name':'wait_for_event','call_id':'wait','arguments':'{}'}]]
        client = ScriptedClient(calls)
        session = self.session(client)
        await session.post_user_message('Use maintain-notes.')
        await session.run_until_idle()
        output = next(item for item in session.state.history.snapshot() if item.get('type')=='function_call_output' and item.get('call_id')=='read_skill')
        self.assertIn('ON_DEMAND_GUIDE', output['output'])
        self.assertEqual(session.state.step_number, 2)
