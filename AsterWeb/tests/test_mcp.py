import asyncio
import json
import socket
import unittest
from unittest.mock import AsyncMock

import aiohttp
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
import uvicorn

from aster_web.server import Auth, build
from aster_web.service import Service


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_auth_tool_schemas_and_provider_roundtrip(self):
        service = Service()
        service._deepseek = AsyncMock(return_value={"results":[{"title":"Docs","url":"https://example.com","summary":"reference"}]})
        token = 'test-private-token-for-mcp-only'
        sock = socket.socket(); sock.bind(('127.0.0.1',0)); port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(Auth(build(service).streamable_http_app(), token),log_level='critical',access_log=False))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            for _ in range(100):
                if server.started: break
                await asyncio.sleep(.02)
            self.assertTrue(server.started)
            async with aiohttp.ClientSession() as http:
                async with http.get(f'http://127.0.0.1:{port}/mcp') as response:
                    self.assertEqual(response.status,401)
                    self.assertNotIn(token, await response.text())
            async with streamablehttp_client(f'http://127.0.0.1:{port}/mcp',headers={'Authorization':'Bearer '+token}) as (read,write,_):
                async with ClientSession(read,write) as session:
                    await session.initialize(); tools=(await session.list_tools()).tools
                    self.assertEqual({t.name for t in tools},{'web_search','web_fetch'})
                    for tool in tools:self.assertIn('provider',tool.inputSchema['required'])
                    result=await session.call_tool('web_search',{'query':'docs','provider':'deepseek','limit':3})
                    self.assertFalse(result.isError)
                    self.assertEqual(result.structuredContent['provider'],'deepseek')
                    self.assertEqual(result.structuredContent['results'][0]['url'],'https://example.com')
                    missing=await session.call_tool('web_search',{'query':'docs'})
                    self.assertTrue(missing.isError)
                    invalid=await session.call_tool('web_fetch',{'url':'file:///etc/passwd','provider':'exa'})
                    self.assertTrue(invalid.isError)
                    self.assertIn('invalid_argument',str(invalid.content))
                    self.assertEqual(service._deepseek.await_count,1)
        finally:
            server.should_exit=True
            await asyncio.wait_for(task,5)
            sock.close()
