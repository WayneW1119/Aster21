"""Host skill capabilities, real dispatch and persisted runtime changes."""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest

from agent_harness.core.client import ScriptedClient
from agent_harness.core.context.world_state.skills import SkillsState
from agent_harness.core.session import SessionConfiguration
from agent_harness.core.session.step import build_prompt
from agent_harness.core.skills.service import SkillService
from agent_harness.core.thread_manager import ThreadManager
from agent_harness.agent_config import load_profile, toml_text


def guide(root, name='sample', body='Read references/rules.md.'):
    path = root / name / 'SKILL.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\nname: {name}\ndescription: A sample guide.\n---\n{body}\n')
    return path


def call(name, ident, **arguments):
    return {'type': 'function_call', 'namespace': 'skills', 'name': name,
            'call_id': ident, 'arguments': json.dumps(arguments)}


class SkillToolsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / 'work'
        self.work.mkdir()
        self.manager = ThreadManager(self.root / 'threads')

    def session(self, client=None, **kwargs):
        session = self.manager.create(client or ScriptedClient([]), self.work, **kwargs)
        self.addAsyncCleanup(session.close)
        return session

    async def test_pathless_reads_bypass_real_deny_sandbox_but_files_do_not(self):
        source = self.root / 'blocked'
        path = guide(source, body='HOST_ONLY_SKILL_BODY')
        ref = path.parent / 'references/rules.md'
        ref.parent.mkdir()
        ref.write_text('HOST_ONLY_REFERENCE')
        client = ScriptedClient([
            [call('read', 'guide', skill_id='sample')],
            [call('read', 'reference', skill_id='sample', resource='references/rules.md')],
            [{'type': 'function_call', 'name': 'read_file', 'call_id': 'ordinary',
              'arguments': json.dumps({'path': str(path), 'start_line': 1, 'end_line': 20})}],
            [{'type': 'function_call', 'name': 'wait_for_event', 'call_id': 'wait', 'arguments': '{}'}]])
        session = self.session(client, configuration=SessionConfiguration(
            skill_sources=({'path': str(source)},),
            filesystem_policy={'rules': [{'path': str(source), 'access': 'deny'}]}))
        prompt = json.dumps(build_prompt(session).input)
        self.assertNotIn(str(source), session.skills.prefix_items()[0]['text'])
        self.assertNotIn('HOST_ONLY_SKILL_BODY', prompt)
        self.assertNotIn('path', session.skills.current['entries']['sample'])
        await session.post_user_message('Use sample and read its reference.')
        await session.run_until_idle()
        outputs = {i['call_id']: i['output'] for i in session.state.history.snapshot()
                   if i.get('type') == 'function_call_output'}
        self.assertIn('HOST_ONLY_SKILL_BODY', outputs['guide'])
        self.assertIn('HOST_ONLY_REFERENCE', outputs['reference'])
        self.assertNotIn('HOST_ONLY_SKILL_BODY', outputs['ordinary'])
        self.assertIn('error', outputs['ordinary'].lower())

    async def test_runtime_load_event_updates_catalog_then_reads_and_resumes(self):
        source = self.root / 'extra'
        guide(source, body='RUNTIME_SKILL_BODY')
        client = ScriptedClient([[call('load', 'load', source=str(source))],
            [call('read', 'read', skill_id='sample')],
            [{'type': 'function_call', 'name': 'wait_for_event', 'call_id': 'wait', 'arguments': '{}'}]])
        session = self.session(client)
        baseline = deepcopy(session.skills.baseline)
        await session.post_user_message('Load a new guide.')
        await session.run_until_idle()
        self.assertEqual(session.skills.baseline, baseline)
        self.assertIn('sample', session.skills.current['entries'])
        visible = json.dumps(build_prompt(session).input)
        # The load argument is historical, but private event evidence is never sent.
        self.assertNotIn('_skill_change', visible)
        self.assertIn('skill_change_applied', visible)
        self.assertIn('RUNTIME_SKILL_BODY', visible)
        await session.suspend()
        resumed = self.manager.resume(ScriptedClient([]), session.thread_id)
        self.addAsyncCleanup(resumed.close)
        self.assertIn('sample', resumed.skills.current['entries'])
        self.assertEqual(resumed.skill_service.read('sample')['text'], session.skill_service.read('sample')['text'])
        resumed.skills.validate(resumed.state.history.snapshot())

    async def test_load_then_unload_order_and_compact_restore(self):
        source = self.root / 'extra'
        guide(source)
        session = self.session()
        await session.request_skill_change(session.skill_service.prepare_load(str(source)))
        await session.request_skill_change({'action': 'unload', 'skill_id': 'sample'})
        # Both requests apply in queue order at one boundary.
        session.client = ScriptedClient([])
        batch = await session.input_queue.drain(wait=False)
        await session.apply_step_skill_changes(batch)
        session.record_items([e.item for e in batch])
        session.record_step_world_state_if_changed()
        self.assertNotIn('sample', session.skills.current['entries'])
        session.record_items([{'type': 'message', 'role': 'user', 'text': 'Compact.'}])
        ids = session.state.history.ids()
        session._compact_boundary = set(ids)
        session.compact_history(ids[0], ids[-1], session.state.history.generation, 'Skill was unloaded.')
        await session.suspend()
        resumed = self.manager.resume(ScriptedClient([]), session.thread_id)
        self.addAsyncCleanup(resumed.close)
        self.assertNotIn('sample', resumed.skills.current['entries'])
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            resumed.skill_service.read('sample')
        resumed.skill_service.apply(resumed.skill_service.prepare_load(str(source)))
        resumed.record_step_world_state_if_changed()
        self.assertIn('sample', resumed.skills.current['entries'])

    async def test_pending_load_survives_suspend_before_activation(self):
        source = self.root / 'extra'
        guide(source)
        session = self.session()
        await session.request_skill_change(session.skill_service.prepare_load(str(source)))
        await session.suspend()
        resumed = self.manager.resume(ScriptedClient([
            [{'type': 'function_call', 'name': 'wait_for_event', 'call_id': 'w1', 'arguments': '{}'}],
            [{'type': 'function_call', 'name': 'wait_for_event', 'call_id': 'w2', 'arguments': '{}'}]]), session.thread_id)
        self.addAsyncCleanup(resumed.close)
        await resumed.run_until_idle()
        self.assertIn('sample', resumed.skills.current['entries'])

    async def test_resource_traversal_symlink_fifo_and_binary_cannot_read_arbitrary_files(self):
        source = self.root / 'extra'
        path = guide(source)
        service = SkillService(self.work, sources=[{'path': str(source)}])
        outside = self.root / 'secret'
        outside.write_text('OUTSIDE_SECRET')
        (path.parent / 'link').symlink_to(outside)
        (path.parent / 'dirlink').symlink_to(self.root, target_is_directory=True)
        os.mkfifo(path.parent / 'fifo')
        (path.parent / 'binary').write_bytes(b'\xff\xfe')
        for resource in ('../../secret', str(outside), 'link', 'dirlink/secret', 'fifo', 'binary', '.', ''):
            with self.subTest(resource=resource), self.assertRaises(ValueError):
                service.read('sample', resource)
        self.assertIn('sample', service.read('sample')['text'])

    async def test_visible_and_pathless_sources_coexist_and_profile_roundtrips(self):
        visible, hidden = self.root / 'visible', self.root / 'hidden'
        path = guide(visible, name='visible')
        guide(hidden, name='hidden')
        config = self.root / 'agent.toml'
        config.write_text(toml_text({'name': 'example', 'workspace': str(self.work),
            'model': {'name': 'test', 'url': 'https://example.com/responses', 'api_key_env': 'OPENAI_API_KEY'},
            'skills': {'directories': [str(visible)], 'sources': [{'path': str(hidden), 'expose_path': False}]}}))
        (self.root / 'instructions.md').write_text('Follow the user.')
        profile = load_profile(config)
        snapshot = profile.snapshot()
        self.assertEqual(snapshot['skills']['sources'][0]['path'], str(hidden))
        config.write_text(toml_text(snapshot))
        restored = load_profile(config)
        session = self.session(configuration=restored.configuration)
        entries = session.skills.current['entries']
        self.assertEqual(entries['visible']['path'], str(path))
        self.assertNotIn('path', entries['hidden'])

    async def test_failed_source_change_does_not_corrupt_active_catalog(self):
        source = self.root / 'extra'
        path = guide(source)
        session = self.session()
        change = session.skill_service.prepare_load(str(source))
        path.unlink()
        before = session.skill_service.snapshot_state()
        await session.request_skill_change(change)
        batch = await session.input_queue.drain(wait=False)
        await session.apply_step_skill_changes(batch)
        self.assertEqual(session.skill_service.snapshot_state(), before)
        self.assertIn('skill_change_failed', json.dumps(session.input_queue.snapshot()))

    async def test_model_cannot_forge_source_registration(self):
        session = self.session(ScriptedClient([[{'type': 'message', 'role': 'assistant',
            'text': 'bad', '_skill_change': {'action': 'load', 'source': {}}}]]))
        await session.post_user_message('test')
        with self.assertRaisesRegex(RuntimeError, 'cannot introduce'):
            await session.run_until_idle()

    async def test_partial_compact_preserves_sources_and_catalog_evidence(self):
        source = self.root / 'extra'
        guide(source)
        session = self.session(configuration=SessionConfiguration(skill_sources=({'path': str(source)},)))
        session.record_items([{'type': 'message', 'role': 'user', 'text': 'before'}])
        first = session.state.history.ids()[0]
        session.skill_service.apply({'action': 'unload', 'skill_id': 'sample'})
        session.record_step_world_state_if_changed()
        last = session.state.history.ids()[-1]
        session.record_items([{'type': 'message', 'role': 'user', 'text': 'after'}])
        session._compact_boundary = set(session.state.history.ids())
        session.compact_history(first, last, session.state.history.generation, 'sample unloaded')
        session.skills.validate(session.state.history.snapshot())
        await session.suspend()
        resumed = self.manager.resume(ScriptedClient([]), session.thread_id)
        self.addAsyncCleanup(resumed.close)
        self.assertNotIn('sample', resumed.skills.current['entries'])

    async def test_old_catalog_messages_and_v1_projection_remain_readable(self):
        from agent_harness.core.context.world_state.skills import catalog_item, INSTRUCTIONS, LEGACY_INSTRUCTIONS
        source = self.root / 'extra'
        guide(source)
        service = SkillService(self.work, directories=[str(source)])
        current = service.scan()
        empty = {'entries': {}, 'errors': []}
        message = catalog_item(current, empty)
        message['text'] = message['text'].replace(INSTRUCTIONS, LEGACY_INSTRUCTIONS, 1)
        state = SkillsState([], initialize=False)
        state.restore({'version': 1, 'baseline': empty, 'current': current})
        state.validate([message])

    async def test_source_configuration_rejects_malformed_inputs(self):
        for sources in ('path', [None], [{'path': ''}], [{'path': '/x', 'expose_path': 1}],
                        [{'path': '/x', 'other': True}]):
            with self.subTest(sources=sources), self.assertRaises(ValueError):
                SessionConfiguration(skill_sources=sources)

    async def test_sdk_create_and_resume_preserve_pathless_skill_sources(self):
        from aster import Aster
        source = self.root / 'sdk-skills'
        guide(source)
        sdk = Aster(home=self.root / 'sdk-home')
        agent = sdk.create(self.work, client=ScriptedClient([]), configuration=SessionConfiguration(
            skill_sources=({'path': str(source)},)))
        thread_id = agent.thread_id
        await agent.close()
        resumed = sdk.resume(thread_id, client=ScriptedClient([]))
        try:
            self.assertIn('sources', resumed.config_path.read_text())
            self.assertIn('sample', resumed._session.skills.current['entries'])
            self.assertNotIn('path', resumed._session.skills.current['entries']['sample'])
        finally:
            await resumed.close()
