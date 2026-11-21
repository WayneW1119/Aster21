"""Shared guides are tool capabilities; runtime loads retain ordinary permissions."""
import json
from pathlib import Path
import tempfile
import unittest

from agent_harness.core.skills.service import SkillService
from agent_harness.core.sandbox import FilesystemSandbox
from squad.service import Service
from squad.skills import sources
from squad.migrate import migrate


def guide(root, name, text='guide'):
    path = root / name / 'SKILL.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\nname: {name}\ndescription: Test guide.\n---\n{text}\n')
    return path


class SkillScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='squad-skills-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = Service(self.root/'data',mode='real',relay_url='http://127.0.0.1:1',
            relay_mcp='http://127.0.0.1:2/mcp',squad_mcp='http://127.0.0.1:3/mcp',
            owner_relay_token='test',domain='test',user_account='test',models=['test'],model_url='http://localhost')
        self.addCleanup(self.service.store.db.close)
        self.agent,_ = self.service.store.create('alice',None,0,{'model':'test','instruction':'Own work','grants':[]},'account')

    def skills(self, agent=None):
        agent = agent or self.agent
        sandbox = FilesystemSandbox(self.service.workspace(agent), {'rules':self.service.rules(agent)})
        return SkillService(self.service.workspace(agent), sources=self.service.skill_sources(agent), workspace_skills=False, sandbox=sandbox)

    def test_three_scopes_hidden_files_and_no_workspace_population(self):
        entries = self.service.skill_sources(self.agent)
        for source,name in zip(entries,('global-guide','space-guide','personal-guide')):
            guide(Path(source['path']),name)
        service = self.skills()
        self.assertEqual(list(self.service.workspace(self.agent).iterdir()),[])
        catalog = service.scan()['entries']
        for name,scope in [('global-guide','global'),('space-guide','space'),('personal-guide','agent')]:
            self.assertEqual(catalog[name]['source'],scope)
            self.assertNotIn('path',catalog[name])
            self.assertIn('guide',service.read(name)['text'])
            with self.assertRaises(ValueError):
                service.sandbox.file_operation('bytes',path=str(service.bindings[name]),limit=10000)
        other,_ = self.service.store.create('bob',None,1,self.agent['config'],'other')
        self.assertNotIn('personal-guide',self.skills(other).scan()['entries'])

    def test_runtime_load_cannot_open_denied_sources_or_resources(self):
        protected = self.service.skill_sources(self.agent)[0]['path']
        guide(Path(protected),'protected-guide')
        service = self.skills()
        with self.assertRaises(ValueError): service.prepare_load(protected)
        local = self.service.workspace(self.agent)/'my-guides'
        guide(local,'local-guide')
        resource = local/'local-guide'/'references'
        resource.mkdir()
        (resource/'private.txt').symlink_to(Path(protected)/'protected-guide/SKILL.md')
        service.apply(service.prepare_load(str(local)))
        self.assertIn('local-guide',service.scan()['entries'])
        with self.assertRaises(ValueError): service.read('local-guide','references/private.txt')
        # The restore state does not convert a runtime source to a trusted source.
        saved = service.snapshot_state()
        saved['loaded'].append({'path':protected,'expose_path':False})
        service.restore(saved)
        self.assertNotIn('protected-guide',service.ordinary_bindings)
        service.scan()
        self.assertNotIn('protected-guide',service.ordinary_bindings)

    def test_conflicts_are_reported_and_legacy_workspace_copy_is_not_scanned(self):
        guide(Path(self.service.skill_sources(self.agent)[0]['path']),'shared','current')
        guide(Path(self.service.skill_sources(self.agent)[1]['path']),'shared','conflict')
        guide(self.service.workspace(self.agent)/'skills','shared','stale')
        service = self.skills()
        catalog = service.scan()
        self.assertTrue(any('Duplicate skill shared' in x['error'] for x in catalog['errors']))
        self.assertIn('current',service.read('shared')['text'])
        self.assertNotIn('stale',service.read('shared')['text'])

    def test_external_maintainer_grant_opens_source_only_for_maintainer(self):
        external = self.root/'netops'/'skills';guide(external,'local-network')
        manifest=self.service.shared_skill_root/'skills/sources.json'
        manifest.write_text(json.dumps([{'path':str(external),'label':'local-network-ops'}]))
        consumer=self.skills()
        self.assertIn('local-network',consumer.scan()['entries'])
        with self.assertRaises(ValueError): consumer.sandbox.file_operation('bytes',path=str(external/'local-network/SKILL.md'),limit=10000)
        maintained={**self.agent,'config':{**self.agent['config'],'grants':[{'path':str(external.parent),'access':'write'}]}}
        owner=self.skills(maintained)
        self.assertIn('local-network',owner.scan()['entries'])
        owner.sandbox.file_operation('bytes',path=str(external/'local-network/SKILL.md'),limit=10000)


