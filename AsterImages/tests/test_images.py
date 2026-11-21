import asyncio
import base64
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer
from PIL import Image
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from aster_images.runner import Runner, ImageError, image_info, request_data
from aster_images.server import backend, build


def png():
    out=io.BytesIO();Image.new('RGB',(12,12),'red').save(out,format='PNG');return out.getvalue()


class ImagesTests(unittest.IsolatedAsyncioTestCase):
    async def test_reject_bad_inputs_before_start_and_busy(self):
        for body,action in [({'prompt':''},'generate'),({'prompt':'ok','images':[]},'edit'),({'prompt':'x','transparent_background':'false'},'generate'),({'prompt':'x','images':['bad']},'edit')]:
            with self.assertRaises(ImageError):request_data(body,action)
        with tempfile.TemporaryDirectory() as tmp:
            runner=Runner(tmp);runner._run=AsyncMock(return_value={'ok':True})
            async with runner.lock:
                with self.assertRaises(ImageError) as caught:await runner.run('generate',{'prompt':'x'})
                self.assertEqual(caught.exception.code,'busy')
            runner._run.assert_not_called()

    async def test_runner_uses_native_thread_file_not_claimed_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);home=root/'codex';home.mkdir();fake=root/('fake-codex.py' if os.name == 'nt' else 'fake-codex')
            payload=base64.b64encode(png()).decode()
            fake.write_text('#!'+sys.executable+'\n'+f'''import os,json,base64,sys
from pathlib import Path
if sys.argv[1:]==['login','status']:
 print('Logged in using ChatGPT');sys.exit(0)
home=Path(os.environ['CODEX_HOME']);ident='01234567-0123-0123-0123-0123456789ab'
assert 'OPENAI_API_KEY' not in os.environ and 'CODEX_API_KEY' not in os.environ
assert '--ephemeral' in sys.argv and '--ignore-user-config' in sys.argv
args=sys.argv;assert args[args.index('--sandbox')+1]=='read-only'
assert args.count('--disable')==3
out=home/'generated_images'/ident;out.mkdir(parents=True)
(out/'exec-native.png').write_bytes(base64.b64decode('{payload}'))
print(json.dumps({{'type':'thread.started','thread_id':ident}}))
print(json.dumps({{'type':'item.completed','item':{{'type':'agent_message','text':'/etc/private.png'}}}}))
print(json.dumps({{'type':'turn.completed'}}))
''');fake.chmod(0o700)
            runner=Runner(root/'jobs',codex=str(fake),codex_home=home)
            result=await runner.run('generate',{'prompt':'mug'})
            self.assertEqual(base64.b64decode(result['image']),png())
            self.assertEqual(result['width'],12)
            self.assertTrue((root/'jobs'/result['id']/'receipt.json').exists())
            # A nominally successful opaque image cannot satisfy transparency.
            import shutil
            shutil.rmtree(home/'generated_images')
            with self.assertRaises(ImageError) as caught:
                await runner.run('generate',{'prompt':'transparent mug','transparent_background':True})
            self.assertEqual(caught.exception.code,'requirement_unmet')
            self.assertEqual(len(list((root/'jobs').iterdir())),2)

    async def test_missing_native_output_is_failure_despite_success_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);fake=root/('fake.py' if os.name == 'nt' else 'fake')
            fake.write_text('#!'+sys.executable+'\n'+'''import json,sys
if sys.argv[1:]==['login','status']:
 print('Logged in using ChatGPT');sys.exit(0)
print(json.dumps({'type':'thread.started','thread_id':'01234567-0123-0123-0123-0123456789ab'}))
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Successfully generated /tmp/fake.png'}}))
print(json.dumps({'type':'turn.completed'}))
''');fake.chmod(0o700)
            with self.assertRaises(ImageError) as caught:await Runner(root/'jobs',codex=str(fake),codex_home=root/'home').run('generate',{'prompt':'x'})
            self.assertEqual(caught.exception.code,'no_image')

    async def test_timeout_stops_own_worker_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);fake=root/('fake.py' if os.name == 'nt' else 'fake')
            fake.write_text('#!'+sys.executable+'\nimport time,sys\nif sys.argv[1:]==["login","status"]:\n print("Logged in using ChatGPT");sys.exit(0)\ntime.sleep(30)\n');fake.chmod(0o700)
            with self.assertRaises(ImageError) as caught:await Runner(root/'jobs',codex=str(fake),timeout=.05).run('generate',{'prompt':'x'})
            self.assertEqual(caught.exception.code,'timeout')
            self.assertEqual(len(list((root/'jobs').iterdir())),1)

    async def test_api_login_is_rejected_before_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);fake=root/('fake.py' if os.name == 'nt' else 'fake')
            fake.write_text('#!'+sys.executable+'\nprint("Logged in using an API key")\n');fake.chmod(0o700)
            with self.assertRaises(ImageError) as caught:
                await Runner(root/'jobs',codex=str(fake)).run('generate',{'prompt':'x'})
            self.assertEqual(caught.exception.code,'authentication')
            self.assertEqual(list((root/'jobs').glob('*/events.jsonl')),[])

    async def test_backend_auth_and_mcp_image_roundtrip(self):
        runner=Runner('/not-used');runner.run=AsyncMock(return_value={'id':'a'*32,'thread_id':'private-thread','width':12,'height':12,'image':base64.b64encode(png()).decode()})
        token='private-image-test-token-123456'
        async with TestClient(TestServer(backend(runner,token))) as http:
            response=await http.post('/generate',json={'prompt':'x'});self.assertEqual(response.status,401);runner.run.assert_not_called()
            response=await http.post('/generate',json={'prompt':'x'},headers={'Authorization':'Bearer '+token});self.assertEqual(response.status,200)
            runner.run.reset_mock()
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);(root/'target.png').write_bytes(png())
                module=Path(__file__).resolve().parents[1]
                script=root/'start.py';script.write_text('from aster_images.server import build\nimport os\nbuild(os.environ["TEST_URL"],os.environ["ASTER_IMAGES_TOKEN"]).run()\n')
                env={'PYTHONPATH':str(module),'TEST_URL':str(http.make_url('')).rstrip('/'),'ASTER_IMAGES_TOKEN':token}
                async with stdio_client(StdioServerParameters(command=sys.executable,args=[str(script)],cwd=str(root),env=env)) as (read,write):
                    async with ClientSession(read,write) as session:
                        await session.initialize();tools=await session.list_tools();self.assertEqual({t.name for t in tools.tools},{'image_generate','image_edit'})
                        result=await session.call_tool('image_edit',{'prompt':'blue mug','image_path':'target.png'})
                        self.assertFalse(result.isError)
                        self.assertEqual([v.type for v in result.content],['text','image'])
                        meta=json.loads(result.content[0].text);self.assertEqual(Path(meta['path']).read_bytes(),png())
                        self.assertEqual(runner.run.call_args.args[0],'edit')
                        self.assertEqual(base64.b64decode(runner.run.call_args.args[1]['images'][0]),png())
                        failed=await session.call_tool('image_generate',{'prompt':'x','output_dir':'../escape'})
                        self.assertTrue(failed.isError)
                        missing=await session.call_tool('image_edit',{'prompt':'x','image_path':'missing.png'})
                        self.assertTrue(missing.isError)
                        self.assertEqual(runner.run.await_count,1)
                        self.assertEqual((root/'target.png').read_bytes(),png())


if __name__=='__main__':unittest.main()
