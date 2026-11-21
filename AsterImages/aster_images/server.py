"""Private Codex worker and a thin stdio MCP client inside the caller's sandbox."""
import argparse
import asyncio
import base64
import hmac
import json
import os
from pathlib import Path
import subprocess
import uuid

import aiohttp
from aiohttp import web
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import TextContent

from .runner import Runner, ImageError, MAX_BYTES, image_info, decode_image


def backend(runner, token):
    @web.middleware
    async def auth(request, handler):
        if not hmac.compare_digest(request.headers.get('Authorization', ''), 'Bearer '+token):
            return web.json_response({'code': 'authentication_required'}, status=401)
        try:
            return await handler(request)
        except ImageError as error:
            return web.json_response({'code': error.code, 'message': str(error)}, status=409 if error.code=='busy' else 400)
        except (ValueError, TypeError):
            return web.json_response({'code': 'invalid_argument', 'message': '请求格式无效'}, status=400)
    app = web.Application(middlewares=[auth], client_max_size=24 * 1024 * 1024)
    async def generate(request):
        return web.json_response(await runner.run(request.match_info['action'], await request.json()))
    app.router.add_post('/{action:generate|edit}', generate)
    return app


def build(url, token, *, workspace=None):
    # This process runs inside the caller's Harness sandbox. File access is
    # checked there, before bytes leave the process. The backend accepts no paths.
    root = Path(workspace or Path.cwd()).resolve()
    mcp = FastMCP('images', log_level='WARNING')
    async def perform(action, prompt, paths, transparent, output_dir):
        try:
            if not isinstance(paths, list) or len(paths) > 4:
                raise ImageError('invalid_argument', '最多 4 张输入图片')
            images=[]
            for value in paths:
                path = (root / value).resolve(strict=True)
                with path.open('rb') as stream:
                    data=stream.read(MAX_BYTES+1)
                image_info(data)
                images.append(base64.b64encode(data).decode())
            target=(root/output_dir).resolve()
            if not target.is_relative_to(root):
                raise ImageError('invalid_argument', 'output_dir 须位于当前工作区内')
            target.mkdir(parents=True, exist_ok=True)
            # Check writability before consuming quota.
            probe=target/('.image-check-'+uuid.uuid4().hex)
            with probe.open('xb') as stream:stream.write(b'')
            probe.unlink()
            body={'prompt':prompt,'images':images,'transparent_background':transparent}
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=450)) as http:
                async with http.post(url.rstrip('/')+'/'+action, json=body, headers={'Authorization':'Bearer '+token}, allow_redirects=False) as response:
                    raw=bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        raw.extend(chunk)
                        if len(raw)>12*1024*1024:
                            raise ImageError('invalid_result','图片服务响应过大')
                    result=json.loads(raw)
                    if response.status!=200:
                        raise ImageError(result.get('code','backend_failed'),result.get('message','图片服务请求失败'))
            data=decode_image(result.get('image'));info=image_info(data)
            ident=result.get('id')
            if not isinstance(ident,str) or len(ident)!=32 or any(c not in '0123456789abcdef' for c in ident):
                raise ImageError('invalid_result','图片回执无效')
            output=target/(ident+'.png')
            with output.open('xb') as stream:stream.write(data)
            metadata={'id':ident,'path':str(output),**info,'backend':'codex','auth':'chatgpt','requirements':'内容与构图要求由 Codex 解释；请检查图片'}
            return [TextContent(type='text',text=json.dumps(metadata,ensure_ascii=False)),Image(data=data,format='png').to_image_content()]
        except ImageError as error:
            raise ToolError(json.dumps({'code':error.code,'message':str(error)},ensure_ascii=False)) from None
        except OSError:
            raise ToolError('图片文件无法读取或保存，请检查路径权限；若任务已开始可能已消耗额度，未自动重试') from None
        except (aiohttp.ClientError,asyncio.TimeoutError,ValueError,TypeError):
            raise ToolError('图片服务连接失败、超时或返回格式异常；结果可能未知，请勿盲目重复调用') from None

    @mcp.tool()
    async def image_generate(prompt: str, reference_paths: list[str] | None = None, transparent_background: bool = False, output_dir: str = 'images'):
        """生成一张图片，可附 0–4 张本地参考图。使用宿主 Codex 的 ChatGPT 登录额度；不自动重试。output_dir 为工作区内输出目录。"""
        return await perform('generate',prompt,reference_paths or [],transparent_background,output_dir)

    @mcp.tool()
    async def image_edit(prompt: str, image_path: str, reference_paths: list[str] | None = None, transparent_background: bool = False, output_dir: str = 'images'):
        """修改一张本地图片；说明修改什么、保留什么。可另附最多 3 张参考图。生成新文件，不覆盖原图；不是像素精确编辑。"""
        return await perform('edit',prompt,[image_path,*(reference_paths or [])],transparent_background,output_dir)
    return mcp


def main():
    parser=argparse.ArgumentParser(description='Independent Codex image generation/editing MCP')
    sub=parser.add_subparsers(dest='mode',required=True)
    serve=sub.add_parser('serve');serve.add_argument('--host',default='127.0.0.1');serve.add_argument('--port',type=int,default=19931)
    serve.add_argument('--root',default='~/.aster-images/jobs');serve.add_argument('--codex',default='codex');serve.add_argument('--codex-home',default=os.environ.get('CODEX_HOME',str(Path.home()/'.codex')));serve.add_argument('--model')
    sub.add_parser('mcp')
    args=parser.parse_args();token=os.environ.get('ASTER_IMAGES_TOKEN','')
    if len(token)<24:parser.error('ASTER_IMAGES_TOKEN must contain at least 24 characters')
    if args.mode=='mcp':
        build(os.environ.get('ASTER_IMAGES_URL','http://127.0.0.1:19931'),token).run(transport='stdio')
    else:
        os.umask(0o077)
        env={k:v for k,v in os.environ.items() if not(k.startswith('OPENAI_') or k in {'CODEX_API_KEY','CODEX_ACCESS_TOKEN'})};env['CODEX_HOME']=str(Path(args.codex_home).expanduser())
        check=subprocess.run([args.codex,'login','status'],env=env,capture_output=True,text=True,timeout=20)
        if check.returncode or 'ChatGPT' not in check.stdout+check.stderr:parser.error('Codex must be logged in with ChatGPT; API-key fallback is disabled')
        web.run_app(backend(Runner(args.root,codex=args.codex,codex_home=args.codex_home,model=args.model),token),host=args.host,port=args.port,access_log=None,print=None,handler_cancellation=True)
