"""Account card login states with protocol fixtures. No real CLI or account."""
import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

DEVICE = {"id": "deviceauth", "provider": "codex", "status": "pending", "url": "https://auth.openai.com/codex/device",
          "code": "WXYZ-98765", "expires_in": 800}


def test_account_card_resumes_cancels_and_verifies_keys_in_background():
    root = Path(__file__).parents[1]
    if not (root / "controller/static/index.html").exists():
        pytest.skip("Build the web UI before the browser test")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(root / "controller/static")))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    calls, errors = [], []
    device = {"pending": True}
    secret = {"status": "verifying"}

    def route(route):
        request = route.request
        path = request.url.split("/api/")[1]
        if request.method == "POST":
            assert request.headers["x-agentic-csrf"] == "fixture-csrf"
            calls.append((path, request.post_data_json))
        if path == "deployment":
            result = {"mode": "host", "authenticated": True}
        elif path == "session":
            result = {"principal": "owner", "csrf": "fixture-csrf"}
        elif path == "status":
            result = {"version": "0.9.0", "paused": False, "ha": {"status": "connected", "location_name": "介面測試家庭"},
                      "runner": {"protocol": 1, "providers": []}, "onboarding": {"policy_confirmed": True},
                      "policy_entities": 0}
        elif path == "host/status":
            result = {"vm": "running", "provisioned": True, "ha_connected": True, "snapshots": [], "jobs": [],
                      "storage_free_bytes": 120 * 1024**3, "profile": {"label": "介面 fixture", "memory_mib": 4096, "vcpus": 2}}
        elif path == "hermes":
            result = {"runtime": {"state": "needs_auth", "execution_state": "idle"}, "workers": []}
        elif path == "tasks":
            result = {"tasks": []}
        elif path == "runtimes":
            codex = {"provider": "codex", "status": "needs_reauth"}
            if device["pending"]:
                codex["pending_session"] = DEVICE
            claude = {"provider": "claude", "status": "needs_reauth"}
            if secret["status"] == "failed":
                claude.update(reason="invalid_credentials", message="登入資訊無效，請重新輸入。")
            result = {"host": {"protocol": 1, "providers": [codex, claude]}, "haos": {"status": "unpaired"}}
        elif path == "runtimes/host/auth/deviceauth":
            result = DEVICE if device["pending"] else {**DEVICE, "status": "cancelled", "url": None, "code": None, "expires_in": 0}
        elif path == "runtimes/host/providers/codex/auth/deviceauth/cancel":
            device["pending"] = False
            result = {**DEVICE, "status": "cancelled", "url": None, "code": None, "expires_in": 0}
        elif path == "runtimes/host/providers/claude/auth":
            secret["status"] = "verifying"
            result = {"id": "secretauth", "provider": "claude", "status": "verifying", "url": None, "code": None,
                      "expires_in": 120}
        elif path == "runtimes/host/auth/secretauth":
            result = {"id": "secretauth", "provider": "claude", "status": secret["status"], "url": None, "code": None,
                      "expires_in": 100 if secret["status"] == "verifying" else 0}
            if secret["status"] == "failed":
                result.update(reason="invalid_credentials", message="登入資訊無效，請重新輸入。")
        elif path == "runtimes/host/providers/claude/auth/secretauth/cancel":
            secret["status"] = "unverified"
            result = {"id": "secretauth", "provider": "claude", "status": "unverified", "url": None, "code": None,
                      "expires_in": 0}
        else:
            result = {}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(result))

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/api/**", route)
            page.goto(f"http://127.0.0.1:{server.server_port}/#agents")
            panel = page.get_by_role("region", name="開發助手帳號", exact=True)
            codex = panel.locator("article").filter(has=page.get_by_role("heading", name="Codex", exact=True))
            claude = panel.locator("article").filter(has=page.get_by_role("heading", name="Claude Code", exact=True))

            # Another tab started this login; the card shows the same code without a second start.
            expect(codex.get_by_text("WXYZ-98765", exact=True)).to_be_visible()
            expect(codex.get_by_text("等待登入", exact=True)).to_be_visible()
            codex.get_by_role("button", name="取消登入", exact=True).click()
            expect(codex.get_by_role("button", name="開始登入", exact=True)).to_be_visible()
            expect(codex.get_by_text("WXYZ-98765", exact=True)).to_have_count(0)
            assert not any(path == "runtimes/host/providers/codex/auth" for path, _ in calls)

            expect(claude.get_by_text("使用 API 金鑰，可能另外計費。", exact=True)).to_be_visible()
            claude.get_by_label("API 金鑰", exact=True).fill("fixture-claude-key")
            claude.get_by_role("button", name="開始登入", exact=True).click()
            expect(claude.get_by_text("正在向官方確認登入資訊…", exact=True)).to_be_visible()
            expect(claude.get_by_text("確認中", exact=True)).to_be_visible()
            expect(claude.get_by_role("button", name="開始登入", exact=True)).to_be_hidden()
            claude.get_by_role("button", name="取消", exact=True).click()
            expect(claude.get_by_role("button", name="開始登入", exact=True)).to_be_visible()
            expect(claude.get_by_role("alert")).to_have_count(0)

            claude.get_by_label("API 金鑰", exact=True).fill("fixture-wrong-key")
            claude.get_by_role("button", name="開始登入", exact=True).click()
            expect(claude.get_by_text("正在向官方確認登入資訊…", exact=True)).to_be_visible()
            secret["status"] = "failed"
            expect(claude.get_by_role("alert")).to_have_text("登入資訊無效，請重新輸入。")
            expect(claude.get_by_role("button", name="重新開始", exact=True)).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert [path for path, _ in calls] == [
                "runtimes/host/providers/codex/auth/deviceauth/cancel",
                "runtimes/host/providers/claude/auth",
                "runtimes/host/providers/claude/auth/secretauth/cancel",
                "runtimes/host/providers/claude/auth",
            ]
            assert calls[1][1] == {"method": "api_key", "secret": "fixture-claude-key"}
            assert page.evaluate("sessionStorage.getItem('agentic-auth-host-claude')") is None
            browser.close()
        assert not errors
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_home_runtime_tab_leads_to_workers_and_folds_stable_manual_pair():
    root = Path(__file__).parents[1]
    if not (root / "controller/static/index.html").exists():
        pytest.skip("Build the web UI before the browser test")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(root / "controller/static")))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    calls, errors = [], []
    runtimes = {"host": {"status": "unpaired", "providers": []}, "haos": {"status": "unpaired", "providers": []}}

    def route(route):
        request = route.request
        path = request.url.split("/api/")[1]
        if request.method == "POST":
            assert request.headers["x-agentic-csrf"] == "fixture-csrf"
            calls.append((path, request.post_data_json))
        if path == "deployment":
            result = {"mode": "host", "authenticated": True}
        elif path == "session":
            result = {"principal": "owner", "csrf": "fixture-csrf"}
        elif path == "status":
            result = {"version": "0.9.0", "paused": False, "ha": {"status": "connected", "location_name": "介面測試家庭"},
                      "runner": {"status": "unpaired", "providers": []}, "onboarding": {"policy_confirmed": True},
                      "policy_entities": 0}
        elif path == "host/status":
            result = {"vm": "running", "provisioned": True, "ha_connected": True, "snapshots": [], "jobs": [],
                      "storage_free_bytes": 120 * 1024**3, "profile": {"label": "介面 fixture", "memory_mib": 4096, "vcpus": 2}}
        elif path == "hermes":
            result = {"runtime": {"state": "needs_auth", "execution_state": "idle"}, "workers": []}
        elif path == "tasks":
            result = {"tasks": []}
        elif path == "runtimes":
            result = runtimes
        elif path == "runtimes/haos/pair":
            result = {"paired": True}
        else:
            result = {}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(result))

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/api/**", route)
            page.goto(f"http://127.0.0.1:{server.server_port}/#agents")
            panel = page.get_by_role("region", name="開發助手帳號", exact=True)
            tabs = panel.get_by_role("group", name="開發助手位置", exact=True)
            tabs.get_by_role("button", name="家庭現場").click()

            expect(panel.get_by_text("在開發助手選「家庭現場」並啟用，主機會自動安裝和配對。", exact=True)).to_be_visible()
            expect(panel.get_by_role("button", name="開始登入")).to_have_count(0)
            workers_link = panel.get_by_role("link", name="前往開發助手", exact=True)
            expect(workers_link).to_be_visible()
            expect(workers_link).to_have_attribute("href", "#workers")

            repo_url = "https://github.com/TED-H-com/agentic-smart-home#stable"
            textarea = panel.get_by_label("配對資料", exact=True)
            expect(textarea).to_be_hidden()
            expect(panel.get_by_text(repo_url, exact=True)).to_be_hidden()
            expect(panel.locator('a[href*="downloads/agentic-runner.zip"]')).to_have_count(0)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

            panel.locator("summary").filter(has_text="手動安裝與配對").click()
            expect(panel.locator("code")).to_be_visible()
            expect(panel.locator("code")).to_have_text(repo_url)
            expect(panel.locator('a[href*="downloads/agentic-runner.zip"]')).to_have_count(0)
            pair_button = panel.get_by_role("button", name="配對家庭現場", exact=True)
            expect(pair_button).to_be_disabled()
            payload = {"fingerprint": "fixture-fingerprint", "code": "fixture-code"}
            textarea.fill(json.dumps(payload))
            expect(pair_button).to_be_enabled()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            pair_button.click()
            expect(textarea).to_have_value("")
            assert calls == [("runtimes/haos/pair", payload)]

            tabs.get_by_role("button", name="主機").click()
            expect(panel.get_by_text("先連接這台主機上的開發助手。", exact=True)).to_be_visible()
            expect(panel.get_by_role("button", name="連接主機", exact=True)).to_be_visible()
            expect(panel.get_by_role("button", name="開始登入")).to_have_count(0)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

            # Paired but not answering, for example while the host restarts: wait, do not ask to enable or pair again.
            runtimes.update(host={"status": "unavailable", "providers": []}, haos={"status": "unavailable", "providers": []})
            page.reload()
            panel = page.get_by_role("region", name="開發助手帳號", exact=True)
            tabs = panel.get_by_role("group", name="開發助手位置", exact=True)
            for name in ("主機", "家庭現場"):
                tabs.get_by_role("button", name=name).click()
                expect(panel.get_by_text("暫時連不上，會自動重試。", exact=True)).to_be_visible()
                expect(panel.locator(".section-heading").get_by_text("無法連線", exact=True)).to_be_visible()
                expect(tabs.get_by_text("無法連線", exact=True)).to_have_count(2)
                for text in ("先連接這台主機上的開發助手。", "在開發助手選「家庭現場」並啟用，主機會自動安裝和配對。", "手動安裝與配對"):
                    expect(panel.get_by_text(text, exact=True)).to_have_count(0)
                expect(panel.get_by_role("button", name="連接主機", exact=True)).to_have_count(0)
                expect(panel.get_by_role("button", name="開始登入")).to_have_count(0)
                expect(panel.get_by_role("link", name="前往開發助手", exact=True)).to_have_count(1 if name == "家庭現場" else 0)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert calls == [("runtimes/haos/pair", payload)]
            browser.close()
        assert not errors
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
