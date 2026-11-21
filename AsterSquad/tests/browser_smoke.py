"""Optional browser acceptance. Requires playwright and an installed Chromium.

Run against the local demo: python tests/browser_smoke.py
Also launches a completely independent Relay domain for two-browser tests.
Never writes credentials into screenshots or test output.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright, expect
from browser_utils import chromium_executable

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
ARTIFACTS.mkdir(exist_ok=True)


def get(url, token):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": "Bearer " + token}), timeout=10) as response:
        return json.load(response)


def login(page, url, token, kind="relay"):
    page.goto(url)
    if kind=="relay":page.locator("#tokenLoginDetails").evaluate("e=>e.open=true")
    page.locator("#token").fill(token)
    page.locator("#loginForm button" if kind == "relay" else "#login button").click()
    expect(page.locator("#app")).to_be_visible()


def main():
    relay_token = (ROOT / "data/control/relay/owner.token").read_text().strip()
    squad_token = (ROOT / "data/control/owner.token").read_text().strip()
    request=urllib.request.Request("http://127.0.0.1:19910/api/squads/studio/bulk", data=json.dumps({"action":"start"}).encode(), headers={"Authorization":"Bearer "+squad_token,"Content-Type":"application/json"})
    with urllib.request.urlopen(request,timeout=90) as response:
        result=json.load(response)
        assert not result["failed"], "Demo agents could not be started"
    with sync_playwright() as p:
        executable = chromium_executable(p)
        browser = p.chromium.launch(headless=True, executable_path=executable, args=["--no-sandbox"])
        errors = []
        context = browser.new_context(viewport={"width": 1440, "height": 960}, device_scale_factor=1)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        login(page, "http://127.0.0.1:19920", relay_token)
        state = get("http://127.0.0.1:19920/api/state", relay_token)
        lin = next(a for a in state["accounts"] if a["username"] == "lin")
        page.locator(f'button.contact[data-id="{lin["id"]}"]').click()
        text = "我们先把这套简单的协作环境跑顺，再一起讨论后续设计。"
        replies = page.locator(".bubble").filter(has_text="[演示模式 · lin] 收到：" + text)
        existing = get("http://127.0.0.1:19920/api/messages?peer="+lin["id"], relay_token)["messages"]
        previous_replies = sum(row["text"] == "[演示模式 · lin] 收到："+text for row in existing)
        page.locator("#text").fill(text)
        page.locator("#sendBtn").click()
        expect(replies).to_have_count(previous_replies + 1, timeout=20000)
        expect(replies.last).to_be_visible(timeout=20000)
        expect(page.locator("#banner")).not_to_be_visible()
        page.screenshot(path=str(ARTIFACTS / "relay-desktop.png"), full_page=True)
        page.locator("#manageBtn").click()
        expect(page.locator("#managedList")).to_contain_text("lin")
        page.screenshot(path=str(ARTIFACTS / "relay-accounts.png"), full_page=True)
        page.locator("#closeManager").click()
        page.set_viewport_size({"width": 390, "height": 844})
        expect(page.locator("#composer")).to_be_visible()
        page.screenshot(path=str(ARTIFACTS / "relay-mobile.png"), full_page=True)
        page.locator("#backBtn").click()
        expect(page.locator(".sidebar")).to_be_visible()
        page.set_viewport_size({"width": 1440, "height": 960})
        squad_page = context.new_page()
        squad_page.on("pageerror", lambda error: errors.append(str(error)))
        login(squad_page, "http://127.0.0.1:19910", squad_token, "squad")
        squad_page.locator('#viewInstruction').click()
        expect(squad_page.locator("#instructionText")).to_contain_text("用户的 Relay 用户名")
        squad_page.screenshot(path=str(ARTIFACTS / "squad-instruction.png"), full_page=True)
        # Completely independent standalone Relay, without Squad or Aster.
        with tempfile.TemporaryDirectory(prefix="relay-browser-") as temporary:
            sockets = [socket.socket(), socket.socket()]
            for sock in sockets:
                sock.bind(("127.0.0.1", 0))
            ports = [sock.getsockname()[1] for sock in sockets]
            for sock in sockets:
                sock.close()
            proc = subprocess.Popen([sys.executable, "-m", "relay", "--root", temporary, "--port", str(ports[0]), "--mcp-port", str(ports[1])], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                url = f"http://127.0.0.1:{ports[0]}"
                for _ in range(100):
                    try:
                        owner = (Path(temporary) / "owner.token").read_text().strip()
                        standalone = get(url + "/api/state", owner)
                        break
                    except (OSError, ValueError):
                        time.sleep(.1)
                else:
                    raise AssertionError("Standalone Relay did not start")
                admin = context.new_page()
                admin.on("pageerror", lambda error: errors.append(str(error)))
                login(admin, url, owner)
                admin.locator("#manageBtn").click()
                admin.locator("#newHandle").fill("visitor")
                admin.locator("#newName").fill("访客")
                admin.locator("#canCreate").uncheck()
                admin.locator("#createAccount button").click()
                expect(admin.locator("#credential")).to_be_visible()
                guest_token = admin.locator("#credentialValue").inner_text()
                admin.locator("#closeCredential").click()
                admin.locator("#closeManager").click()
                other = browser.new_context(viewport={"width": 1100, "height": 800})
                guest = other.new_page()
                guest.on("pageerror", lambda error: errors.append(str(error)))
                login(guest, url, guest_token)
                owner_id = standalone["me"]["id"]
                guest.locator(f'button.contact[data-id="{owner_id}"]').click()
                message = '<img src=x onerror="window.INJECTED=true"> 两个网页账号独立收发'
                guest.locator("#text").fill(message)
                guest.locator("#sendBtn").click()
                state2 = get(url + "/api/state", owner)
                visitor = next(a for a in state2["accounts"] if a["username"] == "visitor")
                expect(admin.locator(f'button.contact[data-id="{visitor["id"]}"] .badge')).to_have_text("1", timeout=8000)
                admin.locator(f'button.contact[data-id="{visitor["id"]}"]').click()
                expect(admin.locator(".bubble").last).to_have_text(message)
                assert admin.evaluate("window.INJECTED") is None
                expect(guest.locator(".receipt").last).to_have_text("已读", timeout=8000)
                admin.locator("#text").fill("网页版私聊验证通过。")
                admin.locator("#sendBtn").click()
                expect(guest.locator(".bubble").last).to_have_text("网页版私聊验证通过。", timeout=8000)
                admin.locator("#manageBtn").click()
                admin.on("dialog", lambda dialog: dialog.accept())
                admin.locator(f'[data-account="{visitor["id"]}"][data-action="reset_token"]').click()
                expect(admin.locator("#credential")).to_be_visible()
                new_token = admin.locator("#credentialValue").inner_text()
                assert new_token != guest_token
                admin.locator("#closeCredential").click()
                expect(guest.locator("#login")).to_be_visible(timeout=8000)
                login(guest, url, new_token)
                admin.locator(f'[data-account="{visitor["id"]}"][data-action="disable"]').click()
                expect(guest.locator("#login")).to_be_visible(timeout=8000)
                admin.locator(f'[data-account="{visitor["id"]}"][data-action="enable"]').click()
                login(guest, url, new_token)
                other.close()
                print("Standalone Relay: two-browser messaging, unread/read, XSS escaping, create/reset/disable/enable: PASS")
            finally:
                proc.terminate()
                proc.wait(timeout=10)
        assert not errors, errors
        browser.close()
    print("Squad instruction UI, desktop/mobile Relay, browser errors: PASS")
    print("Screenshots:", ARTIFACTS)


if __name__ == "__main__":
    main()
