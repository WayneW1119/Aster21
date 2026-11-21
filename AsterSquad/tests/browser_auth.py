"""Isolated real WebAuthn signatures with Chromium's virtual authenticator."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright,expect
from browser_utils import chromium_executable


def main():
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder)
        sockets=[]
        for _ in range(5):
            sock=socket.socket();sock.bind(('127.0.0.1',0));sockets.append(sock)
        ports=[s.getsockname()[1] for s in sockets]
        for s in sockets:s.close()
        # localhost is a browser secure context, suitable for isolated WebAuthn tests.
        base=f'http://localhost:{ports[0]}'
        args={'root':str(root),'port':ports[0],'primary_port':ports[1],'mcp_port':ports[2],
            'relay_port':ports[3],'relay_mcp_port':ports[4],'name':'Auth verification','mode':'fake',
            'models':'','responses_url':'https://api.openai.com/v1/responses','model_catalog':'',
            'allow_http':False,'public_url':base}
        source='import argparse,asyncio;from squad.fleet import serve;asyncio.run(serve(argparse.Namespace(**'+repr(args)+')))'
        with (root/'server.log').open('w') as log:
            process=subprocess.Popen([sys.executable,'-c',source],stdout=log,stderr=log)
            try:
                for _ in range(200):
                    try:
                        urllib.request.urlopen(base+'/squad/auth/status',timeout=1);break
                    except OSError:time.sleep(.1)
                else:raise AssertionError((root/'server.log').read_text())
                token=(root/'control/owner.token').read_text().strip()
                with sync_playwright() as p:
                    browser=p.chromium.launch(executable_path=chromium_executable(p),headless=True,args=['--no-sandbox'])
                    context=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
                    page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    cdp=context.new_cdp_session(page);cdp.send('WebAuthn.enable')
                    authenticator=cdp.send('WebAuthn.addVirtualAuthenticator',{'options':{'protocol':'ctap2','transport':'internal','hasResidentKey':True,'hasUserVerification':True,'isUserVerified':True,'automaticPresenceSimulation':True}})['authenticatorId']
                    page.goto(base+'/squad/');page.locator('#token').fill(token);page.locator('#loginForm button').click();expect(page.locator('#app')).to_be_visible()
                    page.locator('#mobileSettings').click();page.locator('#securityUsername').fill('my-account');page.locator('#newPassword').fill('a-long-password-for-tests');page.locator('#confirmPassword').fill('a-long-password-for-tests');page.locator('#passwordSettingsForm button').click()
                    expect(page.locator('#securityMessage')).to_contain_text('账号密码已保存')
                    page.locator('#passkeyName').fill('Virtual phone');page.locator('#registerPasskey').click();expect(page.locator('#securityMessage')).to_contain_text('通行密钥已绑定',timeout=20000)
                    assert len(cdp.send('WebAuthn.getCredentials',{'authenticatorId':authenticator})['credentials'])==1
                    page.locator('[data-close=securityDialog]').last.click();page.locator('#mobileNav').click();page.locator('#logout').click();expect(page.locator('#passwordLoginForm')).to_be_visible()
                    page.locator('#passkeyLogin').click();expect(page.locator('#app')).to_be_visible(timeout=20000)
                    # Capture and tamper with a real signed assertion; replay is also rejected.
                    result=page.evaluate('''async()=>{const o=await authApi('passkeys/login/options',{});o.challenge=bytesFromBase64url(o.challenge);o.allowCredentials=o.allowCredentials.map(k=>({...k,id:bytesFromBase64url(k.id)}));const c=credentialJson(await navigator.credentials.get({publicKey:o}));c.response.signature='AAAA';const send=()=>fetch('/squad/auth/passkeys/login/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({credential:c})});return [(await send()).status,(await send()).status];}''')
                    assert result==[400,400],result
                    page.locator('#mobileNav').click();page.locator('#logout').click();expect(page.locator('#passwordLoginForm')).to_be_visible()
                    page.locator('#loginUsername').fill('my-account');page.locator('#loginPassword').fill('wrong-password')
                    with page.expect_response(lambda r:r.url.endswith('/password/login')) as bad_login:
                        page.locator('#passwordLoginForm button').click()
                    assert bad_login.value.status==401
                    expect(page.locator('#loginError')).to_contain_text('账号或密码不正确')
                    page.locator('#loginPassword').fill('a-long-password-for-tests')
                    with page.expect_response(lambda r:r.url.endswith('/password/login')) as good_login:
                        page.locator('#passwordLoginForm button').click()
                    assert good_login.value.status==200
                    expect(page.locator('#app')).to_be_visible(timeout=15000)
                    page.locator('#mobileSettings').click();page.locator('#currentPassword').fill('a-long-password-for-tests');page.locator('#newPassword').fill('a-replacement-password');page.locator('#confirmPassword').fill('a-replacement-password');page.locator('#passwordSettingsForm button').click();expect(page.locator('#securityMessage')).to_contain_text('账号密码已保存')
                    page.on('dialog',lambda d:d.accept());page.locator('[data-remove-key]').click();expect(page.locator('#securityMessage')).to_contain_text('通行密钥已移除')
                    assert not errors,errors
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    out=Path('artifacts/auth');out.mkdir(parents=True,exist_ok=True);page.screenshot(path=str(out/'security-phone.png'),full_page=True)
                    browser.close()
                    print('PASS: owner setup; password login/change/logout; real passkey registration/login; invalid signature and replay rejected; removal; mobile layout.')
            except Exception:
                print((root/'server.log').read_text())
                raise
            finally:
                process.terminate()
                try:process.wait(timeout=65)
                except subprocess.TimeoutExpired:process.kill();process.wait()

if __name__=='__main__':main()
