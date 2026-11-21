"""Real SDK HTTP endpoint with optional authentication; all effects stay in state-dir."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import socket

from mcp.server.fastmcp import FastMCP
import uvicorn

parser = argparse.ArgumentParser()
parser.add_argument('--state-dir', type=Path, required=True)
parser.add_argument('--json-response', action='store_true')
args = parser.parse_args()
args.state_dir.mkdir(parents=True, exist_ok=True)


def record(kind, **data):
    with (args.state_dir / 'effects.jsonl').open('a') as stream:
        stream.write(json.dumps({'kind': kind, **data}) + '\n')


mcp = FastMCP('http-fixture', instructions='Remote arithmetic and reference resources.',
              json_response=args.json_response, log_level='ERROR')


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers on the remote MCP service."""
    record('add', a=a, b=b)
    return a + b


@mcp.tool()
async def slow(seconds: float) -> str:
    """Wait a short time to test ambiguous client timeouts."""
    record('slow', seconds=seconds)
    await asyncio.sleep(seconds)
    return 'done'


@mcp.resource('fixture://guide')
def guide() -> str:
    return 'Reference delivered through Streamable HTTP.'


@mcp.resource('fixture://items/{item_id}')
def item(item_id: str) -> str:
    return 'HTTP item ' + item_id


class Probe:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        if scope['path'] == '/health':
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            return await send({'type': 'http.response.body', 'body': b'ok'})
        headers = {k.decode().lower(): v.decode() for k, v in scope['headers']}
        token_path = args.state_dir / 'expected-token.txt'
        token = token_path.read_text() if token_path.exists() else os.environ.get('HTTP_FIXTURE_TOKEN', '')
        key = os.environ.get('HTTP_FIXTURE_KEY', '')
        authorized = ((not token or headers.get('authorization') == 'Bearer ' + token)
                      and (not key or headers.get('x-api-key') == key)
                      and headers.get('x-tenant', 'test') == 'test')
        record('request', method=scope['method'], path=scope['path'], authorized=authorized,
               session_id=headers.get('mcp-session-id'))
        if not authorized:
            await send({'type': 'http.response.start', 'status': 401,
                        'headers': [(b'www-authenticate', b'Bearer')]})
            return await send({'type': 'http.response.body', 'body': b'authorization required'})
        if scope['path'] == '/redirect':
            await send({'type': 'http.response.start', 'status': 307,
                        'headers': [(b'location', (url + '/mcp').encode())]})
            return await send({'type': 'http.response.body', 'body': b''})
        body, rpc = bytearray(), {}

        async def capture_receive():
            message = await receive()
            if message['type'] == 'http.request':
                body.extend(message.get('body', b''))
                if not message.get('more_body', False) and body:
                    rpc.update(json.loads(body))
                    record('rpc', method=rpc.get('method'))
            return message

        async def capture_send(message):
            if message['type'] == 'http.response.start':
                response_headers = dict(message.get('headers', []))
                record('response', method=scope['method'], status=message['status'],
                       content_type=response_headers.get(b'content-type', b'').decode())
            await send(message)

        await self.app(scope, capture_receive, capture_send)


sock = socket.socket()
sock.bind(('127.0.0.1', 0))
url = 'http://127.0.0.1:' + str(sock.getsockname()[1])
(args.state_dir / 'server.json').write_text(json.dumps({'origin': url, 'url': url + '/mcp', 'pid': os.getpid()}))
server = uvicorn.Server(uvicorn.Config(Probe(mcp.streamable_http_app()), log_level='error',
                                      lifespan='on', timeout_graceful_shutdown=1))
server.run(sockets=[sock])
