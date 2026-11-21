"""Delayed real API responses must not block navigation, rendering or send receipts."""
import asyncio
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import aiohttp
from playwright.async_api import async_playwright, expect

ROOT=Path(__file__).resolve().parents[1]


async def main():
    with tempfile.TemporaryDirectory(prefix='relay-responsive-') as temporary:
        sockets=[socket.socket() for _ in range(2)]
        for sock in sockets:sock.bind(('127.0.0.1',0))
        ports=[s.getsockname()[1] for s in sockets]
        for sock in sockets:sock.close()
        base=f'http://127.0.0.1:{ports[0]}'
        with (Path(temporary)/'server.log').open('w') as log:
            proc=subprocess.Popen([sys.executable,'-m','relay','--root',temporary,'--port',str(ports[0]),'--mcp-port',str(ports[1])],cwd=ROOT,stdout=log,stderr=log)
            try:
                async with aiohttp.ClientSession() as http:
                    async def api(path,body=None,credential=None):
                        async with http.request('GET' if body is None else 'POST',base+'/api/'+path,headers={'Authorization':'Bearer '+(credential or token)},json=body) as r:
                            data=await r.json();assert r.status==200,(r.status,data);return data
                    for _ in range(100):
                        try:
                            token=(Path(temporary)/'owner.token').read_text().strip();state=await api('state');break
                        except (OSError,aiohttp.ClientError,AssertionError):await asyncio.sleep(.1)
                    else:raise AssertionError('Relay did not start')
                    alice=await api('accounts',{'username':'alice'});bob=await api('accounts',{'username':'bob'})
                    owner=state['me']['id'];aid=alice['account']['id'];bid=bob['account']['id']
                    group=await api('groups',{'name':'Group','members':['alice']})
                    async with async_playwright() as p:
                        executable=p.chromium.executable_path
                        if not Path(executable).is_file():executable=str(sorted((Path.home()/'.cache/ms-playwright').glob('chromium-*/chrome-linux*/chrome'))[-1])
                        browser=await p.chromium.launch(executable_path=executable,headless=True,args=['--no-sandbox'])
                        for mobile in (False,True):
                            await api('messages',{'to':'user','text':'from-alice'},alice['token'])
                            await api('messages',{'to':'user','text':'from-bob'},bob['token'])
                            context=await browser.new_context(viewport={'width':390 if mobile else 1440,'height':844 if mobile else 1000},is_mobile=mobile,has_touch=mobile)
                            await context.add_init_script('sessionStorage.setItem("relay-token",'+json.dumps(token)+')')
                            page=await context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                            await page.goto(base+'/');await expect(page.locator('#app')).to_be_visible()
                            gates={name:asyncio.Event() for name in ('state','ack','alice','bob','send')}
                            started={name:asyncio.Event() for name in gates}
                            hold={'state':True,'ack':True,'alice':False,'bob':False,'send':False}
                            send_mode='normal'
                            async def route_request(route):
                                nonlocal send_mode
                                url=route.request.url;method=route.request.method
                                kind=('state' if url.endswith('/api/state') else 'ack' if url.endswith('/api/ack') else
                                      'alice' if method=='GET' and 'peer='+aid in url else 'bob' if method=='GET' and 'peer='+bid in url else None)
                                try:
                                    if method=='POST' and url.endswith('/api/messages'):
                                        response=await route.fetch()  # Real database commit, then delay or lose response.
                                        if send_mode=='lost':send_mode='normal';await route.abort('failed');return
                                        if hold['send']:started['send'].set();await gates['send'].wait()
                                        await route.fulfill(response=response);return
                                    if kind and hold[kind]:started[kind].set();await gates[kind].wait()
                                    await route.continue_()
                                except Exception:
                                    # Navigation aborts old fetches; a closed context releases any final handlers.
                                    if not page.is_closed():
                                        try:await route.abort()
                                        except Exception:pass
                            await context.route('**/api/**',route_request)
                            await page.evaluate('void refresh()');await asyncio.wait_for(started['state'].wait(),3)
                            async def select(peer):
                                if mobile and await page.locator('#app').evaluate("el=>el.classList.contains('chat-open')"):
                                    await page.locator('#backBtn').click()
                                await page.locator(f'#contacts [data-id="{peer}"]').click()
                            try:
                                begin=time.monotonic();await select(aid)
                                await expect(page.locator('.bubble').filter(has_text='from-alice').last).to_be_visible(timeout=900)
                                assert time.monotonic()-begin<1.2
                                await asyncio.wait_for(started['ack'].wait(),2)  # Still held: read receipt cannot hide messages.
                                await select(bid);await expect(page.locator('.bubble').filter(has_text='from-bob').last).to_be_visible(timeout=900)
                                hold['alice']=True
                                await select(aid);await expect(page.locator('.bubble').filter(has_text='from-alice').last).to_be_visible(timeout=300)
                                await asyncio.wait_for(started['alice'].wait(),2)
                                await select(bid);gates['alice'].set()
                                await expect(page.locator('.bubble').filter(has_text='from-alice')).to_have_count(0)
                                # Sending feedback appears before the real POST response; navigation preserves a later draft.
                                hold['send']=True;send_text=f'delayed-send-{mobile}'
                                await page.locator('#text').fill(send_text);await page.locator('#sendBtn').click()
                                await expect(page.locator('.receipt').filter(has_text='发送中')).to_be_visible(timeout=500)
                                await asyncio.wait_for(started['send'].wait(),2)
                                await page.locator('#text').fill('next draft');await select(aid)
                                await expect(page.locator('.bubble').filter(has_text=send_text)).to_have_count(0)
                                hold['send']=False;gates['send'].set();await select(bid)
                                await expect(page.locator('.bubble').filter(has_text=send_text)).to_have_count(1)
                                await expect(page.locator('.receipt').filter(has_text='发送中')).to_have_count(0,timeout=1500)
                                await expect(page.locator('#text')).to_have_value('next draft')
                                # A lost acknowledgement after commit must retry with the same idempotency key.
                                hold['bob']=True;await page.evaluate('void refreshConversation()');await asyncio.wait_for(started['bob'].wait(),3)
                                retry_text=f'retry-after-commit-{mobile}';send_mode='lost'
                                await page.locator('#text').fill(retry_text);await page.locator('#sendBtn').click()
                                await expect(page.locator('.retry-message')).to_be_visible(timeout=2000)
                                await page.locator('.retry-message').click()
                                await expect(page.locator('.retry-message')).to_have_count(0,timeout=2000)
                                history=(await api('messages?peer='+bid))['messages']
                                assert sum(row['text']==retry_text for row in history)==1
                                await expect(page.locator('.bubble').filter(has_text=retry_text)).to_have_count(1)
                                # A later history read can confirm a lost response without leaving a duplicate draft.
                                hold['bob']=False;gates['bob'].set();await page.evaluate('refreshConversation()')
                                gates['bob']=asyncio.Event();started['bob']=asyncio.Event();hold['bob']=True
                                await page.evaluate('void refreshConversation()');await asyncio.wait_for(started['bob'].wait(),3)
                                reconcile_text=f'confirmed-by-history-{mobile}';send_mode='lost'
                                await page.locator('#text').fill(reconcile_text);await page.locator('#sendBtn').click()
                                await expect(page.locator('.retry-message')).to_be_visible(timeout=2000)
                                hold['bob']=False;gates['bob'].set()
                                await expect(page.locator('.retry-message')).to_have_count(0,timeout=2000)
                                await expect(page.locator('#text')).to_have_value('')
                                history=(await api('messages?peer='+bid))['messages']
                                assert sum(row['text']==reconcile_text for row in history)==1
                                # Group sends use the same independent path and immediate receipt.
                                if mobile:await page.locator('#backBtn').click()
                                await page.locator(f'[data-group="{group["id"]}"]').click()
                                group_text=f'group-send-{mobile}';await page.locator('#text').fill(group_text);await page.locator('#sendBtn').click()
                                await expect(page.locator('.bubble').filter(has_text=group_text)).to_have_count(1,timeout=1000)
                                await expect(page.locator('.receipt').filter(has_text='已发送到群')).to_be_visible(timeout=1500)
                                assert not errors,errors
                            finally:
                                for gate in gates.values():gate.set()
                                await context.close()
                        await browser.close()
                print('PASS: desktop/mobile navigation bypasses held state/ACK; cached revisit is immediate; stale history cannot replace selection; pending sends survive navigation, preserve drafts; lost-response retry stores once; group sending works.')
            finally:
                proc.terminate()
                try:await asyncio.to_thread(proc.wait,15)
                except subprocess.TimeoutExpired:proc.kill();await asyncio.to_thread(proc.wait)


if __name__=='__main__':asyncio.run(main())
