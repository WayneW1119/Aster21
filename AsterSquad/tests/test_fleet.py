"""Independent processes: multiple Squads, domains, branches and archival."""
import asyncio
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import unquote, urlsplit

import aiohttp

ROOT = Path(__file__).resolve().parents[1]


class FleetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aster-console-test-")
        self.root = Path(self.temp.name)
        socks = [socket.socket() for _ in range(5)]
        for sock in socks:
            sock.bind(("127.0.0.1", 0))
        self.ports = [s.getsockname()[1] for s in socks]
        for sock in socks:
            sock.close()
        self.log = (self.root / "console.log").open("a")
        self.command = [sys.executable, "-m", "squad.fleet", "--root", str(self.root)]
        for flag, port in zip(["--port", "--primary-port", "--mcp-port", "--relay-port", "--relay-mcp-port"], self.ports):
            self.command.extend([flag,str(port)])
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90))
        await self.start_console()

    async def start_console(self):
        self.proc = subprocess.Popen(self.command, cwd=ROOT, stdout=self.log, stderr=self.log, start_new_session=True)
        for _ in range(150):
            try:
                self.token = (self.root / "control/owner.token").read_text().strip()
                status, state = await self.request("state")
                if status == 200 and any(r["id"]=="studio" and r["status"]=="running" for r in state["resources"]):
                    return
            except (OSError, aiohttp.ClientError):
                pass
            await asyncio.sleep(.1)
        self.fail("Console did not start: " + (self.root / "console.log").read_text()[-2000:])

    async def stop_console(self):
        if self.proc.returncode is None:
            __import__('squad._windows',fromlist=['request_stop']).request_stop(self.proc) if os.name == 'nt' else self.proc.terminate()
            try:
                await asyncio.to_thread(self.proc.wait, 55)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid,signal.SIGKILL)
                await asyncio.to_thread(self.proc.wait)

    async def asyncTearDown(self):
        await self.stop_console()
        await self.http.close()
        self.log.close()
        self.temp.cleanup()

    async def request(self, path, body=None, token=None):
        async with self.http.request("GET" if body is None else "POST",f"http://127.0.0.1:{self.ports[0]}/api/"+path,
            headers={"Authorization":"Bearer "+(self.token if token is None else token)},json=body) as response:
            return response.status,await response.json()

    async def ok(self,path,body=None):
        status,data=await self.request(path,body)
        self.assertEqual(status,200,data)
        return data

    async def connections(self):
        data=await self.ok("relay-connections")
        return json.loads(unquote(urlsplit(data["url"]).fragment.removeprefix("connections=")))["connections"]

    async def test_startup_and_space_settings_require_owner_and_are_live(self):
        code, _ = await self.request('startup', {'enabled':True}, token='wrong')
        self.assertEqual(code, 401)
        startup = await self.ok('startup')
        self.assertFalse(startup['available'])
        code, _ = await self.request('startup', {'enabled':True})
        self.assertEqual(code, 409)
        await self.ok('resources/studio/settings', {'name':'Default','restore_members':True})
        state = await self.ok('squads/studio/state')
        self.assertTrue(state['restore_members'])
        self.assertTrue(state['resource']['restore_members'])
        await self.ok('resources/studio/stop', {})
        await self.ok('resources/studio/settings', {'name':'Default','restore_members':False})
        self.assertFalse((await self.ok('squads/studio/state'))['resource']['restore_members'])
        await self.ok('resources/studio/start', {})
        self.assertFalse((await self.ok('squads/studio/state'))['restore_members'])

    async def test_remove_running_space_preserves_member_data_and_releases_name(self):
        created=await self.ok('squads',{'name':'Removable'})
        ident=created['resource']['id']
        member=await self.ok(f'squads/{ident}/agents',{'handle':'resident','instruction':'Isolated fixture'})
        workspace=Path(member['workspace']);sentinel=workspace/'keep.txt';sentinel.write_text('preserve')
        status,_=await self.request(f'resources/{ident}/delete',{},token='wrong')
        self.assertEqual(status,401)
        result=await self.ok(f'resources/{ident}/delete',{})
        self.assertTrue(result['data_preserved'])
        self.assertEqual(sentinel.read_text(),'preserve')
        record=json.loads((self.root/'control/fleet/removed'/f'{ident}.json').read_text())
        self.assertTrue(list(Path(record['root']).rglob('rollout.jsonl')))
        status,_=await self.request(f'squads/{ident}/state')
        self.assertEqual(status,404)
        replacement=await self.ok('squads',{'name':'Removable'})
        self.assertNotEqual(replacement['resource']['id'],ident)

    async def test_create_branch_isolate_archive_restart_and_select_domains(self):
        status,_=await self.request("squads",{"name":"unauthorized"},token="bogus")
        self.assertEqual(status,401)
        first=await self.ok("squads/studio/agents",{"handle":"lead","instruction":"Coordinate this team."})
        child=await self.ok("squads/studio/agents",{"handle":"researcher","instruction":"Research sources.","parent":first["id"]})
        self.assertEqual(child["parent"],first["id"])
        branch=await self.ok("squads/studio/state")
        self.assertEqual(len(branch["agents"]),2)
        second=await self.ok("squads",{"name":"Research","description":"Separate team","mode":"fake"})
        sid=second["resource"]["id"]
        separate=await self.ok(f"squads/{sid}/agents",{"handle":"lead","instruction":"Another independent team."})
        self.assertNotEqual(first["relay_account"],separate["relay_account"])
        self.assertNotEqual(first["workspace"],separate["workspace"])
        self.assertEqual(len((await self.ok("squads/studio/state"))["agents"]),2)
        status,_=await self.request("squads",{"name":"RESEARCH"})
        self.assertEqual(status,409)
        status,_=await self.request("squads/studio/agents/"+first["id"]+"/archive",{})
        self.assertEqual(status,409)
        archived=await self.ok("squads/studio/agents/"+child["id"]+"/archive",{})
        self.assertEqual(archived["status"],"archived")
        status,_=await self.request("squads/studio/agents/"+child["id"]+"/start",{})
        self.assertEqual(status,409)
        restored=await self.ok("squads/studio/agents/"+child["id"]+"/restore",{})
        self.assertEqual(restored["thread"],child["thread"])
        self.assertEqual(restored["status"],"offline")
        await self.ok("squads/studio/agents/"+child["id"]+"/configure",{"instruction":"Updated responsibility."})
        bulk=await self.ok("squads/studio/bulk",{"action":"start"})
        self.assertEqual(bulk["failed"],[])
        self.assertEqual(len(bulk["succeeded"]),2)
        independent=await self.ok("domains",{"name":"Open discussion"})
        self.assertEqual(independent["resource"]["kind"],"domain")
        connections=await self.connections()
        self.assertEqual(len(connections),3)
        self.assertEqual(len({c["url"] for c in connections}),3)
        realm_ids=[]
        for connection in connections:
            async with self.http.get(connection["url"]+"/api/state",headers={"Authorization":"Bearer "+connection["token"]}) as response:
                self.assertEqual(response.status,200)
                data=await response.json();realm_ids.append(data["domain"]["id"])
        self.assertEqual(len(set(realm_ids)),3)
        async with self.http.get(connections[1]["url"]+"/api/state",headers={"Authorization":"Bearer "+connections[0]["token"]}) as response:
            self.assertEqual(response.status,401)
        async with self.http.options(connections[1]["url"]+"/api/state",headers={"Origin":connections[0]["url"],"Access-Control-Request-Headers":"authorization,content-type","Access-Control-Request-Method":"GET"}) as response:
            self.assertEqual(response.status,204)
            self.assertEqual(response.headers["Access-Control-Allow-Origin"],connections[0]["url"])
        await self.ok(f"resources/{sid}/settings",{"name":"Research Lab","description":"Renamed"})
        self.assertEqual((await self.ok(f"squads/{sid}/state"))["name"],"Research Lab")
        await self.ok(f"resources/{sid}/stop",{})
        paused=await self.ok(f"squads/{sid}/state")
        self.assertEqual(paused["resource"]["status"],"stopped")
        self.assertTrue(all(a["status"]=="offline" for a in paused["agents"]))
        self.assertEqual(len(await self.connections()),2)
        await self.stop_console()
        await self.start_console()
        state=await self.ok("state")
        self.assertEqual(len(state["resources"]),3)
        self.assertEqual(next(r for r in state["resources"] if r["id"]==sid)["status"],"stopped")
        await self.ok(f"resources/{sid}/start",{})
        restarted=await self.ok(f"squads/{sid}/state")
        self.assertEqual(restarted["agents"][0]["thread"],separate["thread"])
        current=await self.connections()
        self.assertEqual({c["url"] for c in current},{c["url"] for c in connections})

    async def test_relay_file_proxy_streams_upload_download_and_range_with_domain_auth(self):
        connection = (await self.connections())[0]
        base = connection['url']
        headers = {'Authorization': 'Bearer '+connection['token']}
        async with self.http.post(base+'/api/accounts', json={'username':'filepeer'}, headers=headers) as response:
            peer = await response.json()
            self.assertEqual(response.status, 200)
        payload = b'file-data\x00' * 50000
        form = aiohttp.FormData(quote_fields=False)
        form.add_field('file', payload, filename='文件.bin')
        async with self.http.post(base+'/api/files?to=filepeer&client_id=upload-1', data=form, headers=headers) as response:
            message = await response.json()
            self.assertEqual(response.status, 200, message)
        path = base+'/api/files/'+message['attachment']['id']
        async with self.http.get(path, headers=headers) as response:
            self.assertEqual(response.status, 200)
            self.assertIn('attachment;', response.headers['Content-Disposition'])
            self.assertEqual(await response.read(), payload)
        peer_headers = {'Authorization': 'Bearer '+peer['token'], 'Range':'bytes=1-9'}
        async with self.http.get(path, headers=peer_headers) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(await response.read(), payload[1:10])
        async with self.http.get(path) as response:
            self.assertEqual(response.status, 401)
        other = await self.ok('domains', {'name':'Other file domain'})
        second = next(c for c in await self.connections() if c['url'] != base)
        async with self.http.get(path, headers={'Authorization':'Bearer '+second['token']}) as response:
            self.assertEqual(response.status, 401)


if __name__=="__main__":
    unittest.main()
