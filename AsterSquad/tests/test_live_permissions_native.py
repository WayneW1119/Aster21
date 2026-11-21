"""Actual Step-boundary grant/revoke, old-process cleanup and resume via public SDK."""
import asyncio
import json
from pathlib import Path
from queue import Empty,Queue
import shlex
import sys
import tempfile
import threading
import unittest
import uuid

import toml
import tomllib
from aster import Aster, ModelResponse, SessionConfiguration
from squad.worker import policy_receipt


def call(name,args):
    return {'type':'function_call','name':name,'arguments':json.dumps(args),'call_id':uuid.uuid4().hex}


class QueueClient:
    def __init__(self,block=False):
        self.commands=Queue();self.output_for=None;self.block=block
        self.entered=threading.Event();self.release=threading.Event()
    def respond(self,prompt):
        if self.output_for:
            result=next(item['output'] for item in reversed(prompt.input) if item.get('type')=='function_call_output' and item.get('call_id')==self.output_for)
            self.output_for=None
            return ModelResponse([call('send_user_message',{'text':result,'wake_model':False})])
        try:command=self.commands.get_nowait()
        except Empty:return ModelResponse([call('wait_for_event',{})])
        if self.block:
            self.block=False;self.entered.set()
            if not self.release.wait(10):raise RuntimeError('Test model barrier not released')
        item=call('exec_command',{'cmd':command,'yield_time_ms':100 if command.startswith('sleep 2;') else 1000})
        self.output_for=item['call_id'];return ModelResponse([item])


class NativeLivePolicyTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(Path('/usr/bin/bwrap').exists(),'Linux bubblewrap required')
    async def test_grant_and_revoke_at_boundary_stop_old_process_and_survive_resume(self):
        with tempfile.TemporaryDirectory(prefix='squad-live-policy-') as temporary:
            root=Path(temporary);workspace=root/'workspace';workspace.mkdir()
            external=root/'external';external.mkdir();(external/'read.txt').write_text('reference')
            code=f'''
import json
from pathlib import Path
p=Path({str(external)!r});result={{}}
try:(p/'read.txt').read_text();result['read']=True
except OSError:result['read']=False
try:(p/'probe.txt').write_text('ok');(p/'probe.txt').unlink();result['write']=True
except OSError:result['write']=False
print(json.dumps(result))
'''
            probe=shlex.quote(sys.executable)+' -c '+shlex.quote(code)
            receipts=asyncio.Queue()
            def observe(event,data):
                if receipt:=policy_receipt(event,data):receipts.put_nowait(receipt)
            client=QueueClient(block=True);aster=Aster(root/'home')
            agent=aster.create(workspace,client=client,on_trace=observe,configuration=SessionConfiguration(max_consecutive_steps=None,filesystem_policy={'rules':[{'path':'/','access':'read'}]}))
            identity=(agent.thread_id,agent.session_id)
            async def receipt_for(request):
                while True:
                    receipt=await asyncio.wait_for(receipts.get(),15)
                    if receipt['data']['request_id']==request:
                        self.assertEqual(receipt['kind'],'filesystem_policy_applied');return receipt['data']
            async def replace(policy):
                # Public profile contract: desired config also governs resume.
                profile=tomllib.loads(agent.config_path.read_text());profile['sandbox']={'enabled':True,'format':2,'rules':policy['rules']}
                agent.config_path.write_text(toml.dumps(profile))
                return await agent.set_filesystem_policy(policy)
            async with agent.messages() as messages,agent:
                client.commands.put(probe);await agent.send('Probe old permissions')
                self.assertTrue(await asyncio.to_thread(client.entered.wait,3))
                request=await replace({'rules':[{'path':str(external),'access':'write'}]})
                self.assertTrue(receipts.empty())  # Cannot apply halfway through a model/tool Step.
                client.release.set()
                before=json.loads((await asyncio.wait_for(anext(messages),15)).text)
                self.assertEqual(json.loads(before['output'].strip()),{'read':True,'write':False})
                await receipt_for(request)
                client.commands.put(probe);await agent.send('Probe newly writable path')
                after=json.loads((await asyncio.wait_for(anext(messages),15)).text)
                self.assertEqual(json.loads(after['output'].strip()),{'read':True,'write':True})
                delayed=external/'must-not-exist.txt'
                client.commands.put('sleep 2; printf escaped > '+shlex.quote(str(delayed)))
                await agent.send('Start old-authority background command')
                pending=json.loads((await asyncio.wait_for(anext(messages),15)).text)
                self.assertIn('session_id',pending)
                request=await replace({'rules':[{'path':str(external),'access':'read'}]})
                receipt=await receipt_for(request)
                self.assertIn(pending['session_id'],receipt['stopped_command_handles'])
                await asyncio.sleep(2.1)
                self.assertFalse(delayed.exists())
                client.commands.put(probe);await agent.send('Probe revoked write')
                revoked=json.loads((await asyncio.wait_for(anext(messages),15)).text)
                self.assertEqual(json.loads(revoked['output'].strip()),{'read':True,'write':False})
                request=await replace({'rules':[{'path':str(external),'access':'deny'}]});await receipt_for(request)
                client.commands.put(probe);await agent.send('Probe deny')
                denied=json.loads((await asyncio.wait_for(anext(messages),15)).text)
                self.assertEqual(json.loads(denied['output'].strip()),{'read':False,'write':False})
                app=external/'app';app.mkdir();(app/'read.txt').write_text('app reference')
                scoped_code=f"""
import json
from pathlib import Path
p=Path({str(app)!r});result={{}}
try:(p/'read.txt').read_text();result['read']=True
except OSError:result['read']=False
try:(p/'ok').write_text('ok');(p/'ok').unlink();result['write']=True
except OSError:result['write']=False
try:Path({str(external/'read.txt')!r}).read_text();result['peer_read']=True
except OSError:result['peer_read']=False
print(json.dumps(result))
"""
                scoped_probe=shlex.quote(sys.executable)+' -c '+shlex.quote(scoped_code)
                for access,expected in [('write',{'read':True,'write':True,'peer_read':False}),('read',{'read':True,'write':False,'peer_read':False}),(None,{'read':False,'write':False,'peer_read':False})]:
                    rules=[{'path':str(external),'access':'deny'}]
                    if access:rules.append({'path':str(app),'access':access})
                    request=await replace({'rules':rules});await receipt_for(request)
                    client.commands.put(scoped_probe);await agent.send('Probe scoped live update')
                    scoped=json.loads((await asyncio.wait_for(anext(messages),15)).text)
                    self.assertEqual(json.loads(scoped['output'].strip()),expected)
                self.assertEqual((agent.thread_id,agent.session_id),identity)
            resumed_client=QueueClient();resumed=aster.resume(identity[0],client=resumed_client)
            async with resumed.messages() as messages,resumed:
                resumed_client.commands.put(probe);await resumed.send('Probe after explicit resume')
                result=json.loads((await asyncio.wait_for(anext(messages),15)).text)
                self.assertEqual(json.loads(result['output'].strip()),{'read':False,'write':False})
                self.assertEqual(resumed.thread_id,identity[0])
