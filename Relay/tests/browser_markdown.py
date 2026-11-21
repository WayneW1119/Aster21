"""Markdown flows through real private/group APIs, storage and mobile browser UI."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import Request,urlopen

from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[1]
SOURCE='''## 协作进展

这是 **重点**、*说明* 和 ~~旧内容~~，以及 `inline <tag>`。
下一行保持换行。

- 第一项
- 第二项
  - 子项

1. 检查服务
2. 核对结果

> 保留原始消息，网页按 Markdown 显示。

[文档](https://example.com/docs?q=1&x=2)

```python
print("<hello>")
'''+('long_line_'*50)+'''
```

| 项目 | 状态 | 负责人 | 入口 | 备注 |
| --- | --- | --- | --- | --- |
| Relay | 完成 | Alice | 本机 | 说明 |
'''
SOURCE=SOURCE.strip()
ATTACK='''<script>window.relayXss=1</script>
<img src=x onerror="window.relayXss=2">
<svg onload="window.relayXss=3"></svg>
<form id="composer"><input name="text"></form>
[unsafe](javascript:alert(1))
[encoded](javascript&#58;alert(1))
[data](data:text/html,evil)
![remote image](https://example.com/tracking.png)
'''


def main():
    with tempfile.TemporaryDirectory(prefix='relay-markdown-') as temporary:
        sockets=[socket.socket() for _ in range(2)]
        for sock in sockets:sock.bind(('127.0.0.1',0))
        ports=[sock.getsockname()[1] for sock in sockets]
        for sock in sockets:sock.close()
        base=f'http://127.0.0.1:{ports[0]}'
        with (Path(temporary)/'server.log').open('w') as log:
            proc=subprocess.Popen([sys.executable,'-P','-m','relay','--root',temporary,'--port',str(ports[0]),'--mcp-port',str(ports[1])],cwd=ROOT,env={**os.environ,'PYTHONPATH':str(ROOT)},stdout=log,stderr=log,start_new_session=True)
            try:
                def api(path,body=None,credential=None):
                    request=Request(base+'/api/'+path,headers={'Authorization':'Bearer '+(credential or token),'Content-Type':'application/json'},data=None if body is None else json.dumps(body).encode())
                    with urlopen(request,timeout=10) as response:return json.load(response)
                for _ in range(100):
                    try:
                        token=(Path(temporary)/'owner.token').read_text(encoding="utf-8").strip();state=api('state');break
                    except OSError:time.sleep(.1)
                else:raise AssertionError('Temporary Relay did not start')
                owner_id=state['me']['id']
                alice=api('accounts',{'username':'alice'})
                group=api('groups',{'name':'Markdown 讨论','members':['alice']})
                errors=[];external=[]
                with sync_playwright() as p:
                    browser=p.chromium.launch(executable_path=str(Path.home()/'.cache/ms-playwright/chromium-1193/chrome-linux/chrome'),headless=True,args=['--no-sandbox'])
                    def login(credential,fallback=False):
                        context=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
                        context.grant_permissions(['clipboard-read','clipboard-write'],origin=base)
                        context.add_init_script('sessionStorage.setItem("relay-token",'+json.dumps(credential)+');window.relayXss=0;')
                        if fallback:context.route('**/app.js',lambda route:route.fulfill(body=(ROOT/'relay/web/markdown.js').read_text(encoding="utf-8")+'\n'+(ROOT/'relay/web/app-main.js').read_text(encoding="utf-8"),content_type='application/javascript'))
                        page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
                        page.on('request',lambda request:external.append(request.url) if not request.url.startswith(base+'/') else None)
                        page.goto(base+'/');expect(page.locator('#app')).to_be_visible();return page
                    owner=login(token);peer=login(alice['token'])
                    owner.locator(f'#contacts [data-id="{alice["account"]["id"]}"]').tap()
                    peer.locator(f'#contacts [data-id="{owner_id}"]').tap()
                    owner.locator('#text').fill(SOURCE);owner.locator('#sendBtn').tap()
                    expect(peer.locator('.bubble h2')).to_have_text('协作进展',timeout=10000)
                    for page in [owner,peer]:
                        expect(page.locator('.bubble strong')).to_have_text('重点')
                        expect(page.locator('.bubble em')).to_have_text('说明')
                        expect(page.locator('.bubble s')).to_have_text('旧内容')
                        expect(page.locator('.bubble blockquote')).to_contain_text('保留原始消息')
                        expect(page.locator('.bubble ul')).to_have_count(2)
                        expect(page.locator('.bubble ol')).to_have_count(1)
                        expect(page.locator('.bubble table')).to_have_count(1)
                        assert page.locator('.code-block pre code').text_content()=='print("<hello>")\n'+('long_line_'*50)+'\n'
                        assert page.locator('.code-block pre').evaluate('(el)=>el.scrollWidth>el.clientWidth')
                        assert page.locator('.markdown-table').evaluate('(el)=>el.scrollWidth>el.clientWidth')
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                        assert page.locator('#messages').evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
                        link=page.locator('.bubble a').first
                        assert link.get_attribute('target')=='_blank'
                        assert 'noopener' in link.get_attribute('rel') and 'noreferrer' in link.get_attribute('rel')
                    owner.locator('.copy-code').tap()
                    expect(owner.locator('.copy-code')).to_have_text('已复制')
                    assert owner.evaluate('navigator.clipboard.readText()')=='print("<hello>")\n'+('long_line_'*50)+'\n'
                    history=api('messages?peer='+alice['account']['id'])['messages']
                    assert history[-1]['text']==SOURCE
                    artifacts=ROOT/'artifacts/markdown';artifacts.mkdir(parents=True,exist_ok=True)
                    peer.locator('#messages').evaluate('(el)=>el.scrollTop=0')
                    peer.screenshot(path=str(artifacts/'mobile-markdown.png'))
                    api('messages',{'to':'user','text':ATTACK},alice['token'])
                    expect(owner.locator('.bubble').last).to_contain_text('<script>',timeout=10000)
                    assert owner.evaluate('window.relayXss')==0
                    assert owner.locator('.bubble script,.bubble img,.bubble iframe,.bubble svg,.bubble form').count()==0
                    assert owner.locator('#composer').count()==1
                    assert not owner.locator('.bubble a').evaluate_all('(links)=>links.some(a=>/^(javascript|data|vbscript|file):/i.test(a.getAttribute("href")))')
                    assert owner.locator('.image-link').get_attribute('href')=='https://example.com/tracking.png'
                    assert not external,external
                    # Pagination must apply the same renderer to older stored messages.
                    for index in range(51):api('messages',{'to':'alice','text':f'普通消息 {index}'})
                    expect(owner.locator('.bubble h2')).to_have_count(0,timeout=10000)
                    owner.locator('#olderBtn').tap()
                    expect(owner.locator('.bubble h2')).to_have_text('协作进展',timeout=10000)
                    # The shared message renderer also covers both sides of group chat.
                    owner.locator('#backBtn').tap();peer.locator('#backBtn').tap()
                    owner.locator(f'[data-group="{group["id"]}"]').tap();peer.locator(f'[data-group="{group["id"]}"]').tap()
                    owner.locator('#text').fill(SOURCE);owner.locator('#sendBtn').tap()
                    expect(peer.locator('.bubble h2')).to_have_text('协作进展',timeout=10000)
                    assert api(f'groups/{group["id"]}/messages')['messages'][0]['text']==SOURCE
                    owner.set_viewport_size({'width':1440,'height':900})
                    assert owner.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                    owner.screenshot(path=str(artifacts/'desktop-markdown.png'))
                    fallback=login(token,fallback=True);fallback.locator(f'[data-group="{group["id"]}"]').tap()
                    expect(fallback.locator('.bubble')).to_contain_text('**重点**',timeout=10000)
                    assert fallback.locator('.bubble strong').count()==0
                    assert not errors,errors
                    browser.close()
                print('PASS: Markdown private/group chat, unchanged API source, mobile/desktop overflow, copy, history pagination, HTML/URL injection protection, no remote image fetches, plain-text fallback.')
            finally:
                os.killpg(proc.pid,signal.SIGTERM)
                try:proc.wait(timeout=10)
                except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()


if __name__=='__main__':main()
