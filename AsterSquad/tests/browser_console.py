"""Browser acceptance for the console and multi-domain Relay, fully isolated."""
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
from urllib.parse import unquote,urlsplit

from playwright.sync_api import sync_playwright,expect
from browser_utils import chromium_executable
from mobile_viewport import VIEWPORT_MOCK, verify_squad_keyboard, verify_relay_keyboard, verify_member_modal

ROOT=Path(__file__).resolve().parents[1]


def main():
    (ROOT/'artifacts/mobile-repair').mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aster-ui-") as temporary:
        root=Path(temporary)
        sockets=[socket.socket() for _ in range(5)]
        for sock in sockets:sock.bind(("127.0.0.1",0))
        ports=[sock.getsockname()[1] for sock in sockets]
        for sock in sockets:sock.close()
        command=[sys.executable,"-m","squad.fleet","--root",temporary]
        catalog=root/'mcp-services.json'
        catalog.write_text(json.dumps({
            'web':{'name':'网页检索 · 测试服务','default':True,'server':{'url':'http://127.0.0.1:1/mcp','enabled':False}},
            'offline':{'name':'未安装的测试服务','default':False,'server':{'command':str(root/'missing-server'),'required':False}},
        }))
        command.extend(['--mcp-catalog',str(catalog)])
        for flag,port in zip(["--port","--primary-port","--mcp-port","--relay-port","--relay-mcp-port"],ports):command.extend([flag,str(port)])
        with (root/"console.log").open("w") as log:
            process=subprocess.Popen(command,cwd=ROOT,stdout=log,stderr=log,start_new_session=True)
            try:
                prefix=os.environ.get("ASTER_BROWSER_PREFIX", "").rstrip("/")
                url=f"http://127.0.0.1:{ports[0]}"+prefix
                def api(path):
                    with urllib.request.urlopen(urllib.request.Request(url+"/api/"+path,headers={"Authorization":"Bearer "+token}),timeout=30) as response:return json.load(response)
                for _ in range(150):
                    try:
                        token=(root/"control/owner.token").read_text().strip()
                        state=api("state")
                        if state["resources"][0]["status"]=="running":break
                    except (OSError,ValueError):pass
                    time.sleep(.1)
                else:raise AssertionError("Console failed to start")
                with sync_playwright() as p:
                    executable=chromium_executable(p)
                    browser=p.chromium.launch(executable_path=executable,headless=True,args=["--no-sandbox"])
                    context=browser.new_context(viewport={"width":1600,"height":1000})
                    errors=[]
                    context.on("page",lambda page:page.on("pageerror",lambda error:errors.append(str(error))))
                    page=context.new_page();page.on("dialog",lambda dialog:dialog.accept())
                    page.goto(url);page.locator("#token").fill(token);page.locator("#loginForm button[type=submit]").click();expect(page.locator("#app")).to_be_visible()
                    page.locator('#securitySettings').click()
                    expect(page.locator('#consoleAutoStart')).to_be_disabled()
                    expect(page.locator('#consoleStartupHint')).to_contain_text('手动启动')
                    page.locator('[data-close="securityDialog"]').first.click()
                    page.locator("#newSquadSide").click();page.locator("#resourceName").fill("浏览器研究组");page.locator("#resourceDescription").fill("用清晰的分支组织长期协作。");page.locator("#saveResource").click()
                    expect(page.locator("#squadTitle")).to_have_text("浏览器研究组",timeout=15000)
                    expect(page.locator(".squad-item")).to_have_count(2)
                    page.locator('#resourceSettings').click()
                    expect(page.locator('#resourceRestoreMembers')).not_to_be_checked()
                    page.locator('#resourceRestoreMembers').check()
                    page.locator('#saveResource').click()
                    expect(page.locator('#resourceDialog')).not_to_be_visible()
                    page.locator('#resourceSettings').click()
                    expect(page.locator('#resourceRestoreMembers')).to_be_checked()
                    page.locator('[data-close="resourceDialog"]').first.click()
                    page.locator("#newAgent").click();page.locator("#agentHandle").fill("lead");page.locator("#agentInstruction").fill("负责梳理目标，协调资料研究，并跟进重要事项。")
                    expect(page.locator('#agentAutoStart')).to_have_count(0)
                    expect(page.locator('#squadBaseSuite')).to_be_visible()
                    expect(page.locator('#agentConnectMcp')).not_to_be_checked()
                    page.locator('#agentConnectMcp').check()
                    expect(page.locator('#agentLocalIpc')).not_to_be_checked()
                    expect(page.locator('#agentLocalIpc')).to_be_disabled()
                    expect(page.locator('#agentWindowsAdmin')).to_have_count(1)
                    page.locator("#saveAgent").click()
                    expect(page.locator(".node")).to_have_count(1,timeout=15000)
                    page.locator('#editAgent').click()
                    expect(page.locator('#mcpServiceChoices input[value="web"]')).to_be_checked()
                    page.locator('#mcpServiceChoices input[value="web"]').uncheck()
                    page.locator('#saveAgent').click()
                    expect(page.locator('#agentDialog')).not_to_be_visible(timeout=15000)
                    first=api("state");squad=next(r for r in first["resources"] if r["name"]=="浏览器研究组");sid=squad["id"]
                    lead=api(f"squads/{sid}/state")["agents"][0]
                    assert lead['mcp_services']==[]
                    assert lead['config']['allow_connect'] is True
                    assert lead['config']['relay_namespace'].startswith('relay_')
                    assert lead['config']['auto_start'] is False
                    # A connected worker can accept a configuration even when
                    # an optional server fails. Show that distinction in the UI.
                    page.locator('#editAgent').click()
                    page.locator('#mcpServiceChoices input[value="offline"]').check()
                    page.locator('#saveAgent').click()
                    expect(page.locator('#agentDialog')).not_to_be_visible(timeout=15000)
                    expect(page.locator('#detailPanel')).to_contain_text('这些服务未连接：offline',timeout=15000)
                    after=api(f"squads/{sid}/state")["agents"][0]
                    assert after['thread']==lead['thread']
                    page.locator('#editAgent').click()
                    page.locator('#mcpServiceChoices input[value="offline"]').uncheck()
                    page.locator('#saveAgent').click()
                    expect(page.locator('#agentDialog')).not_to_be_visible(timeout=15000)
                    expect(page.locator('#detailPanel')).not_to_contain_text('这些服务未连接')
                    page.locator(f'[data-parent="{lead["id"]}"]').click();expect(page.locator("#agentParent")).to_have_value(lead["id"])
                    page.locator("#agentHandle").fill("researcher");page.locator("#agentInstruction").fill("核对资料与来源，持续维护研究记录。");page.locator("#saveAgent").click()
                    expect(page.locator(".node")).to_have_count(2,timeout=15000)
                    child=next(a for a in api(f"squads/{sid}/state")["agents"] if a["handle"]=="researcher")
                    self_branch=page.locator(f'.node[data-agent="{lead["id"]}"]').locator("..")
                    expect(self_branch.locator("ul .node")).to_have_count(1)
                    page.locator("#editAgent").click();page.locator("#agentInstruction").fill("核对研究结论，标记证据与不确定性。");page.locator("#saveAgent").click()
                    expect(page.locator(".role-text")).to_have_text("核对研究结论，标记证据与不确定性。",timeout=15000)
                    expect(page.locator("#agentRun")).to_have_text("正常暂停")
                    page.locator("#agentRun").click();expect(page.locator("#agentRun")).to_have_text("启动成员",timeout=10000)
                    page.locator("#agentRun").click();expect(page.locator("#agentRun")).to_have_text("正常暂停",timeout=10000)
                    page.locator("#archiveAgent").click();expect(page.locator("#restoreAgent")).to_be_visible(timeout=10000)
                    expect(page.locator(".node")).to_have_count(1)
                    page.locator("#restoreAgent").click();expect(page.locator("#editAgent")).to_be_visible();expect(page.locator(".node")).to_have_count(2)
                    current=next(a for a in api(f"squads/{sid}/state")["agents"] if a["handle"]=="researcher")
                    assert current["thread"]==child["thread"]
                    page.locator("#listView").click();expect(page.locator("#memberTable tbody tr")).to_have_count(2);page.locator("#treeView").click()
                    page.locator("#domainsNav").click();page.locator("#newDomain").click();page.locator("#resourceName").fill("开放讨论");page.locator("#saveResource").click()
                    expect(page.locator(".domain-card")).to_have_count(3,timeout=15000)
                    with page.expect_popup() as popup_info:page.locator("#openRelay").click()
                    relay=popup_info.value;expect(relay.locator("#app")).to_be_visible(timeout=15000)
                    expect(relay.locator("#domainName")).to_have_text("浏览器研究组")
                    expect(relay.locator(".rail-domain")).to_have_count(3)
                    relay.locator(".contact").filter(has_text="@lead").click();text="这条消息只属于研究组。";relay.locator("#text").fill(text);relay.locator("#sendBtn").click()
                    expect(relay.locator(".bubble").filter(has_text="[演示模式 · lead] 收到："+text)).to_have_count(1,timeout=20000)
                    relay.locator('.rail-domain[aria-label="切换到 开放讨论"]').click();expect(relay.locator("#domainName")).to_have_text("开放讨论")
                    expect(relay.locator(".contact")).to_have_count(0);expect(relay.locator("#composer")).not_to_be_visible();expect(relay.locator("#messages")).not_to_contain_text(text)
                    relay.locator('.rail-domain[aria-label="切换到 浏览器研究组"]').click();expect(relay.locator("#domainName")).to_have_text("浏览器研究组")
                    relay.locator(".contact").filter(has_text="@lead").click();expect(relay.locator("#messages")).to_contain_text(text)
                    relay.locator("#domainSwitcher").click();expect(relay.locator(".domain-connection")).to_have_count(3)
                    relay.on("dialog",lambda dialog:dialog.accept())
                    relay.locator('.domain-connection').filter(has_text="Aster Studio").locator('[data-remove]').click();expect(relay.locator(".rail-domain")).to_have_count(2)
                    bundle=json.loads(unquote(urlsplit(api("relay-connections")["url"]).fragment.removeprefix("connections=")))
                    studio=next(c for c in bundle["connections"] if c["label"]=="Aster Studio")
                    relay.locator("#connectUrl").fill(studio["url"]);relay.locator("#connectToken").fill(studio["token"]);relay.locator("#connectDomainButton").click()
                    expect(relay.locator("#domainName")).to_have_text("Aster Studio");expect(relay.locator(".rail-domain")).to_have_count(3)
                    relay.set_viewport_size({"width":390,"height":844});expect(relay.locator("#domainSwitcher")).to_be_visible();relay.locator("#domainSwitcher").click();expect(relay.locator("#domainPicker")).to_be_visible();relay.locator("#closeDomainPicker").click()
                    page.set_viewport_size({"width":390,"height":844});
                    if page.locator("#closeDetail").is_visible():page.locator("#closeDetail").click()
                    page.locator("#mobileNav").click();expect(page.locator("#squadList")).to_be_visible();page.locator(f'[data-squad="{sid}"]').click();expect(page.locator(".tree-canvas")).to_be_visible()
                    # First member on a phone: invalid input must explain itself,
                    # then a valid submit must create a live member in this space.
                    phone_context=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True,service_workers='block')
                    phone_context.add_init_script(VIEWPORT_MOCK)
                    phone_context.add_init_script('sessionStorage.setItem("squad-token",'+json.dumps(token)+')')
                    page=phone_context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
                    page.on('console',lambda message:errors.append(message.text) if message.type=='error' else None)
                    page.goto(url);expect(page.locator('#app')).to_be_visible()
                    page.locator('#mobileSpaces').tap();page.locator('#newSquadSide').tap()
                    page.locator('#resourceName').fill('手机新空间');page.locator('#saveResource').click()
                    expect(page.locator('#squadTitle')).to_have_text('手机新空间',timeout=15000)
                    verify_squad_keyboard(page)
                    page.locator('#newAgent').click();page.locator('#saveAgent').click()
                    expect(page.locator('#agentFormError')).to_contain_text('成员用户名')
                    page.locator('#agentHandle').fill('PhoneAgent');page.locator('#agentInstruction').fill('负责手机端验证。')
                    page.locator('#saveAgent').click();expect(page.locator('#agentFormError')).to_contain_text('小写字母')
                    page.locator('#agentHandle').fill('phone_agent');page.locator('#agentInstruction').fill('')
                    page.locator('#saveAgent').click();expect(page.locator('#agentFormError')).to_contain_text('身份与职责')
                    page.locator('#agentInstruction').fill('负责手机端验证。')
                    expect(page.locator('#mcpServiceChoices input[value="web"]')).to_be_checked()
                    page.locator('#mcpServiceChoices input[value="web"]').uncheck()
                    page.locator('#mcpServiceChoices input[value="web"]').check()
                    page.locator('#saveAgent').click()
                    expect(page.locator('#agentDialog')).not_to_be_visible(timeout=15000)
                    expect(page.locator('#detailPanel')).to_contain_text('phone_agent')
                    expect(page.locator('#agentRun')).to_have_text('正常暂停')
                    verify_member_modal(page)
                    # Three permission lists, long-path touch editing, persistence
                    # and deletion all use the real API in an isolated fake squad.
                    reference=root/'phone-reference';reference.mkdir()
                    private=root/'phone-private';private.mkdir()
                    new_path=root/'phone-new'/'a-long-project-directory-for-mobile-editing'/'nested'
                    page.locator('#editAgent').tap()
                    expect(page.locator('.permission-group')).to_have_count(3)
                    # The global read grant is a normal editable row; remove it.
                    expect(page.get_by_role('textbox',name='只读路径',exact=True)).to_have_value('/')
                    page.get_by_role('button',name='删除只读路径',exact=True).tap()
                    page.get_by_role('button',name='添加可读写路径',exact=True).tap()
                    page.locator('#saveAgent').click()
                    expect(page.locator('#agentFormError')).to_contain_text('授权路径')
                    page.get_by_role('textbox',name='可读写路径',exact=True).fill(str(new_path))
                    page.get_by_role('button',name='添加只读路径',exact=True).tap()
                    page.get_by_role('textbox',name='只读路径',exact=True).fill(str(reference))
                    page.get_by_role('button',name='添加禁止访问路径',exact=True).tap()
                    page.get_by_role('textbox',name='禁止访问路径',exact=True).fill(str(private))
                    assert page.locator('#agentDialog').evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
                    page.locator('#grantList-write').scroll_into_view_if_needed()
                    page.screenshot(path=str(ROOT/'artifacts/mobile-repair/phone-permissions.png'))
                    page.locator('#saveAgent').click();expect(page.locator('#agentDialog')).not_to_be_visible(timeout=15000)
                    assert new_path.is_dir()
                    phone_sid=next(r['id'] for r in api('state')['resources'] if r['name']=='手机新空间')
                    saved=next(a for a in api(f'squads/{phone_sid}/state')['agents'] if a['handle']=='phone_agent')
                    assert {g['access'] for g in saved['config']['grants']}=={'read','write','deny'}
                    saved_thread=saved['thread']
                    page.locator('#editAgent').tap()
                    expect(page.get_by_role('textbox',name='可读写路径',exact=True)).to_have_value(str(new_path))
                    page.get_by_role('button',name='删除可读写路径',exact=True).tap()
                    page.get_by_role('button',name='删除只读路径',exact=True).tap()
                    page.get_by_role('button',name='删除禁止访问路径',exact=True).tap()
                    expect(page.locator('#grantCount-write')).to_have_text('0')
                    page.locator('#saveAgent').click();expect(page.locator('#agentDialog')).not_to_be_visible(timeout=15000)
                    saved=next(a for a in api(f'squads/{phone_sid}/state')['agents'] if a['handle']=='phone_agent')
                    assert saved['config']['grants']==[] and saved['thread']==saved_thread and new_path.is_dir()
                    # Deterministic provider usage fixture; no paid model request.
                    def measured_state(route):
                        response=route.fetch();body=response.json()
                        for member in body.get('agents',[]):
                            if member['handle']=='phone_agent':
                                member['telemetry']={'phase':'waiting','latest':{'input_tokens':120000,'output_tokens':350,'cached_tokens':84000,'at':time.time(),'model':member['config']['model']},'cache_window':{'requests':20,'reported':19,'hit_rate':.85}}
                        route.fulfill(response=response,json=body)
                    page.route('**/api/squads/'+phone_sid+'/state',measured_state)
                    expect(page.locator('.runtime-metrics')).to_contain_text('120,000',timeout=10000)
                    expect(page.locator('.runtime-metrics')).to_contain_text('70.0%')
                    expect(page.locator('.runtime-metrics')).to_contain_text('85.0%')
                    expect(page.locator('#detailPanel')).to_contain_text('等待事件')
                    for width in (320,390):
                        page.set_viewport_size({'width':width,'height':844})
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'metrics overflow'
                    page.screenshot(path=str(ROOT/'artifacts/mobile-repair/agent-runtime-phone.png'),full_page=True)
                    page.set_viewport_size({'width':1600,'height':1000})
                    page.screenshot(path=str(ROOT/'artifacts/mobile-repair/agent-runtime-desktop.png'),full_page=True)
                    page.set_viewport_size({'width':390,'height':844})
                    page.locator('#closeDetail').click();expect(page.locator('#detailPanel')).not_to_be_visible()
                    page.locator('#listView').click();expect(page.locator('#memberTable tbody tr')).to_have_count(1)
                    page.locator('#memberTable tbody tr').click();expect(page.locator('#detailPanel')).to_be_visible()
                    page.locator('#closeDetail').click()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'phone page overflow'
                    page.screenshot(path=str(ROOT/'artifacts/mobile-repair/squad-phone.png'),full_page=True)
                    # Both independent apps expose valid install assets through the console.
                    for path in ('manifest.webmanifest','icon-192.png','sw.js','app-shell.js'):
                        assert page.request.get(url+'/'+path).ok,path
                        assert page.request.get(url+'/relay/'+path if not prefix else url.removesuffix(prefix)+'/relay/'+path).ok,path
                    bundle=api('relay-connections?selected='+sid)
                    page.goto(bundle['url']);expect(page.locator('#app')).to_be_visible(timeout=15000)
                    page.locator('.contact').filter(has_text='@lead').tap()
                    page.locator('#text').fill('第一行');page.locator('#text').press('Enter');page.locator('#text').type('第二行')
                    expect(page.locator('#text')).to_have_value('第一行\n第二行')
                    verify_relay_keyboard(page)
                    # A compact visible viewport simulates the keyboard taking space.
                    page.set_viewport_size({'width':390,'height':480})
                    page.wait_for_function('document.querySelector("#composer").getBoundingClientRect().bottom <= 481')
                    composer=page.locator('#composer').bounding_box()
                    assert composer['y']+composer['height']<=481,composer
                    page.locator('#sendBtn').tap()
                    expect(page.locator('.bubble').filter(has_text='[演示模式 · lead] 收到：第一行')).to_have_count(1,timeout=20000)
                    page.set_viewport_size({'width':390,'height':844})
                    page.screenshot(path=str(ROOT/'artifacts/mobile-repair/relay-phone.png'),full_page=True)
                    assert page.locator('#workspaceChatLink').count()==0
                    for width in (320,430):
                        page.set_viewport_size({'width':width,'height':740})
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),width
                    # Delete via the phone UI, keep the data, then reuse the name.
                    page.goto(url);expect(page.locator('#app')).to_be_visible()
                    page.locator('#resourceSettings').tap()
                    expect(page.locator('#resourceRemoval')).to_be_visible()
                    page.once('dialog',lambda dialog:dialog.accept())
                    page.locator('#deleteResource').tap()
                    expect(page.locator('#resourceDialog')).not_to_be_visible(timeout=20000)
                    expect(page.locator('[data-squad="'+phone_sid+'"]')).to_have_count(0)
                    expect(page.locator('#notice')).to_contain_text('数据已保留')
                    page.locator('#mobileSpaces').tap();page.locator('#newSquadSide').tap()
                    page.locator('#resourceName').fill('手机新空间');page.locator('#saveResource').tap()
                    expect(page.locator('#squadTitle')).to_have_text('手机新空间',timeout=15000)
                    assert not errors,errors
                    browser.close()
                print("PASS: create Squad; add root/child; edit/pause/archive/restore same Thread; member list; independent domain; domain import/switch/remove/reconnect; cross-domain message isolation; mobile keyboard resize/pan, modal inputs, history touch scrolling and zoom; no browser exceptions.")
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:process.wait(timeout=55)
                    except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()


if __name__=="__main__":main()
