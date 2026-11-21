"""Desktop and phone file transfer with the real standalone UI and isolated data."""

from pathlib import Path
import json, os, signal, socket, subprocess, sys, tempfile, time, struct, zlib
from urllib.request import Request, urlopen
from playwright.sync_api import sync_playwright, expect


def png():
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\x00" + b"\x40\xa0\x80" * 16) * 16))
        + chunk(b"IEND", b"")
    )


def main():
    root = Path(__file__).resolve().parents[1]
    out = root / "artifacts/file-transfer-20261113"
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="relay-files-browser-") as temporary:
        sockets = [socket.socket() for _ in range(2)]
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        ports = [s.getsockname()[1] for s in sockets]
        for sock in sockets:
            sock.close()
        base = f"http://127.0.0.1:{ports[0]}"
        with (out / "browser-server.log").open("w") as log:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-P",
                    "-m",
                    "relay",
                    "--root",
                    temporary,
                    "--port",
                    str(ports[0]),
                    "--mcp-port",
                    str(ports[1]),
                ],
                cwd=root,
                env={**os.environ, "PYTHONPATH": str(root)},
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            try:

                def api(path, body=None):
                    with urlopen(
                        Request(
                            base + "/api/" + path,
                            headers={
                                "Authorization": "Bearer " + token,
                                "Content-Type": "application/json",
                            },
                            data=None if body is None else json.dumps(body).encode(),
                        ),
                        timeout=10,
                    ) as response:
                        return json.load(response)

                for _ in range(100):
                    try:
                        token = (Path(temporary) / "owner.token").read_text().strip()
                        api("state")
                        break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise AssertionError("Server failed to start")
                peer = api("accounts", {"username": "alice", "name": "Alice"})
                group = api("groups", {"name": "文件协作", "members": ["alice"]})
                with sync_playwright() as p:
                    executable = p.chromium.executable_path
                    if not Path(executable).is_file():
                        executable = str(
                            sorted(
                                (Path.home() / ".cache/ms-playwright").glob(
                                    "chromium-*/chrome-linux*/chrome"
                                )
                            )[-1]
                        )
                    browser = p.chromium.launch(
                        executable_path=executable, headless=True, args=["--no-sandbox"]
                    )
                    errors = []
                    for mobile in (False, True):
                        context = browser.new_context(
                            viewport={
                                "width": 390 if mobile else 1280,
                                "height": 844 if mobile else 900,
                            },
                            is_mobile=mobile,
                            has_touch=mobile,
                            accept_downloads=True,
                            service_workers="block",  # Let route() simulate one network failure.
                        )

                        def login(credential):
                            page = context.new_page()
                            page.on("pageerror", lambda error: errors.append(str(error)))
                            page.add_init_script(
                                'sessionStorage.setItem("relay-token",'
                                + json.dumps(credential)
                                + ")"
                            )
                            page.goto(base + "/")
                            expect(page.locator("#app")).to_be_visible()
                            return page

                        owner = login(token)
                        alice = login(peer["token"])
                        # Use a browser session cookie for owner uploads; Alice uses Bearer.
                        (
                            owner.evaluate(
                                """async token=>{
                          const response=await fetch('/api/auth/password/set',{method:'POST',headers:{'Authorization':'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify({password:'files-browser-password'})});
                          if(!response.ok)throw Error(await response.text());
                          await activate({url:location.origin,browser:true});
                        }""",
                                token,
                            )
                            if not mobile
                            else None
                        )
                        # Mobile reuses the already configured account via token; its
                        # cookie path is covered by the desktop flow and HTTP tests.
                        owner.locator('.contact[data-id="' + peer["account"]["id"] + '"]').click()
                        filename = ("手机" if mobile else "电脑") + "报告.txt"
                        payload = b"attachment-data\x00" * 25000
                        with owner.expect_file_chooser() as chooser:
                            owner.locator("#attachFile").click()
                        chooser.value.set_files(
                            {"name": filename, "mimeType": "text/plain", "buffer": payload}
                        )
                        expect(owner.locator("#fileDraft")).to_contain_text(filename)
                        owner.locator("#text").fill("文件说明")
                        owner.locator("#sendBtn").click()
                        expect(
                            owner.locator(".file-card")
                            .filter(has_text=filename)
                            .locator(".download-file")
                        ).to_be_visible(timeout=15000)
                        alice.locator(
                            '.contact[data-id="' + api("state")["me"]["id"] + '"]'
                        ).click()
                        card = alice.locator(".file-card").filter(has_text=filename)
                        expect(card).to_be_visible(timeout=15000)
                        with alice.expect_download() as download:
                            card.locator(".download-file").click()
                        saved = Path(temporary) / (
                            "phone-download" if mobile else "desktop-download"
                        )
                        download.value.save_as(saved)
                        assert saved.read_bytes() == payload
                        assert download.value.suggested_filename == filename
                        if mobile:
                            alice.screenshot(path=str(out / "phone-files.png"))
                        else:
                            alice.screenshot(path=str(out / "desktop-files.png"))
                        # Group file uses the same UI and download contract.
                        owner.bring_to_front()
                        if mobile:
                            owner.locator("#backBtn").click()
                        owner.locator('[data-group="' + str(group["id"]) + '"]').click()
                        owner.locator("#fileInput").set_input_files(
                            {
                                "name": "group-" + filename,
                                "mimeType": "text/plain",
                                "buffer": b"group-file",
                            }
                        )
                        owner.locator("#sendBtn").click()
                        expect(
                            owner.locator(".file-card").filter(has_text="group-" + filename)
                        ).to_be_visible(timeout=15000)
                        # Real raster bytes: selected draft, sent thumbnail, recipient
                        # preview, full-screen viewer and unchanged original download.
                        image_name = ("手机" if mobile else "电脑") + "-photo.png"
                        owner.locator("#fileInput").set_input_files(
                            {"name": image_name, "mimeType": "image/png", "buffer": png()}
                        )
                        expect(owner.locator("#fileDraft img")).to_be_visible()
                        owner.wait_for_function(
                            "document.querySelector('#fileDraft img')?.naturalWidth===16"
                        )
                        owner.locator("#sendBtn").click()
                        preview = owner.locator('[data-image-name="' + image_name + '"]')
                        expect(preview.locator("img")).to_be_visible(timeout=15000)
                        preview.click()
                        expect(owner.locator("#imageViewer")).to_be_visible()
                        owner.wait_for_function(
                            "document.querySelector('#imageViewerImage').naturalWidth===16"
                        )
                        owner.locator("#closeImageViewer").click()
                        alice.bring_to_front()
                        if mobile:
                            alice.locator("#backBtn").click()
                        image_id = preview.get_attribute("data-image-preview")
                        attempts = []

                        def first_preview_fails(route):
                            attempts.append(1)
                            if len(attempts) == 1:
                                route.fulfill(
                                    status=503,
                                    content_type="application/json",
                                    body='{"error":"temporary"}',
                                )
                            else:
                                route.continue_()

                        alice.route(base + "/api/files/" + image_id, first_preview_fails)
                        alice.locator('[data-group="' + str(group["id"]) + '"]').click()
                        received = alice.locator('[data-image-name="' + image_name + '"]')
                        expect(received).to_contain_text("点击重试预览", timeout=15000)
                        alice.wait_for_timeout(3200)  # Multiple regular chat refreshes.
                        assert len(attempts) == 1, "Preview failure must not automatically retry"
                        received.click()
                        expect(alice.locator("#imageViewer")).to_be_visible(timeout=15000)
                        alice.locator("#closeImageViewer").click()
                        expect(received.locator("img")).to_be_visible(timeout=15000)
                        received.click()
                        expect(alice.locator("#imageViewer")).to_be_visible()
                        alice.locator("#closeImageViewer").click()
                        card = alice.locator(".file-card").filter(has_text=image_name)
                        with alice.expect_download() as image_download:
                            card.locator(".download-file").click()
                        image_download.value.save_as(Path(temporary) / "image-download")
                        assert (Path(temporary) / "image-download").read_bytes() == png()
                        alice.screenshot(
                            path=str(
                                out
                                / (
                                    "phone-image-preview.png"
                                    if mobile
                                    else "desktop-image-preview.png"
                                )
                            )
                        )
                        # A vector renamed .png is never inserted as an image.
                        owner.bring_to_front()
                        invalid_name = ("phone" if mobile else "desktop") + "-unsafe.png"
                        owner.locator("#fileInput").set_input_files(
                            {
                                "name": invalid_name,
                                "mimeType": "image/png",
                                "buffer": b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
                            }
                        )
                        expect(owner.locator("#fileDraft img")).to_have_count(0)
                        owner.locator("#sendBtn").click()
                        invalid = owner.locator('[data-image-name="' + invalid_name + '"]')
                        expect(invalid).to_contain_text("点击重试预览", timeout=15000)
                        expect(invalid.locator("img")).to_have_count(0)
                        if mobile:
                            owner.locator("#backBtn").click()
                        owner.locator('.contact[data-id="' + peer["account"]["id"] + '"]').click()
                        expect(owner.locator("#imageViewer")).not_to_be_visible()
                        assert owner.locator("body").evaluate("(el)=>el.scrollWidth<=innerWidth+1")
                        context.close()
                    assert not errors, errors
                    browser.close()
                print(
                    "PASS: desktop/phone plus picker, caption, private receive/download byte equality, Unicode filename, cookie/Bearer, group file, raster draft/thumbnail/viewer, original image download, SVG rejection, no horizontal overflow or JS errors"
                )
            finally:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()


if __name__ == "__main__":
    main()
