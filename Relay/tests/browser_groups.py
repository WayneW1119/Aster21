"""Standalone, isolated mobile group acceptance. No real agents or models."""
from pathlib import Path
import json,os,signal,socket,subprocess,sys,tempfile,time
from urllib.request import Request,urlopen
from playwright.sync_api import sync_playwright,expect


def main():
 root=Path(__file__).resolve().parents[1]
 with tempfile.TemporaryDirectory(prefix='relay-groups-ui-') as temporary:
  sockets=[socket.socket() for _ in range(2)]
  for sock in sockets:sock.bind(('127.0.0.1',0))
  ports=[s.getsockname()[1] for s in sockets]
  for sock in sockets:sock.close()
  base=f'http://127.0.0.1:{ports[0]}'
  with (Path(temporary)/'server.log').open('w') as log:
   proc=subprocess.Popen([sys.executable,'-P','-m','relay','--root',temporary,'--port',str(ports[0]),'--mcp-port',str(ports[1])],cwd=root,stdout=log,stderr=log,start_new_session=True)
   try:
    def api(token,path,body=None):
     with urlopen(Request(base+'/api/'+path,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},data=json.dumps(body).encode() if body is not None else None),timeout=10) as r:return json.load(r)
    for _ in range(100):
     try:
      token=(Path(temporary)/'owner.token').read_text().strip();api(token,'state');break
     except OSError:time.sleep(.1)
    else:raise AssertionError('Relay did not start')
    alice=api(token,'accounts',{'username':'alice','name':'Alice'})
    bob=api(token,'accounts',{'username':'bob','name':'Bob'})
    with sync_playwright() as playwright:
     executable=playwright.chromium.executable_path
     if not Path(executable).is_file():executable=str(sorted((Path.home()/'.cache/ms-playwright').glob('chromium-*/chrome-linux*/chrome'))[-1])
     browser=playwright.chromium.launch(executable_path=executable,headless=True,args=['--no-sandbox'])
     errors=[]
     def login(credential):
      context=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
      context.add_init_script('sessionStorage.setItem("relay-token",'+json.dumps(credential)+')')
      page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
      page.on('dialog',lambda dialog:dialog.accept());page.goto(base+'/');expect(page.locator('#app')).to_be_visible();return page
     owner=login(token);a=login(alice['token']);b=login(bob['token'])
     owner.locator('#newGroup').tap();owner.locator('#groupName').fill('项目讨论')
     owner.locator('#groupInvitees input[value="alice"]').check();owner.locator('#createGroupBtn').tap()
     expect(owner.locator('#groupCreator')).not_to_be_visible();expect(owner.locator('#peerName')).to_have_text('项目讨论')
     owner.locator('#text').fill('第一条群消息');owner.locator('#sendBtn').tap()
     expect(a.locator('.group-contact')).to_have_count(1,timeout=10000)
     expect(b.locator('.group-contact')).to_have_count(0)
     a.locator('.group-contact').tap();expect(a.locator('.bubble')).to_contain_text('第一条群消息')
     a.locator('#text').fill('Alice 已收到');a.locator('#sendBtn').tap();expect(owner.locator('#messages')).to_contain_text('Alice 已收到')
     gid=api(token,'groups')['groups'][0]['id']
     assert api(token,'messages?peer='+alice['account']['id'])['messages']==[]
     owner.locator('#groupSettings').tap();owner.locator('[data-group-action="mute"][data-username="alice"]').tap()
     expect(owner.locator('[data-group-action="unmute"][data-username="alice"]')).to_be_visible()
     expect(a.locator('#sendBtn')).to_be_disabled(timeout=10000)
     owner.locator('[data-group-action="unmute"][data-username="alice"]').tap();expect(a.locator('#sendBtn')).to_be_enabled(timeout=10000)
     owner.locator('#toggleGroupMute').tap();expect(owner.locator('#toggleGroupMute')).to_have_text('解除全员禁言')
     expect(a.locator('#sendBtn')).to_be_disabled(timeout=10000)
     owner.locator('#toggleGroupMute').tap();expect(a.locator('#sendBtn')).to_be_enabled(timeout=10000)
     owner.locator('#settingsInvitees input[value="bob"]').check();owner.locator('#inviteGroupForm button').tap()
     expect(b.locator('.group-contact')).to_have_count(1,timeout=10000);b.locator('.group-contact').tap()
     expect(b.locator('.bubble')).to_have_count(0)
     owner.locator('#renameGroupName').fill('新版群聊');owner.locator('#renameGroupForm button').tap()
     expect(owner.locator('#groupSettingsTitle')).to_contain_text('新版群聊')
     owner.locator('#closeGroupSettings').tap();owner.locator('#text').fill('新成员可见的消息');owner.locator('#sendBtn').tap()
     expect(b.locator('#messages')).to_contain_text('新成员可见的消息',timeout=10000)
     expect(b.locator('#messages')).not_to_contain_text('第一条群消息')
     artifacts=root/'artifacts';artifacts.mkdir(exist_ok=True)
     owner.screenshot(path=str(artifacts/'phone-group-chat.png'))
     owner.locator('#groupSettings').tap();expect(owner.locator('#groupSettingsDialog')).to_be_visible()
     owner.screenshot(path=str(artifacts/'phone-group-settings.png'))
     assert owner.locator('#groupSettingsDialog').evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
     owner.locator('[data-group-action="remove"][data-username="alice"]').tap()
     expect(a.locator('.group-contact')).to_have_count(0,timeout=10000)
     expect(a.locator('#messages')).not_to_contain_text('第一条群消息')
     owner.locator('#dissolveGroup').tap();expect(owner.locator('#groupSettingsDialog')).not_to_be_visible()
     expect(b.locator('.group-contact')).to_have_count(0,timeout=10000)
     assert not errors,errors
     browser.close();print('PASS: mobile create/invite/group replies/DM isolation/mute/unmute/all-mute/rename/new-member history/removal/dissolve; no models or browser errors')
   finally:
    os.killpg(proc.pid,signal.SIGTERM)
    try:proc.wait(timeout=10)
    except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()

if __name__=='__main__':main()
