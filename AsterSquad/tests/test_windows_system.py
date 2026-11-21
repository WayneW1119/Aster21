"""Windows-native preview: real workers, MCP and Relay, scripted model only."""
import asyncio
import os
import unittest


@unittest.skipUnless(os.name == 'nt', 'Windows integration acceptance')
class WindowsSystemTests(unittest.IsolatedAsyncioTestCase):
    async def test_member_relay_and_same_thread_resume(self):
        from test_system import SystemTests
        fixture = SystemTests()
        await fixture.asyncSetUp()
        try:
            member = await fixture.owner('agents', {'handle':'windowsagent','instruction':'Offline Windows acceptance.'})
            ident = member['id']
            for _ in range(100):
                state=await fixture.owner('state')
                agent=next(a for a in state['agents'] if a['id']==ident)
                if agent['telemetry']['phase']=='waiting':break
                self.assertNotEqual(agent['status'],'error',agent.get('error'))
                await asyncio.sleep(.1)
            self.assertEqual(agent['telemetry']['phase'],'waiting')
            thread=agent['thread']
            status,_=await fixture.request(fixture.relay,'POST','messages',fixture.relay_token,
                                          {'to':'windowsagent','text':'Windows native message','client_id':'windows-test'})
            self.assertEqual(status,200)
            messages=await fixture.wait_reply('windowsagent',2)
            self.assertTrue(any('Windows native message' in m['text'] for m in messages))
            await fixture.owner('agents/'+ident+'/stop',{})
            await fixture.owner('agents/'+ident+'/start',{})
            state=await fixture.owner('state')
            resumed=next(a for a in state['agents'] if a['id']==ident)
            self.assertEqual(resumed['thread'],thread)
            await fixture.owner('agents/'+ident+'/stop',{})
        finally:
            await fixture.asyncTearDown()
