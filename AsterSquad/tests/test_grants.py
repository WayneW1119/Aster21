"""Path grants, missing-directory preparation and delegation boundaries."""
import json
from pathlib import Path
import tempfile
import unittest

from relay.store import Problem
from squad.service import Service


class GrantTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='squad-grants-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.service=Service(self.root/'squad',mode='fake',relay_url='http://127.0.0.1:1',
            relay_mcp='http://127.0.0.1:2/mcp',squad_mcp='http://127.0.0.1:3/mcp',
            owner_relay_token='test',domain='test',user_account='test',models=['fake'],model_url='https://example.invalid/responses')
        self.addCleanup(self.service.store.db.close)
        self.external=self.root/'external';self.external.mkdir()

    def member(self,handle='parent',slot=0,parent=None,grants=None):
        record,_=self.service.store.create(handle,parent,slot,{'instruction':'Test','model':'fake','grants':grants or []},'test')
        p=self.service.credentials(record['id']);p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(json.dumps({'relay':'test','squad':'test'}))
        return record

    def test_new_write_directory_only_created_when_applied(self):
        path=self.external/'new'/'nested'
        grants=self.service.grants('owner',[{'path':str(path),'access':'write'}])
        self.assertFalse(path.exists())
        self.service.materialize_grants(grants)
        self.assertTrue(path.is_dir())
        self.assertEqual(grants,[{'path':str(path),'access':'write'}])
        self.assertFalse((self.external/'unrelated').exists())
        self.service.materialize_grants(grants)  # Idempotent on resume.

    def test_read_and_deny_require_existing_paths_and_existing_file_is_supported(self):
        for access in ('read','deny'):
            with self.subTest(access=access),self.assertRaises(Problem):
                self.service.grants('owner',[{'path':str(self.external/'missing'),'access':access}])
        file=self.external/'file.txt';file.write_text('old')
        self.service.materialize_grants(self.service.grants('owner',[{'path':str(file),'access':'write'}]))
        self.assertEqual(file.read_text(),'old')

    def test_invalid_or_protected_paths_are_rejected_before_creation(self):
        for path in ('relative',str(self.external/'..'/'elsewhere'),str(self.service.private/'missing'),str(self.service.root/'runtime/new')):
            with self.subTest(path=path),self.assertRaises(Problem):
                self.service.grants('owner',[{'path':path,'access':'write'}])
        with self.assertRaises(Problem):
            self.service.grants('owner',[{'path':str(self.external/'new'),'access':'write'}, {'path':str(self.external/'new'),'access':'read'}])
        self.assertFalse((self.external/'new').exists())

    def test_symlinks_cannot_redirect_new_directory_preparation(self):
        link=self.root/'link';link.symlink_to(self.external,target_is_directory=True)
        with self.assertRaises(Problem):self.service.grants('owner',[{'path':str(link/'new'),'access':'write'}])
        path=self.external/'new';grants=self.service.grants('owner',[{'path':str(path),'access':'write'}])
        # Even replacement after validation must not be followed during mkdir.
        self.external.rmdir();self.external.symlink_to(self.service.private,target_is_directory=True)
        with self.assertRaises(Problem):self.service.materialize_grants(grants)
        self.assertFalse((self.service.private/'new').exists())

    def test_deny_can_restrict_workspace_subfolder_and_never_expose_peer(self):
        record=self.member();sub=self.service.workspace(record)/'secret';sub.mkdir()
        grants=self.service.grants('owner',[{'path':str(sub),'access':'deny'}],workspace=self.service.workspace(record))
        self.service.store.update(record['id'],config={**record['config'],'grants':grants})
        rules=self.service.rules(self.service.store.agent(record['id']))
        self.assertIn({'path':str(sub),'access':'deny'},rules)
        with self.assertRaises(Problem):self.service.grants('owner',[{'path':str(self.service.workspace(record)),'access':'deny'}],workspace=self.service.workspace(record))
        peer=self.service.slots[1]
        self.service.store.update(record['id'],config={**record['config'],'grants':[{'path':str(peer),'access':'deny'}]})
        self.assertEqual(sum(r['path']==str(peer) for r in self.service.rules(self.service.store.agent(record['id']))),1)

    def test_delegation_respects_nested_readonly_and_deny(self):
        readonly=self.external/'readonly';readonly.mkdir()
        secret=self.external/'secret';secret.mkdir()
        grants=[{'path':str(self.external),'access':'write'},{'path':str(readonly),'access':'read'},{'path':str(secret),'access':'deny'}]
        parent=self.member(grants=grants)
        for path,access in ((readonly,'write'),(secret,'read'),(self.external,'write'),(self.external,'read')):
            with self.subTest(path=path,access=access),self.assertRaises(Problem):
                self.service.grants(parent['id'],[{'path':str(path),'access':access}])
        self.service.grants(parent['id'],[{'path':str(self.external/'new'),'access':'write'}])
        self.service.grants(parent['id'],[{'path':str(readonly),'access':'read'}])

    async def test_adding_parent_restriction_cannot_leave_child_authority(self):
        secret=self.external/'secret';secret.mkdir()
        parent=self.member(grants=[{'path':str(self.external),'access':'write'}])
        self.member('child',1,parent['id'],[{'path':str(self.external),'access':'write'}])
        with self.assertRaises(Problem):
            await self.service.configure('owner',parent['id'],grants=[{'path':str(self.external),'access':'write'},{'path':str(secret),'access':'deny'}])
        self.assertEqual(self.service.store.agent(parent['id'])['config']['grants'],parent['config']['grants'])

    async def test_parent_adjusts_own_scope_while_preserving_owner_grants(self):
        garden=self.external/'garden';garden.mkdir()
        finance=self.external/'finance';finance.mkdir()
        exchange=garden/'exchange';exchange.mkdir()
        mailbox=exchange/'finance'
        parent=self.member(grants=[{'path':str(garden),'access':'write'}])
        independent={'path':str(finance),'access':'write'}
        child=self.member('child',1,parent['id'],[independent])
        scoped=[{'path':str(exchange),'access':'deny'},{'path':str(mailbox),'access':'write'}]
        result=await self.service.configure(parent['id'],child['id'],grants=[independent,*scoped])
        self.assertEqual(result['config']['grants'],[independent,*scoped])
        self.assertTrue(mailbox.is_dir())
        # Narrow and remove the newly assigned authority without touching finance.
        scoped[-1]={**scoped[-1],'access':'read'}
        await self.service.configure(parent['id'],child['id'],grants=[independent,*scoped])
        await self.service.configure(parent['id'],child['id'],grants=[independent])
        # Neither deleting, narrowing, nor expanding an independent grant is allowed.
        restricted=finance/'restricted';restricted.mkdir()
        for candidate in ([],[{**independent,'access':'read'}],
                          [independent,{'path':str(restricted),'access':'read'}],
                          [independent,{'path':str(finance),'access':'deny'}]):
            with self.subTest(candidate=candidate),self.assertRaises(Problem):
                await self.service.configure(parent['id'],child['id'],grants=candidate)
        self.assertEqual(self.service.store.agent(child['id'])['config']['grants'],[independent])
        self.assertFalse((finance/'new').exists())

        # A redundant rule preserves effective authority, but cannot be used
        # to create directories outside the configuring parent's scope.
        redundant=[independent,{'path':str(finance/'new'),'access':'write'}]
        with self.assertRaises(Problem):
            await self.service.configure(parent['id'],child['id'],grants=redundant)
        self.assertFalse((finance/'new').exists())

    async def test_parent_read_scope_cannot_revoke_independent_write(self):
        parent=self.member(grants=[{'path':str(self.external),'access':'read'}])
        write={'path':str(self.external),'access':'write'}
        child=self.member('child',1,parent['id'],[write])
        for candidate in ([],[{**write,'access':'read'}],[write,{'path':str(self.external/'nested'),'access':'deny'}]):
            (self.external/'nested').mkdir(exist_ok=True)
            with self.subTest(candidate=candidate),self.assertRaises(Problem):
                await self.service.configure(parent['id'],child['id'],grants=candidate)

    async def test_parent_cannot_remove_out_of_scope_deny_or_cross_nested_restriction(self):
        app=self.external/'app';app.mkdir()
        secret=self.external/'secret';secret.mkdir()
        permitted=[{'path':str(self.external),'access':'deny'},{'path':str(app),'access':'write'}]
        parent=self.member(grants=permitted)
        child=self.member('child',1,parent['id'],[{'path':'/','access':'read'},{'path':str(secret),'access':'deny'}])
        with self.assertRaises(Problem):
            await self.service.configure(parent['id'],child['id'],grants=[{'path':'/','access':'read'}])
        # An unchanged external deny does not prevent scoped edits elsewhere.
        before=child['config']['grants']
        await self.service.configure(parent['id'],child['id'],grants=[*before,{'path':str(app),'access':'write'}])
        with self.assertRaises(Problem):
            await self.service.configure(parent['id'],child['id'],grants=[*before,{'path':str(self.external),'access':'write'}])

    async def test_parent_updates_preserve_children_independent_grants(self):
        garden=self.external/'garden';garden.mkdir()
        finance=self.external/'finance';finance.mkdir()
        parent=self.member(grants=[{'path':str(garden),'access':'write'}])
        child=self.member('child',1,parent['id'],[{'path':str(finance),'access':'write'}])
        # An unrelated grant from the user does not block the parent's edits.
        readonly=garden/'readonly';readonly.mkdir()
        await self.service.configure('owner',parent['id'],grants=[*parent['config']['grants'],{'path':str(readonly),'access':'read'}])
        # Actual narrowing below a delegated child's authority still needs child-first revocation.
        await self.service.configure(parent['id'],child['id'],grants=[*child['config']['grants'],{'path':str(garden),'access':'read'}])
        with self.assertRaises(Problem):
            await self.service.configure('owner',parent['id'],grants=[])

    async def test_scope_checks_semantic_changes_instead_of_changed_rule_rows(self):
        garden=self.external/'garden';garden.mkdir()
        secret=self.external/'secret';secret.mkdir()
        grants=[{'path':str(self.external),'access':'write'},{'path':str(secret),'access':'deny'}]
        parent=self.member(grants=grants)
        child=self.member('child',1,parent['id'],grants)
        # Reordering and redundant rules do not change authority outside the parent's scope.
        await self.service.configure(parent['id'],child['id'],grants=[*reversed(grants),{'path':str(garden),'access':'write'}])
        # Dropping the nested deny silently opens a region the parent does not own.
        with self.assertRaises(Problem):
            await self.service.configure(parent['id'],child['id'],grants=[{'path':str(self.external),'access':'write'}])
        # A new read rule cannot manufacture explicit delegation authority from default OS readability.
        elsewhere=self.root/'elsewhere';elsewhere.mkdir()
        with self.assertRaises(Problem):
            await self.service.configure(parent['id'],child['id'],grants=[*grants,{'path':str(elsewhere),'access':'read'}])

    def test_explicit_readonly_workspace_does_not_imply_write_delegation(self):
        parent=self.member()
        workspace=self.service.workspace(parent)
        readonly=[{'path':str(workspace),'access':'read'}]
        self.service.store.update(parent['id'],config={**parent['config'],'grants':readonly})
        self.service.grants(parent['id'],readonly)
        with self.assertRaises(Problem):
            self.service.grants(parent['id'],[{'path':str(workspace),'access':'write'}])

    async def test_configure_missing_directory_and_three_permission_groups(self):
        record=self.member();readonly=self.external/'reference';readonly.mkdir()
        denied=self.external/'private';denied.mkdir()
        new=self.external/'new'/'project'
        result=await self.service.configure('owner',record['id'],grants=[{'path':str(new),'access':'write'},{'path':str(readonly),'access':'read'},{'path':str(denied),'access':'deny'}])
        self.assertTrue(new.is_dir())
        self.assertEqual({g['access'] for g in result['config']['grants']},{'read','write','deny'})
        self.assertTrue(any(self.service.private.is_relative_to(Path(p)) for p in result['filesystem']['protected_paths']))
        await self.service.configure('owner',record['id'],grants=[])
        self.assertTrue(new.exists())  # Revoking access never deletes user data.
        self.assertEqual(self.service.store.agent(record['id'])['config']['grants'],[])


    def test_parent_can_delegate_reopened_subtree_and_matching_policy_but_not_escalate(self):
        app=self.external/'app';app.mkdir();private=app/'private';private.mkdir()
        grants=[{'path':str(self.external),'access':'deny'},{'path':str(app),'access':'write'},{'path':str(private),'access':'read'}]
        parent=self.member(grants=grants)
        self.assertEqual(self.service.grants(parent['id'],grants),grants)
        self.service.grants(parent['id'],[{'path':str(app/'new'),'access':'write'},{'path':str(private),'access':'read'}])
        for requested in ([{'path':str(app),'access':'write'}],[{'path':str(private),'access':'write'}],[{'path':str(self.external),'access':'read'}]):
            with self.assertRaises(Problem):self.service.grants(parent['id'],requested)

    def test_platform_policy_size_does_not_grow_with_reserved_member_slots(self):
        record=self.member()
        rules=self.service.rules(record)
        self.assertLess(len(rules),10)
        self.assertIn({'path':str(self.service.root),'access':'deny'},rules)
        self.assertIn({'path':str(self.service.workspace(record)),'access':'write'},rules)
        with self.assertRaises(Problem):
            self.service.grants('owner',[{'path':str(self.service.private/'secret'),'access':'read'}])
