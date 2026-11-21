"""Two independent phone apps: real signed WebAuthn, cookies and tab reopen."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from playwright.sync_api import sync_playwright,expect
from browser_utils import chromium_executable

ROOT=Path(__file__).resolve().parents[1]

def authenticator(context,page):
    session=context.new_cdp_session(page);session.send('WebAuthn.enable')
    session.send('WebAuthn.addVirtualAuthenticator',{'options':{'protocol':'ctap2','transport':'internal','hasResidentKey':True,'hasUserVerification':True,'isUserVerified':True,'automaticPresenceSimulation':True}})
    return session

def main():
    artifacts=ROOT/'artifacts/independent-login';artifacts.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='relay-two-apps-') as folder:
        root=Path(folder);sockets=[]
        for _ in range(5):
            s=socket.socket();s.bind(('127.0.0.1',0));sockets.append(s)
        ports=[s.getsockname()[1] for s in sockets]
        for s in sockets:s.close()
        base=f'http://localhost:{ports[0]}'
        args={'root':folder,'port':ports[0],'primary_port':ports[1],'mcp_port':ports[2],
            'relay_port':ports[3],'relay_mcp_port':ports[4],'name':'Independent apps','mode':'fake',
            'models':'','responses_url':'https://api.openai.com/v1/responses','model_catalog':'',
            'allow_http':False,'public_url':base}
        source='import argparse,asyncio;from squad.fleet import serve;asyncio.run(serve(argparse.Namespace(**'+repr(args)+')))'
        command=[sys.executable,'-P','-c',source]
        with (artifacts/'server.log').open('w') as log:
            process=subprocess.Popen(command,cwd=ROOT,stdout=log,stderr=log,start_new_session=True)
            try:
                def api(path,body=None):
                    request=urllib.request.Request(base+'/squad/api/'+path,data=None if body is None else json.dumps(body).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json','Origin':base})
                    return json.load(urllib.request.urlopen(request,timeout=40))
                for _ in range(150):
                    try:
                        token=(root/'control/owner.token').read_text().strip()
                        if api('state')['resources'][0]['status']=='running':break
                    except (OSError,ValueError):pass
                    time.sleep(.1)
                else:raise AssertionError('Test console did not start')
                api('squads/studio/agents',{'handle':'peer','instruction':'Reply in Relay.'})
                bundle=api('relay-connections?selected=studio')
                with sync_playwright() as p:
                    browser=p.chromium.launch(executable_path=chromium_executable(p),headless=True,args=['--no-sandbox'])
                    context=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
                    errors=[]
                    page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)));authenticator(context,page)
                    page.goto(base+'/squad/');page.locator('#token').fill(token);page.locator('#loginForm button').click();expect(page.locator('#app')).to_be_visible()
                    page.locator('#mobileSettings').click();page.locator('#securityUsername').fill('workspace-owner');page.locator('#newPassword').fill('workspace-password-test');page.locator('#confirmPassword').fill('workspace-password-test');page.locator('#passwordSettingsForm button').click();expect(page.locator('#securityMessage')).to_contain_text('账号密码已保存')
                    page.locator('#registerPasskey').click();expect(page.locator('#securityMessage')).to_contain_text('通行密钥已绑定',timeout=20000)
                    page.locator('[data-close=securityDialog]').last.click()
                    relay=context.new_page();relay.on('pageerror',lambda e:errors.append(str(e)));authenticator(context,relay)
                    relay.goto(base+'/relay/');expect(relay.locator('#passwordLoginForm')).to_be_visible()
                    relay.locator('#connectFromWorkspace').click();expect(relay.locator('#app')).to_be_visible(timeout=15000)
                    expect(relay.locator('#loginSecurityDialog')).to_be_visible();legacy_token=relay.evaluate('active.token');relay.locator('#relayNewPassword').fill('relay-different-password');relay.locator('#relayConfirmPassword').fill('relay-different-password');relay.locator('#passwordSettingsForm button').click();expect(relay.locator('#relaySecurityMessage')).to_contain_text('Relay 密码已保存')
                    relay.evaluate('async token=>{upsert({url:active.url,token,browser:false});await activate({url:active.url,browser:true});}',legacy_token)
                    assert relay.evaluate('active.browser && !active.token')
                    relay.locator('#relayPasskeyName').fill('Phone Relay');relay.locator('#registerRelayPasskey').click();expect(relay.locator('#relaySecurityMessage')).to_contain_text('绑定成功',timeout=20000)
                    relay.locator('#closeLoginSecurity').click()
                    # Squad logout cannot log out Relay; a new tab needs no URL token.
                    page.locator('#mobileNav').click();page.locator('#logout').click();expect(page.locator('#passwordLoginForm')).to_be_visible()
                    reopened=context.new_page();reopened.goto(base+'/relay/');expect(reopened.locator('#app')).to_be_visible(timeout=15000)
                    reopened.locator('.contact').filter(has_text='@peer').tap();reopened.locator('#text').fill('Independent Relay session');reopened.locator('#sendBtn').tap();expect(reopened.locator('.bubble').filter(has_text='[演示模式 · peer] 收到：Independent Relay session')).to_have_count(1,timeout=20000)
                    # Relay passkey login works while Squad is logged out.
                    relay.locator('#logoutBtn').click();expect(relay.locator('#passwordLoginForm')).to_be_visible()
                    relay.locator('#passkeyLogin').click();expect(relay.locator('#app')).to_be_visible(timeout=20000)
                    # Invalid signed assertion and replay both fail.
                    result=relay.evaluate('''async()=>{const o=await relayAuth('passkeys/login/options',{});o.challenge=relayBytes(o.challenge);const c=relayCredential(await navigator.credentials.get({publicKey:o}));c.response.signature='AAAA';const send=()=>fetch(active.url+'/api/auth/passkeys/login/verify',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({credential:c})});return [(await send()).status,(await send()).status];}''')
                    assert result==[400,400],result
                    # Squad's existing key also remains usable, independent of Relay.
                    page.locator('#passkeyLogin').click();expect(page.locator('#app')).to_be_visible(timeout=20000)
                    relay.locator('#logoutBtn').click();expect(relay.locator('#passwordLoginForm')).to_be_visible()
                    page.reload();expect(page.locator('#app')).to_be_visible()
                    relay.locator('#loginUsername').fill('user');relay.locator('#loginPassword').fill('workspace-password-test');relay.locator('#passwordLoginBtn').click();expect(relay.locator('#loginError')).to_contain_text('账号或密码不正确')
                    relay.locator('#loginPassword').fill('relay-different-password');relay.locator('#passwordLoginBtn').click();expect(relay.locator('#app')).to_be_visible()
                    relay.locator('#domainSwitcher').click();relay.locator('#loginAnotherDomain').click();expect(relay.locator('#passwordLoginForm')).to_be_visible()
                    relay.locator('#loginConnections a').first.click();expect(relay.locator('#app')).to_be_visible()
                    saved=relay.evaluate("localStorage.getItem('relay-browser-connections')")
                    assert 'token' not in saved,saved
                    relay.locator('#loginSecurity').click();relay.screenshot(path=str(artifacts/'relay-security.png'));relay.locator('#closeLoginSecurity').click()
                    relay.locator('#logoutBtn').click();expect(relay.locator('#passwordLoginForm')).to_be_visible();relay.screenshot(path=str(artifacts/'relay-login.png'))
                    assert not errors,errors
                    browser.close()
                print('PASS: separate phone tabs; independent passwords; both real passkey registrations/logins; cross-app logout isolation; new Relay tab without URL tokens; real chat; invalid signature/replay rejected; no JS errors.')
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:process.wait(timeout=55)
                    except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()

if __name__=='__main__':main()