class MigrationTests(unittest.TestCase):
    def test_offline_move_preserves_workspace_and_rollout_bytes(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);source=base/'old';destination=base/'new'
            (source/'control/fleet').mkdir(parents=True)
            (source/'workspaces/slot-00').mkdir(parents=True)
            (source/'workspaces/slot-00/own.txt').write_text('private old path: '+str(source))
            thread=source/'runtime/slot-00/threads/test';thread.mkdir(parents=True)
            (thread/'rollout.jsonl').write_text('historical '+str(source)+'\n')
            (thread/'thread.toml').write_text('workspace = "'+str(source/'workspaces/slot-00')+'"\n')
            with sqlite3.connect(source/'control/fleet/console.db') as db:
                db.execute('CREATE TABLE resources(id TEXT, root TEXT)')
                db.execute('INSERT INTO resources VALUES(?,?)',('studio',str(source)))
            preview=migrate(source,destination)
            self.assertFalse(preview['applied']);self.assertFalse(destination.exists())
            report=migrate(source,destination,apply=True)
            self.assertTrue(source.is_symlink())
            self.assertEqual((source/'workspaces/slot-00/own.txt').read_text(),'private old path: '+str(source))
            self.assertEqual((destination/'runtime/slot-00/threads/test/rollout.jsonl').read_text(),'historical '+str(source)+'\n')
            self.assertIn(str(destination),(destination/'runtime/slot-00/threads/test/thread.toml').read_text())
            self.assertTrue(Path(report['backup']).is_dir())
            self.assertTrue(Path(report['backup']).is_relative_to(destination/'control'))


class BuiltinUpgradeTests(unittest.TestCase):
    def test_retired_builtin_is_archived_and_custom_guides_are_preserved(self):
        import hashlib
        from squad.skills import retire_builtins
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            old=guide(root/'skills/global','old-product','historical guide')
            reference=old.parent/'references'/'local-notes.md'
            reference.parent.mkdir();reference.write_text('retained custom notes')
            digest=hashlib.sha256(old.read_bytes()).hexdigest()
            custom=guide(root/'skills/global','custom','unrelated guide')
            retire_builtins(root,{'old-product':digest})
            self.assertFalse(old.exists())
            saved=list((root/'control/retired-skills').glob('*/old-product/SKILL.md'))
            self.assertEqual(len(saved),1)
            self.assertEqual((saved[0].parent/'references/local-notes.md').read_text(),'retained custom notes')
            self.assertTrue(custom.exists())
            retire_builtins(root,{'old-product':digest})
            self.assertEqual(len(list((root/'control/retired-skills').iterdir())),1)

    def test_modified_retired_entry_requires_review(self):
        from squad.skills import retire_builtins
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);old=guide(root/'skills/global','old-product','customized entry')
            with self.assertRaisesRegex(ValueError,'customized'):
                retire_builtins(root,{'old-product':'0'*64})
            self.assertTrue(old.exists())
