"""Host API browser fixtures; these are not physical appliance acceptance."""
import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def test_appliance_status_reports_address_warning_and_clears_stale_success():
    root = Path(__file__).parents[1]
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler,
                                                        directory=str(root / "controller/static")))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    mode = "observed"
    errors = []

    def route(request):
        path = request.request.url.split("/api/")[1]
        if path == "deployment":
            body = {"mode": "host", "authenticated": True}
        elif path == "session":
            body = {"principal": "host-owner", "csrf": "fixture-csrf", "mode": "host"}
        elif path == "status":
            body = {"version": "fixture", "ha": {"status": "connected"}, "runner": {"providers": []}}
        elif path == "tasks":
            body = {"tasks": []}
        elif path == "host/status":
            if mode == "disconnected":
                request.fulfill(status=503, content_type="application/json", body='{"detail":"暫時無法連線"}')
                return
            body = {"vm": "running", "provisioned": True, "jobs": [], "snapshots": [],
                    "profile": {"label": "測試筆電", "memory_mib": 4096, "vcpus": 2}}
            if mode != "old-host":
                body["appliance"] = {
                    "network": {"state": "observed", "address": "192.168.1.112",
                                "previous_address": "192.168.1.113", "address_changed": True,
                                "mdns_hostname": "castle-agentic-home.local", "mac": "02:00:00:00:00:12"},
                    "lid": {"state": "verified", "message": "蓋上蓋子也會繼續運作"},
                    "suspend": {"state": "observed", "warning": "最近曾進入睡眠，期間家庭自動化可能中斷。"}}
        else:
            body = {}
        request.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/api/**", route)
            page.goto(f"http://127.0.0.1:{server.server_port}/#host")
            panel = page.get_by_role("region", name="主機運作", exact=True)
            expect(panel.get_by_text("蓋上蓋子也會繼續運作", exact=True)).to_be_visible()
            expect(panel.get_by_text("castle-agentic-home.local", exact=True)).to_be_visible()
            expect(panel.get_by_text("主機位址已變更：192.168.1.113 → 192.168.1.112。", exact=True)).to_be_visible()
            expect(panel.get_by_text("最近曾進入睡眠", exact=False)).to_be_visible()
            panel.get_by_text("固定主機位址", exact=True).click()
            expect(panel.get_by_text("在路由器的「DHCP 位址保留」中，選這台主機並保留目前位址。", exact=True)).to_be_visible()
            for width in (1440, 768, 390):
                page.set_viewport_size({"width": width, "height": 900})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
            # A failed poll must clear the previous live verification and IP.
            mode = "disconnected"
            expect(panel.get_by_text("蓋上蓋子也會繼續運作", exact=True)).to_have_count(0, timeout=8000)
            expect(panel.get_by_text("castle-agentic-home.local", exact=True)).to_have_count(0)
            expect(panel.get_by_text("尚未確認合蓋後會繼續運作。", exact=True)).to_be_visible()
            mode = "old-host"
            page.reload()
            expect(panel.get_by_text("主機尚未提供運作檢查，請透過正式更新取得支援。", exact=True)).to_be_visible()
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
