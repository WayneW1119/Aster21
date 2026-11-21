"""Independent Windows windows for the local Squad and Relay clients."""

import ctypes
import http.client
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import quote, urlencode, urlsplit

from . import _windows_locks, windows_desktop


def relay_login(port, token, selected):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        query = urlencode({"selected": selected})
        connection.request("GET", "/squad/api/relay-connections?" + query,
                           headers={"Authorization": "Bearer " + token})
        response = connection.getresponse()
        if response.status == 409:
            raise RuntimeError("请先在 Squad 中启动一个聊天域。")
        if response.status != 200:
            raise RuntimeError("无法连接 Relay，请检查 Windows Studio 的运行状态。")
        url = json.loads(response.read())["url"]
        destination = urlsplit(url)
        if (destination.scheme != "http" or destination.netloc != f"127.0.0.1:{port}"
                or destination.path != "/relay/" or destination.query
                or not destination.fragment.startswith("connections=")):
            raise RuntimeError("Relay 地址与当前工作台不匹配。")
        return url
    finally:
        connection.close()


def launch_window(root, page, selected=""):
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    subprocess.Popen([str(pythonw), "-X", "utf8", "-P", "-m", "squad.windows_desktop",
                      "window", "--root", str(root), "--page", page, "--selected=" + selected],
                     cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


class SquadWindowActions:
    def __init__(self, root, squad_url):
        self._root = root
        self._squad_url = squad_url
        self._window = None

    def open_relay(self, selected):
        # This bridge exposes only the local Relay launcher. Domain IDs, rather
        # than destinations or credentials, may be supplied by the page.
        if (not isinstance(selected, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{0,64}", selected)
                or not self._window
                or str(self._window.get_current_url() or "").split("#", 1)[0] != self._squad_url):
            return {"ok": False, "error": "无法从当前页面打开 Relay。"}
        try:
            launch_window(self._root, "relay", selected)
            return {"ok": True}
        except OSError:
            return {"ok": False, "error": "无法启动 Relay 桌面窗口。"}


def run_window(root, page="squad", selected=""):
    try:
        import webview
        from webview.menu import MenuAction
    except ImportError as error:
        raise RuntimeError("Install the Windows window extra: pip install -e '.[windows-window]'") from error

    # Two shortcuts can start together. Wait for the shared supervisor lock,
    # then release it before entering either GUI loop.
    with (root / "desktop.lock").open("a+b") as lock:
        _windows_locks.flock(lock, _windows_locks.LOCK_EX)
        state = windows_desktop.status(root)
        if state["status"] == "stopped":
            state = windows_desktop.start(root)
        if state["status"] != "running":
            raise RuntimeError("Windows Studio is not ready")

    address = urlsplit(state["url"])
    if (address.scheme != "http" or address.hostname != "127.0.0.1"
            or not address.port or address.username or address.password
            or address.path != "/squad/" or address.query or address.fragment):
        raise RuntimeError("The Windows Studio address must be local")
    squad_url = f"http://127.0.0.1:{address.port}/squad/"
    token = (root / "studio/control/owner.token").read_text(encoding="utf-8").strip()
    is_squad = page == "squad"
    title = "Aster Squad" if is_squad else "Relay"
    other = "relay" if is_squad else "squad"
    login = (squad_url + "#token=" + quote(token, safe="") if is_squad
             else relay_login(address.port, token, selected))
    actions = SquadWindowActions(root, squad_url) if is_squad else None
    icon = Path(__file__).with_name("assets") / f"windows-{page}.ico"

    # Give each window its own identity in the Windows taskbar.
    app_id = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
    app_id.argtypes = [ctypes.c_wchar_p]
    app_id("Aster.WindowsStudio." + page)
    window = webview.create_window(title, login, width=1280 if is_squad else 1080,
                                   height=820, min_size=(720, 500), js_api=actions)
    if actions:
        actions._window = window

    def open_other():
        try:
            launch_window(root, other)
        except OSError:
            window.evaluate_js("alert('无法打开另一个桌面窗口。')")

    renderer_missing = False

    def check_renderer(renderer):
        nonlocal renderer_missing
        renderer_missing = renderer != "edgechromium"
        return not renderer_missing

    window.events.initialized += check_renderer
    menu = [MenuAction("打开 Relay" if is_squad else "打开 Squad", open_other),
            MenuAction("刷新", lambda: window.evaluate_js("location.reload()"))]
    webview.start(gui="edgechromium", private_mode=True, menu=menu, icon=str(icon))
    if renderer_missing:
        raise RuntimeError("Aster Windows Studio requires Microsoft Edge WebView2 Runtime")
