"""Real controller restarts, original Threads and fake SDK/MCP only."""
import asyncio
import os
import subprocess
import unittest
import aiohttp
import test_system

ROOT = test_system.ROOT


class AutostartSystemTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_system.SystemTests.asyncSetUp
    asyncTearDown = test_system.SystemTests.asyncTearDown
    request = test_system.SystemTests.request
    owner = test_system.SystemTests.owner

    async def restart(self):
        command = self.proc.args
        if os.name == 'nt':
            from squad._windows import request_stop
            request_stop(self.proc)
        else:
            self.proc.terminate()
        await asyncio.to_thread(self.proc.wait, 40)
        self.proc = subprocess.Popen(command, cwd=ROOT, stdout=self.log, stderr=self.log, start_new_session=True)
        for _ in range(200):
            try:
                status, data = await self.request(self.web, 'GET', 'state', self.token)
                if status == 200:
                    return data
            except (OSError, ConnectionError):
                pass
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(.1)
        self.fail('Controller did not become ready after restart')

    async def test_optional_resume_and_explicit_pause_across_controller_restarts(self):
        await self.owner('settings', {'restore_members':True})
        keep = await self.owner('agents', {'handle':'keep','instruction':'Test restart continuity'})
        paused = await self.owner('agents', {'handle':'paused','instruction':'Test explicit pause','auto_start':True})
        disabled = await self.owner('agents', {'handle':'disabled','instruction':'Explicitly paused'})
        await self.owner('agents/'+disabled['id']+'/stop', {})
        await self.owner('agents/'+paused['id']+'/stop', {})
        await self.restart()
        for _ in range(150):
            rows = {a['id']:a for a in (await self.owner('state'))['agents']}
            if rows[keep['id']]['status'] == 'online': break
            await asyncio.sleep(.1)
        self.assertEqual(rows[keep['id']]['status'], 'online')
        for original in (keep, paused, disabled):
            self.assertEqual(rows[original['id']]['thread'], original['thread'])
        self.assertEqual(rows[paused['id']]['status'], 'offline')
        self.assertEqual(rows[disabled['id']]['status'], 'offline')
        await self.owner('agents/'+keep['id']+'/stop', {})
        await self.restart()
        rows = {a['id']:a for a in (await self.owner('state'))['agents']}
        self.assertEqual(rows[keep['id']]['status'], 'offline')
        self.assertFalse(rows[keep['id']]['run_requested'])
        await self.owner('agents/'+keep['id']+'/start', {})
        await self.owner('settings', {'restore_members':False})
        await self.restart()
        rows = {a['id']:a for a in (await self.owner('state'))['agents']}
        self.assertEqual(rows[keep['id']]['status'], 'offline')
        self.assertTrue(rows[keep['id']]['run_requested'])
        self.assertFalse((await self.owner('state'))['restore_members'])
