"""Verify Aster's real filesystem boundary with a scripted (unpaid) model."""
import asyncio
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
import uuid

from aster import Aster, ModelResponse, SessionConfiguration
from squad.service import Service


class ProbeClient:
    def __init__(self, command):
        self.command, self.phase = command, 0

    def respond(self, prompt):
        if self.phase == 0:
            self.phase = 1
            name, args = "exec_command", {"cmd": self.command}
        elif self.phase == 1:
            self.phase = 2
            result = next(i["output"] for i in reversed(prompt.input) if i.get("type") == "function_call_output")
            name, args = "send_user_message", {"text": result, "wake_model": False}
        else:
            name, args = "wait_for_event", {}
        return ModelResponse([{"type": "function_call", "name": name, "call_id": uuid.uuid4().hex, "arguments": json.dumps(args)}])


class SandboxTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(Path("/usr/bin/bwrap").exists(), "Linux bubblewrap required")
    async def test_new_write_directory_does_not_grant_parent_and_deny_blocks_reads(self):
        with tempfile.TemporaryDirectory(prefix="squad-new-path-") as temporary:
            root=Path(temporary)
            service=Service(root/'squad',mode='real',relay_url='http://127.0.0.1:1',
                relay_mcp='http://127.0.0.1:2/mcp',squad_mcp='http://127.0.0.1:3/mcp',
                owner_relay_token='test',domain='test',user_account='test',models=['scripted'],model_url='https://example.invalid/responses')
            self.addCleanup(service.store.db.close)
            external=root/'external';external.mkdir()
            secret=external/'private.txt';secret.write_text('hidden')
            target=external/'new'/'nested'
            record,_=service.store.create('probe',None,0,{'instruction':'Test','model':'scripted','grants':[]},'test')
            grants=service.grants('owner',[{'path':str(target),'access':'write'},{'path':str(secret),'access':'deny'}])
            service.materialize_grants(grants)
            service.store.update(record['id'],config={**record['config'],'grants':grants})
            code=f"""
from pathlib import Path
Path({str(target/'ok.txt')!r}).write_text('ok')
for operation in [lambda:Path({str(external/'blocked.txt')!r}).write_text('bad'),lambda:Path({str(secret)!r}).read_text()]:
    try:operation()
    except OSError:pass
    else:raise RuntimeError('Unexpected filesystem access')
print('permission-boundary-ok')
"""
            agent=Aster(service.aster_home(record)).create(service.workspace(record),
                client=ProbeClient(shlex.quote(sys.executable)+' -c '+shlex.quote(code)),name='new-path-probe',
                configuration=SessionConfiguration(max_consecutive_steps=None,filesystem_policy={'rules':service.rules(service.store.agent(record['id']))}))
            async with agent.messages() as messages,agent:
                await agent.send('Verify filesystem boundaries')
                result=json.loads((await asyncio.wait_for(anext(messages),15)).text)
                self.assertEqual(result['exit_code'],0,result)
                self.assertIn('permission-boundary-ok',result['output'])
            self.assertEqual((target/'ok.txt').read_text(),'ok')
            self.assertFalse((external/'blocked.txt').exists())

    @unittest.skipUnless(Path("/usr/bin/bwrap").exists(), "Linux bubblewrap required")
    async def test_real_squad_program_generator_can_write_its_private_state(self):
        with tempfile.TemporaryDirectory(prefix="squad-source-sandbox-") as temporary:
            root=Path(temporary)
            service=Service(root,mode="real",relay_url="http://127.0.0.1:1",relay_mcp="http://127.0.0.1:2/mcp",squad_mcp="http://127.0.0.1:3/mcp",
                owner_relay_token="test",domain="test",user_account="test",models=["scripted"],model_url="https://example.test/responses")
            record,_=service.store.create("probe",None,0,{"instruction":"Test","model":"scripted","grants":[]},"test")
            workspace=service.workspace(record)
            script=workspace/"source.py"
            script.write_text("from aster.events import emit_event,state_directory\n(state_directory()/'probe.txt').write_text('ok')\nemit_event('probe_completed',{'ok':True})\n")

            class SourceClient:
                first=True
                delivered=False
                def respond(self,prompt):
                    if self.first:
                        self.first=False;name,args="register_program_generator",{"script_path":"source.py","mode":"once"}
                    elif not self.delivered and any("probe_completed" in i.get("text","") for i in prompt.input):
                        self.delivered=True;name,args="send_user_message",{"text":"program-state-ok","wake_model":False}
                    else:
                        name,args="wait_for_event",{}
                    return ModelResponse([{"type":"function_call","call_id":uuid.uuid4().hex,"name":name,"arguments":json.dumps(args)}])

            agent=Aster(service.aster_home(record)).create(workspace,client=SourceClient(),name="source-probe",
                configuration=SessionConfiguration(max_consecutive_steps=None,filesystem_policy={"rules":service.rules(record)}))
            try:
                async with agent.messages() as messages,agent:
                    await agent.send("Start the source")
                    result=await asyncio.wait_for(anext(messages),15)
                    self.assertEqual(result.text,"program-state-ok")
                self.assertTrue(list(agent.thread_path.glob("programs/*/state/probe.txt")))
            finally:
                service.store.db.close()

    @unittest.skipUnless(Path("/usr/bin/bwrap").exists(), "Linux bubblewrap required")
    async def test_own_connection_access_other_secrets_denied_and_external_write_denied(self):
        with tempfile.TemporaryDirectory(prefix="squad-sandbox-") as temporary:
            root = Path(temporary)
            private, workspace, peer, external = [root / name for name in ["control", "own", "peer", "external"]]
            for folder in (private, workspace, peer, external):
                folder.mkdir()
            (private / "owner.token").write_text("hidden")
            (peer / "relay.json").write_text("hidden")
            (workspace / "relay.json").write_text("own")
            (external / "read.txt").write_text("readable")
            code = f"""
import json,os
from pathlib import Path
checks={{}}
checks['own_connection']=Path({str(workspace / 'relay.json')!r}).read_text()=='own'
for key,path in [('controller',{str(private / 'owner.token')!r}),('peer',{str(peer / 'relay.json')!r})]:
    try:Path(path).read_text();checks[key]=False
    except (PermissionError,FileNotFoundError,IsADirectoryError):checks[key]=True
checks['external_read']=Path({str(external / 'read.txt')!r}).read_text()=='readable'
try:Path({str(external / 'blocked.txt')!r}).write_text('bad');checks['external_write_blocked']=False
except OSError:checks['external_write_blocked']=True
Path({str(workspace / 'allowed.txt')!r}).write_text('ok')
checks['own_write']=True
checks['provider_key_filtered']='SQUAD_MODEL_KEY' not in os.environ
print(json.dumps(checks))
"""
            client = ProbeClient(shlex.quote(sys.executable) + " -c " + shlex.quote(code))
            agent = Aster(private / "aster").create(workspace, client=client, name="sandbox-probe",
                configuration=SessionConfiguration(max_consecutive_steps=None, filesystem_policy={"rules": [
                    {"path": "/", "access": "read"},
                    {"path": str(private), "access": "deny"}, {"path": str(peer), "access": "deny"}]}))
            previous = os.environ.get("SQUAD_MODEL_KEY")
            os.environ["SQUAD_MODEL_KEY"] = "test-only-sentinel"
            try:
                async with agent.messages() as messages, agent:
                    await agent.send("Run the filesystem probe")
                    message = await asyncio.wait_for(anext(messages), 15)
                    result = json.loads(message.text)
                    self.assertEqual(result["exit_code"], 0, result)
                    checks = json.loads(result["output"].strip())
                    self.assertTrue(all(checks.values()), checks)
            finally:
                if previous is None:
                    os.environ.pop("SQUAD_MODEL_KEY", None)
                else:
                    os.environ["SQUAD_MODEL_KEY"] = previous


    @unittest.skipUnless(Path('/usr/bin/bwrap').exists(),'Linux bubblewrap required')
    async def test_compact_data_mask_hides_peers_created_after_process_start(self):
        with tempfile.TemporaryDirectory(prefix='squad-mask-') as temporary:
            root=Path(temporary)
            service=Service(root/'squad',mode='real',relay_url='http://127.0.0.1:1',relay_mcp='http://127.0.0.1:2/mcp',squad_mcp='http://127.0.0.1:3/mcp',owner_relay_token='test',domain='test',user_account='test',models=['scripted'],model_url='https://example.invalid/responses')
            self.addCleanup(service.store.db.close)
            record,_=service.store.create('probe',None,0,{'instruction':'Test','model':'scripted','grants':[]},'test')
            work=service.workspace(record);connection,_=service.connection_paths(record)
            connection.write_text('own connection')
            (service.private/'secret').write_text('controller secret')
            peer=service.slots[1];(peer/'secret').write_text('peer secret')
            future=service.root/'workspaces/slot-99';ready=work/'ready';go=work/'continue'
            client=ProbeClient('')
            rules=service.rules(record);self.assertLess(len(rules),10)
            agent=Aster(service.aster_home(record)).create(work,client=client,configuration=SessionConfiguration(max_consecutive_steps=None,filesystem_policy={'rules':rules}))
            script=f'''
import time
from pathlib import Path
assert Path({str(connection)!r}).read_text()=='own connection'
Path({str(ready)!r}).touch()
while not Path({str(go)!r}).exists():time.sleep(.01)
for name in [{str(peer/'secret')!r},{str(future/'secret')!r},{str(service.private/'secret')!r},{str(agent.thread_path/'rollout.jsonl')!r}]:
    try:Path(name).read_text()
    except OSError:pass
    else:raise RuntimeError('Private path exposed')
Path({str(work/'own.txt')!r}).write_text('ok')
print('compact-isolation-ok')
'''
            client.command=shlex.quote(sys.executable)+' -c '+shlex.quote(script)
            async with agent.messages() as messages,agent:
                await agent.send('Probe compact isolation')
                for _ in range(250):
                    if ready.exists():break
                    await asyncio.sleep(.01)
                self.assertTrue(ready.exists())
                future.mkdir();(future/'secret').write_text('new peer secret');go.touch()
                result=json.loads((await asyncio.wait_for(anext(messages),15)).text)
                self.assertEqual(result['exit_code'],0,result)
                self.assertIn('compact-isolation-ok',result['output'])


if __name__ == "__main__":
    unittest.main()
