"""Regression: touch-scroll past group chats to the last direct conversation."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen

from playwright.sync_api import expect, sync_playwright


def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="relay-sidebar-") as temporary:
        sockets = [socket.socket() for _ in range(2)]
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        ports = [sock.getsockname()[1] for sock in sockets]
        for sock in sockets:
            sock.close()
        base = f"http://127.0.0.1:{ports[0]}"
        with (Path(temporary) / "server.log").open("w") as log:
            proc = subprocess.Popen(
                [sys.executable, "-P", "-m", "relay", "--root", temporary,
                 "--port", str(ports[0]), "--mcp-port", str(ports[1])],
                cwd=root, stdout=log, stderr=log, start_new_session=True)
            try:
                def api(path, body=None):
                    request = Request(base + "/api/" + path,
                                      headers={"Authorization": "Bearer " + token,
                                               "Content-Type": "application/json"},
                                      data=json.dumps(body).encode() if body is not None else None)
                    with urlopen(request, timeout=10) as response:
                        return json.load(response)

                for _ in range(100):
                    try:
                        token = (Path(temporary) / "owner.token").read_text().strip()
                        api("state")
                        break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise AssertionError("Relay did not start")
                members = [api("accounts", {"username": f"agent{i:02}", "name": f"Agent {i:02}"})
                           for i in range(14)]
                for i in range(2):
                    api("groups", {"name": f"讨论群 {i + 1}", "members": ["agent00"]})
                with sync_playwright() as playwright:
                    executable = playwright.chromium.executable_path
                    if not Path(executable).is_file():
                        executable = str(sorted((Path.home() / ".cache/ms-playwright").glob(
                            "chromium-*/chrome-linux*/chrome"))[-1])
                    browser = playwright.chromium.launch(
                        executable_path=executable, args=["--no-sandbox"])
                    errors = []
                    for width, height in [(390, 844), (375, 667), (320, 568), (844, 390), (1920, 1080), (2560, 1440)]:
                        context = browser.new_context(viewport={"width": width, "height": height},
                                                      is_mobile=True, has_touch=True)
                        context.add_init_script('sessionStorage.setItem("relay-token",' + json.dumps(token) + ')')
                        page = context.new_page()
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(base + "/")
                        expect(page.locator("#app")).to_be_visible()
                        app = page.locator("#app").bounding_box()
                        assert abs(app["x"]) <= 1 and abs(app["width"] - width) <= 1, (width, app)
                        expect(page.locator(".group-contact")).to_have_count(2)
                        scroll = page.locator(".conversation-list")
                        bounds = scroll.bounding_box()
                        assert bounds["height"] >= 130, bounds
                        cdp = context.new_cdp_session(page)
                        # Actual touch gestures, rather than scrollIntoView hiding a layout bug.
                        for _ in range(16):
                            at_end = scroll.evaluate("el=>el.scrollTop+el.clientHeight>=el.scrollHeight-2")
                            if at_end:
                                break
                            x = bounds["x"] + bounds["width"] / 2
                            y = bounds["y"] + bounds["height"] * .85
                            cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
                            for step in range(1, 13):
                                cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": y - bounds["height"] * .7 * step / 12}]})
                                page.wait_for_timeout(16)
                            cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
                            page.wait_for_timeout(180)
                        last = page.locator('#contacts [data-id="' + members[-1]["account"]["id"] + '"]')
                        rect = last.bounding_box()
                        assert rect["y"] >= bounds["y"] and rect["y"] + rect["height"] <= bounds["y"] + bounds["height"] + 1, (width, bounds, rect, scroll.evaluate("el=>({top:el.scrollTop,height:el.scrollHeight,client:el.clientHeight})"))
                        last.tap()
                        expect(page.locator("#peerName")).to_have_text("Agent 13")
                        expect(page.locator("#text")).to_be_visible()
                        if width <= 650:
                            page.locator("#backBtn").tap()
                            expect(scroll).to_be_visible()
                        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
                        if width == 390:
                            artifacts = root / "artifacts"
                            artifacts.mkdir(exist_ok=True)
                            page.screenshot(path=str(artifacts / "phone-conversation-list.png"))
                        context.close()
                    assert not errors, errors
                    browser.close()
                    print("PASS: two groups + 14 contacts; touch-scroll to last agent and open DM at phone, landscape and 1920/2560 desktop widths; app fills viewport; no overflow or browser errors")
            finally:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()


if __name__ == "__main__":
    main()
