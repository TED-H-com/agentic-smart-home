"""Browser walk of the isolated interactive preview. Demo fixtures, not a household."""
import http.client
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import expect, sync_playwright
from ui_helpers import click_navigation

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from agentic_host.coding_sandbox import read_web_bundle, store_web_bundle  # noqa: E402
from agentic_host.preview import Previews, preview_server  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
JOB = "ab" * 16
PAGES = (
    ("智能管家", "示範家庭的玄關感應器可以在天黑後打開客廳燈嗎？"),
    ("設備", "客廳燈"),
    ("自動化", "天黑後打開客廳燈"),
    ("紀錄", "天黑後打開客廳燈"),
    ("設定", "AI 助手"),
    ("AI 帳號", "智能管家登入"),
    ("通訊", "選擇你的通訊入口"),
    ("開發助手", "把示範家庭的客廳燈自動化說明寫清楚"),
    ("主機維護", "示範主機"),
)


def test_interactive_preview_renders_demo_household(tmp_path):
    static = ROOT / "controller" / "static"
    if not (static / "index.html").is_file():
        pytest.skip("Build the web UI before the browser test")
    allowed = {".html", ".js", ".css", ".svg", ".json", ".txt", ".png", ".webp", ".woff2", ".ico"}
    artifacts = tmp_path / "artifacts"
    for path in static.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(static).as_posix()
        if relative.startswith("downloads/") or path.suffix not in allowed:
            continue
        target = artifacts / "web-preview" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    (artifacts / "web-fixtures.json").write_bytes((ROOT / "web" / "preview" / "fixtures.json").read_bytes())
    store_web_bundle(tmp_path / "web-previews" / JOB, *read_web_bundle(artifacts))
    service = Previews(SimpleNamespace(root=tmp_path, run=None), bundles=tmp_path / "web-previews")
    server = preview_server(service, ("127.0.0.2", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    errors, missed = [], []
    evidence = ROOT / "evidence" / "ui"
    evidence.mkdir(parents=True, exist_ok=True)
    try:
        view = service.submit({"id": "interactive-demo", "mode": "interactive", "job_id": JOB})
        # A ready household on an empty hash follows the product's HA handoff.
        # The preview stays on this origin only when the owner opens a hash.
        url = view["interactive_url"] + "#entry"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on("pageerror", lambda error: errors.append(str(error)))

            def observe(response):
                if "/api/" in response.url and response.request.method == "GET" and response.status >= 400:
                    missed.append((response.status, response.url))

            page.on("response", observe)
            page.goto(url)
            expect(page.get_by_role("heading", name="家庭已就緒")).to_be_visible()
            expect(page.get_by_text("示範家庭")).to_have_count(0)
            page.screenshot(path=str(evidence / "interactive-preview-entry.png"), full_page=True)
            for name, text in PAGES:
                click_navigation(page, name)
                expect(page.get_by_text(text).locator("visible=true").first).to_be_visible()
                if name == "設備":
                    expect(page.get_by_text("示範家庭").locator("visible=true").first).to_be_visible()
            click_navigation(page, "家庭提案")
            page.locator("#request").fill("請讓示範家庭的客廳燈在天黑後打開。")
            expect(page.get_by_role("button", name="送出需求")).to_be_enabled()
            page.get_by_role("button", name="送出需求").click()
            expect(page.get_by_text("預覽不會執行操作")).to_be_visible()
            with page.expect_console_message(lambda message: "Content Security Policy" in message.text):
                blocked = page.evaluate("""async () => {
                    try { await fetch('http://example.invalid/probe'); return 'ok'; }
                    catch (error) { return String(error); }
                }""")
            assert blocked != "ok"
            click_navigation(page, "初次設定")
            expect(page.get_by_role("heading", name="設定完成")).to_be_visible()
            assert not errors, errors
            assert not missed, missed
            browser.close()
        cap = service.records["interactive-demo"]["capability"]

        def raw(path, host=None, method="GET", extra=None):
            connection = http.client.HTTPConnection("127.0.0.2", server.server_port, timeout=5)
            headers = {"Host": host or f"127.0.0.2:{server.server_port}"}
            if extra:
                headers.update(extra)
            connection.request(method, path, body=b"{}" if method == "POST" else None, headers=headers)
            response = connection.getresponse()
            value = response.status, response.read()
            connection.close()
            return value

        assert raw(f"/i/{cap}/", host="localhost:8099")[0] == 404
        assert raw(f"/i/{cap}/", extra={"Service-Worker": "script"})[0] == 404
        service.records["interactive-demo"]["expires_at"] = time.time() - 1
        assert raw(f"/i/{cap}/")[0] == 404
        service.records["interactive-demo"]["expires_at"] = time.time() + 30
        assert raw(f"/i/{cap}/")[0] == 200
        assert service.revoke("interactive-demo")["state"] == "expired"
        assert raw(f"/i/{cap}/")[0] == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
