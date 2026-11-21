"""Domain switching and account handoff show/copy the correct complete address."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix='relay-connection-info-') as temporary:
        processes, logs, domains = [], [], []
        try:
            for i in range(2):
                folder=Path(temporary)/str(i);folder.mkdir()
                sockets=[socket.socket() for _ in range(2)]
                for sock in sockets:sock.bind(('127.0.0.1',0))
                ports=[sock.getsockname()[1] for sock in sockets]
                for sock in sockets:sock.close()
                base=f'http://127.0.0.1:{ports[0]}'
                log=(folder/'server.log').open('w');logs.append(log)
                processes.append(subprocess.Popen([sys.executable,'-m','relay','--root',str(folder),'--name',f'Domain {i+1}',
                    '--port',str(ports[0]),'--mcp-port',str(ports[1])],cwd=ROOT,stdout=log,stderr=log))
                for _ in range(100):
                    try:
                        token=(folder/'owner.token').read_text().strip()
                        with urlopen(Request(base+'/api/state',headers={'Authorization':'Bearer '+token}),timeout=2) as r:json.load(r)
                        break
                    except OSError:time.sleep(.1)
                else:raise AssertionError('Test Relay did not start')
                domains.append({'url':base,'token':token,'label':f'Domain {i+1}','agent_url':base})
            original=domains[0]['url'];domains[0]['url'] += '/nested/relay'
            with sync_playwright() as p:
                executable=p.chromium.executable_path
                if not Path(executable).is_file():executable=str(sorted((Path.home()/'.cache/ms-playwright').glob('chromium-*/chrome-linux*/chrome'))[-1])
                browser=p.chromium.launch(executable_path=executable,headless=True,args=['--no-sandbox'])
                for mobile in (True,False):
                    context=browser.new_context(viewport={'width':390 if mobile else 1440,'height':844 if mobile else 1000},
                        is_mobile=mobile,has_touch=mobile,permissions=['clipboard-read','clipboard-write'])
                    context.add_init_script('sessionStorage.setItem("relay-connections-v1",'+json.dumps(json.dumps(domains))+');sessionStorage.setItem("relay-active-url",'+json.dumps(domains[0]['url'])+');')
                    def proxy(route):
                        response=route.fetch(url=original+route.request.url[len(domains[0]['url']):])
                        if route.request.url.rstrip('/')==domains[0]['url']:
                            text=response.text().replace('<title>','<meta name="relay-default-url" content="'+domains[0]['url']+'"><title>',1)
                            route.fulfill(response=response,body=text)
                        else:route.fulfill(response=response)
                    context.route(domains[0]['url']+'/**',proxy)
                    page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                    page.on('dialog',lambda dialog:dialog.accept())
                    page.goto(domains[0]['url']+'/');expect(page.locator('#app')).to_be_visible()
                    for i,domain in enumerate(domains):
                        page.locator('#domainSwitcher').click()
                        card=page.locator('.domain-connection',has=page.locator('b',has_text=domain['label']))
                        expect(card.locator('.service-url').first).to_have_text(domain['url'])
                        card.locator('[data-copy-address]').first.click()
                        assert page.evaluate('navigator.clipboard.readText()')==domain['url']
                        card.locator('[data-choose]').click()
                        expect(page.locator('#domainName')).to_have_text(domain['label'])
                        page.locator('#manageBtn').click()
                        expect(page.locator('#managerServiceUrl')).to_have_text(domain['url'])
                        expect(page.locator('#managerAgentUrl')).to_have_text(domain['agent_url'])
                        page.locator('#copyManagerAgentUrl').click()
                        assert page.evaluate('navigator.clipboard.readText()')==domain['agent_url']
                        page.locator('#copyManagerAddress').click()
                        assert page.evaluate('navigator.clipboard.readText()')==domain['url']
                        username=f'handoff_{int(mobile)}_{i}'
                        page.locator('#newHandle').fill(username)
                        page.locator('#createAccount button[type=submit], #createAccount button.primary').click()
                        expect(page.locator('#credential')).to_be_visible()
                        expect(page.locator('#credentialUsername')).to_have_text('用户名：@'+username)
                        expect(page.locator('#credentialServiceUrl')).to_have_text(domain['url'])
                        expect(page.locator('#credentialAgentUrl')).to_have_text(domain['agent_url'])
                        issued=page.locator('#credentialValue').text_content()
                        page.locator('#copyConnectionInfo').click()
                        copied=page.evaluate('navigator.clipboard.readText()')
                        assert f'服务地址：{domain["agent_url"]}' in copied and f'网页访问地址：{domain["url"]}' in copied and f'用户名：{username}' in copied and copied.endswith(issued)
                        assert domain['token'] not in copied  # Never hand off the creator's credential.
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                        # Reset must identify the same target account, not the logged-in user.
                        page.locator('#closeCredential').click()
                        expect(page.locator('#credentialValue')).to_have_text('')
                        row=page.locator('.managed').filter(has=page.locator('small',has_text='@'+username))
                        row.locator('[data-action="reset_token"]').click()
                        expect(page.locator('#credentialUsername')).to_have_text('用户名：@'+username)
                        assert page.locator('#credentialValue').text_content()!=issued
                        page.locator('#closeCredential').click();page.locator('#closeManager').click()
                    assert not errors,errors
                    context.close()
                browser.close()
            print('PASS: desktop/mobile web and same-host agent address display and copy, domain switch, create/reset target account handoff, creator credential excluded, no overflow or browser errors.')
        finally:
            for proc in processes:proc.terminate()
            for proc in processes:
                try:proc.wait(timeout=15)
                except subprocess.TimeoutExpired:proc.kill();proc.wait()
            for log in logs:log.close()


if __name__=='__main__':main()
