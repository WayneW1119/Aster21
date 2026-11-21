"""Real killed process, preserved effects, queue recovery and refusal boundaries."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from aster import Aster, ModelResponse
from agent_harness.core.client_common import RetryableModelError, ResponseEvent
from agent_harness.core.response_stream import collect_response

SCRIPT = '''
import asyncio,json,time
from pathlib import Path
from aster import Aster,SessionConfiguration,ModelResponse
root=Path(__file__).parent
class Client:
    n=0
    def respond(self,prompt):
        self.n+=1
        if self.n==1:
            return ModelResponse([{'type':'function_call','name':'exec_command','call_id':'effect','arguments':json.dumps({'cmd':'printf x >> effect.txt'})}])
        (root/'blocked').touch()
        time.sleep(300)
async def main():
    agent=Aster(root/'home').create(root/'work',client=Client(),trace=True,configuration=SessionConfiguration(filesystem_policy=None,max_consecutive_steps=None))
    (root/'thread').write_text(agent.thread_id)
    async with agent:
        await agent.send('perform one effect')
        while not (root/'enqueue').exists():await asyncio.sleep(.01)
        await agent.emit('late_event',{'note':'preserve this event'})
        (root/'enqueued').touch()
        await agent.wait()
asyncio.run(main())
'''


class CrashRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'work').mkdir()
        script=self.root/'crash.py';script.write_text(SCRIPT)
        self.proc=subprocess.Popen([sys.executable,str(script)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_process)
        await self.wait_file('blocked')
        (self.root/'enqueue').touch();await self.wait_file('enqueued')
        self.stop_process()
        self.thread=(self.root/'thread').read_text()
        self.folder=self.root/'home/threads'/self.thread
        self.path=self.folder/'rollout.jsonl';self.original=self.path.read_bytes()
        self.harness=Aster(self.root/'home')

    def stop_process(self):
        if self.proc.poll() is None:self.proc.kill()
        self.proc.wait(timeout=5)

    async def wait_file(self,name):
        for _ in range(500):
            if (self.root/name).exists():return
            if self.proc.poll() is not None:self.fail('Fixture exited before boundary')
            await asyncio.sleep(.01)
        self.fail('Fixture did not reach boundary')

    async def resume_and_check(self):
        observed=[]
        class Continued:
            def respond(self,prompt):
                observed.append(prompt.input)
                return ModelResponse([{'type':'function_call','name':'send_user_message','call_id':'continued','arguments':json.dumps({'text':'continued','wake_model':False})}])
        agent=self.harness.resume(self.thread,client=Continued())
        async with agent.messages() as messages,agent:
            self.assertEqual((await asyncio.wait_for(anext(messages),5)).text,'continued')
        self.assertEqual((self.root/'work/effect.txt').read_text(),'x')
        self.assertEqual(sum('preserve this event' in json.dumps(i) for i in observed[0]),1)

    async def test_native_boundary_recovers_uncommitted_model_and_pending_event_without_replay(self):
        result=self.harness.recover(self.thread,reason='Recover killed request at durable boundary')
        self.assertEqual(result['source'],'boundary');self.assertEqual(result['pending_events'],1)
        self.assertTrue(self.path.read_bytes().startswith(self.original))
        self.assertFalse(self.harness.recover(self.thread,reason='Idempotent')['recovered'])
        await self.resume_and_check()

    def legacy(self):
        rows=[json.loads(line) for line in self.original.splitlines()]
        rows=[r for r in rows if r['type']!='step_boundary' and not (r['type']=='runtime_checkpoint' and 'boundary' in r['payload'])]
        self.path.write_text(''.join(json.dumps(r)+'\n' for r in rows))

    async def test_explicit_legacy_trace_proves_same_archive_and_queue(self):
        self.legacy();before=self.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'allow_trace'):
            self.harness.recover(self.thread,reason='Trace is not automatic')
        result=self.harness.recover(self.thread,reason='Use complete matching legacy evidence',allow_trace=True)
        self.assertEqual(result['source'],'trace');self.assertEqual(result['pending_events'],1)
        self.assertTrue(self.path.read_bytes().startswith(before))
        await self.resume_and_check()

    def test_unresolved_tool_and_partial_archive_are_refused_without_changes(self):
        call={'timestamp':'test','type':'response_item','payload':{'type':'function_call','call_id':'unknown','name':'exec_command','arguments':'{}'}}
        for damaged in (self.original[:-1],self.original+(json.dumps(call)+'\n').encode()):
            self.path.write_bytes(damaged)
            with self.assertRaises(ValueError):self.harness.recover(self.thread,reason='Must refuse')
            self.assertEqual(self.path.read_bytes(),damaged)

    def test_missing_trace_events_or_tampered_archive_are_refused(self):
        self.legacy()
        trace=next((self.folder/'traces').glob('sdk*.jsonl'))
        rows=[json.loads(line) for line in trace.read_text().splitlines()]
        removed=False;changed=[]
        for row in rows:
            if row['event']=='event_enqueued' and not removed:removed=True;continue
            changed.append(row)
        trace.write_text(''.join(json.dumps(row)+'\n' for row in changed))
        before=self.path.read_bytes()
        with self.assertRaises(ValueError):self.harness.recover(self.thread,reason='Missing queue evidence',allow_trace=True)
        self.assertEqual(self.path.read_bytes(),before)
        self.path.write_bytes(self.original)
        self.harness.recover(self.thread,reason='Valid native recovery')
        rows=[json.loads(line) for line in self.path.read_text().splitlines()]
        rows[-1]['payload']['archive_hash']='tampered'
        self.path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        with self.assertRaises(ValueError):self.harness.resume(self.thread,client=None)


class EmptyResumeRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_interrupted_resume_preserves_history_and_queue(self):
        from uuid import uuid4
        from aster import SessionConfiguration
        from agent_harness.core.rollout.recorder import RolloutRecorder
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); work = root/'work'; work.mkdir()
            harness = Aster(root/'home')
            class IdleClient:
                def respond(self, prompt):
                    raise AssertionError('Recovery must not call a model')
            client = IdleClient()
            agent = harness.create(work, client=client, configuration=SessionConfiguration(filesystem_policy=None))
            async with agent:
                pass
            path = agent.thread_path/'rollout.jsonl'
            original = path.read_bytes()
            history = RolloutRecorder._read(original.decode(), agent.thread_id)
            opened = {'timestamp': 'test', 'type': 'session_opened',
                      'payload': {'session_id': str(uuid4()), 'resumed': True}}
            interrupted = original + (json.dumps(opened)+'\n').encode()
            path.write_bytes(interrupted)
            result = harness.recover(agent.thread_id, reason='Resume interrupted before initialization')
            self.assertTrue(result['recovered'])
            self.assertTrue(path.read_bytes().startswith(interrupted))
            restored = RolloutRecorder._read(path.read_text(), agent.thread_id)
            self.assertEqual(restored.history.snapshot(), history.history.snapshot())
            self.assertEqual(restored.pending_events, history.pending_events)
            self.assertEqual(restored.generators, history.generators)
            resumed = harness.resume(agent.thread_id, client=client)
            async with resumed:
                pass
            self.assertEqual(resumed.thread_id, agent.thread_id)
            # Any activity after opening, or a non-resume, must fail closed.
            for damaged in (interrupted + (json.dumps({'timestamp': 'test', 'type': 'event_queued', 'payload': {}})+'\n').encode(),
                            original + (json.dumps({**opened, 'payload': {**opened['payload'], 'resumed': False}})+'\n').encode()):
                path.write_bytes(damaged)
                with self.assertRaises(ValueError):
                    harness.recover(agent.thread_id, reason='Not an empty resumed Session')
                self.assertEqual(path.read_bytes(), damaged)


class TransientStreamTests(unittest.TestCase):
    def test_eof_is_retryable_but_explicit_provider_failure_is_not(self):
        with self.assertRaises(RetryableModelError):collect_response([])
        with self.assertRaises(RuntimeError) as error:
            collect_response([ResponseEvent('failed',{'error':{'code':'insufficient_quota'}})])
        self.assertNotIsInstance(error.exception,RetryableModelError)


class StreamRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_tool_stream_retries_before_any_effect_is_committed(self):
        from aster import SessionConfiguration
        class Client:
            n=0
            def respond(self,prompt):
                self.n+=1
                effect={'type':'function_call','name':'exec_command','call_id':'effect','arguments':json.dumps({'cmd':'printf x >> effect.txt'})}
                if self.n==1:
                    collect_response([ResponseEvent('output_item_done',{'output_index':0,'item':effect})])
                if self.n==2:
                    collect_response([ResponseEvent('output_item_done',{'output_index':0,'item':effect}),
                        ResponseEvent('failed',{'type':'error','error':{'code':'server_is_overloaded'}})])
                if self.n==3:return ModelResponse([effect])
                return ModelResponse([{'type':'function_call','name':'send_user_message','call_id':'answer','arguments':json.dumps({'text':'finished','wake_model':False})}])
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);work=root/'work';work.mkdir()
            agent=Aster(root/'home').create(work,client=Client(),configuration=SessionConfiguration(filesystem_policy=None,max_consecutive_steps=None))
            async with agent.messages() as messages,agent:
                await agent.send('one effect')
                self.assertEqual((await asyncio.wait_for(anext(messages),8)).text,'finished')
            self.assertEqual((work/'effect.txt').read_text(),'x')
            rows=[json.loads(line) for line in (agent.thread_path/'rollout.jsonl').read_text().splitlines()]
            self.assertEqual(sum(r['type']=='response_item' and r['payload'].get('type')=='function_call' and r['payload'].get('call_id')=='effect' for r in rows),1)


class TransportClassificationTests(unittest.TestCase):
    def test_temporary_http_error_preserves_retry_after(self):
        from urllib.error import HTTPError
        from unittest.mock import patch
        from agent_harness.core.client import ResponsesClient
        from agent_harness.core.client_common import Prompt
        client = ResponsesClient('https://example.test/responses', 'test', 'secret')
        error = HTTPError(client.endpoint, 503, 'secret', {'Retry-After': '328'}, None)
        with patch('agent_harness.core.client.urlopen', side_effect=error):
            with self.assertRaises(RetryableModelError) as caught:
                client.respond(Prompt('test', [], []))
        self.assertEqual(caught.exception.retry_after_seconds, 328)
        self.assertNotIn('secret', str(caught.exception))

    def test_network_and_temporary_http_errors_retry_but_quota_auth_and_tls_do_not(self):
        import io
        from ssl import SSLCertVerificationError
        from urllib.error import HTTPError,URLError
        from unittest.mock import patch
        from agent_harness.core.client import ResponsesClient
        from agent_harness.core.client_common import Prompt
        client=ResponsesClient('https://example.test/responses','test','secret')
        cases=[(URLError('temporary DNS failure'),True),(URLError(SSLCertVerificationError('invalid certificate')),False)]
        for code,body,retry in [(503,{},True),(520,'secret upstream error page',True),(599,{},False),(429,{'error':{'code':'rate_limit_exceeded'}},True),(429,{'error':{'code':'insufficient_quota'}},False),(401,{},False),(400,{},False)]:
            cases.append((HTTPError(client.endpoint,code,'error',{},io.BytesIO(json.dumps(body).encode())),retry))
        for error,retry in cases:
            with self.subTest(error=type(error).__name__,retry=retry),patch('agent_harness.core.client.urlopen',side_effect=error):
                with self.assertRaises(RuntimeError) as raised:client.respond(Prompt('test',[],[]))
                self.assertEqual(isinstance(raised.exception,RetryableModelError),retry)
                self.assertNotIn('secret',str(raised.exception))


class OnceRecoveryTests(unittest.TestCase):
    def test_uncertain_once_source_is_retained_but_never_restarted(self):
        from agent_harness.core.rollout.recovery import stop_uncertain_once
        def source(ident,mode):
            return {'record':{'generator_id':'gen_'+ident*32,'kind':'program','status':'active','configuration':{'script_path':'/tmp/source.py','cwd':'/tmp','args':[],'mode':mode},'events_emitted':0,'created_at':'test'},'state':None}
        checkpoint={'version':1,'pending_events':[],'generators':[source('a','once'),source('b','service')]}
        for n in range(256):
            old = source('c', 'once')
            old['record'].update(status='completed', generator_id=f'gen_{n:032x}')
            checkpoint['generators'].append(old)
        result=stop_uncertain_once(checkpoint)
        self.assertEqual(result['generators'][0]['record']['status'],'failed')
        self.assertEqual(len(result['generators']), 257)
        self.assertIn('unknown',result['generators'][0]['record']['error'])
        self.assertEqual(result['generators'][1]['record']['status'],'active')
