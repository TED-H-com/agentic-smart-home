"""Browser behavior against protocol fixtures; no cloud/login/HAOS acceptance claims."""
import hashlib
import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from ui_helpers import click_navigation


def test_hermes_login_truthful_states_and_gateway_case_continuity():
    root = Path(__file__).parents[1]
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(root / "controller/static")))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    gateway = {"step": 1, "web": {"status": "connected"}, "selected": True,
               "preferences": {"primary": "web", "runtime": "host", "provider": "codex", "coordinator": "hermes"},
               "channels": [{"id": channel, "status": "disabled", "enabled": False,
                             "chats": [], "senders": [], "has_secret": False}
                            for channel in ("line", "discord", "telegram")]}
    runtime = {"state": "needs_auth", "provider": "openrouter", "model": "", "version": "fixture"}
    messages, calls, errors, urls = [], [], [], []
    secret = "fixture-only-secret-for-browser-test"

    def route(route):
        request = route.request
        path = request.url.split("/api/")[1]
        urls.append(request.url)
        body = request.post_data_json if request.method == "POST" else None
        if body is not None:
            assert request.headers.get("x-agentic-csrf") == "fixture-csrf"
            assert request.headers.get("idempotency-key")
            calls.append((path, body))
        if path == "bootstrap":
            result = {"contract": 1, "id": None, "state": "idle", "provider": None,
                      "channel": "line", "current_step": None, "steps": [], "human_action": None, "updated_at": None}
        elif path == "deployment":
            result = {"mode": "host", "authenticated": True}
        elif path == "session":
            result = {"principal": "host-owner", "csrf": "fixture-csrf"}
        elif path == "status":
            result = {"ha": {"status": "unavailable"}, "runner": {"protocol": 1, "providers": []},
                      "gateway": gateway, "version": "0.5.1", "paused": False}
        elif path == "services":
            result = {"services": []}
        elif path == "retention":
            result = {"audit_days": 30, "context_days": 7}
        elif path == "tasks":
            result = {"tasks": []}
        elif path == "gateway":
            result = gateway
        elif path == "gateway/onboarding":
            gateway["step"] = body["step"]
            result = body
        elif path == "workers":
            result = {"workers": [{"worker_id": "host-coder", "available": False, "providers": []}]}
        elif path == "runtimes":
            result = {name: {"protocol": 1, "providers": []} for name in ("host", "haos")}
        elif path == "hermes":
            result = {"runtime": runtime, "workers": [{"worker_id": "host-coder", "available": False},
                                                      {"worker_id": "home-coder", "available": False}]}
        elif path == "hermes/auth":
            assert body == {"provider": "openrouter", "model": "fixture/model", "api_key": secret}
            runtime.update(state="configured", model=body["model"])
            result = {"state": "configured"}
        elif path == "hermes/auth/logout":
            runtime.update(state="needs_auth", model="")
            result = {"state": "needs_auth"}
        elif path == "gateway/messages":
            if body is None:
                result = {"messages": messages}
            else:
                coordinator = body.get("coordinator", "direct")
                parent = next((m for m in messages if m["id"] == body.get("reply_to")), None)
                if body.get("reply_to"):
                    assert parent is not None
                    assert (parent["coordinator"], parent["runtime"], parent["provider"]) == (
                        coordinator, body["runtime"], body["provider"])
                id = hashlib.sha256(str(len(messages)).encode()).hexdigest()
                result = {"id": id, "thread_id": parent["thread_id"] if parent else id,
                          "case_id": parent["case_id"] if parent else "fixture-case-" + id[:12],
                          "source": "web", **body, "coordinator": coordinator, "state": "succeeded",
                          "answer": "已取得主機觀測（UI 協定 fixture，非家庭實測）。",
                          "created_at": "2026-10-11T21:00:00Z"}
                messages.insert(0, result)
        else:
            raise AssertionError("Unexpected API route: " + path)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(result))

    def screenshots(page, name):
        for width in (1440, 768, 390):
            page.set_viewport_size({"width": width, "height": 900 if width == 1440 else 844})
            page.wait_for_timeout(100)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(root / f"evidence/ui/native-home/hermes-{name}-{width}.png"), full_page=True)

    def stored(page):
        return page.evaluate("JSON.stringify({session: {...sessionStorage}, local: {...localStorage}})")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/api/**", route)
            page.goto(f"http://127.0.0.1:{server.server_port}/#onboarding")
            page.get_by_text("其他設定方式", exact=True).click()
            panel = page.get_by_role("region", name="智能管家登入")
            panel.get_by_role("heading", name="智能管家登入", exact=True).wait_for()
            expect(panel.locator(".section-heading").get_by_role("status")).to_have_text("尚未登入")
            expect(panel.get_by_role("button", name="儲存登入")).to_be_disabled()
            screenshots(page, "needs-auth")

            page.get_by_role("textbox", name="模型", exact=True).fill("fixture/model")
            key = page.get_by_role("textbox", name="金鑰", exact=True)
            assert key.get_attribute("type") == "password" and key.get_attribute("autocomplete") == "off"
            key.fill(secret)
            assert secret not in stored(page)
            key.press("Enter")
            expect(panel.locator(".section-heading").get_by_role("status")).to_have_text("已儲存，尚未驗證")
            expect(panel.locator(".setup-facts span")).to_have_text("已登入 · OpenRouter")
            expect(panel.locator(".setup-facts small")).to_have_count(0)
            expect(panel.locator("details").filter(has=page.locator("summary", has_text="技術資料"))).to_contain_text("模型 fixture/model")
            expect(panel.locator(".hermes-login-form")).to_have_count(0)
            assert panel.get_by_text("已完成推論驗證", exact=True).count() == 0
            assert panel.get_by_text("已可對話", exact=True).count() == 0
            assert secret not in stored(page) and secret not in page.content()
            assert all(secret not in url for url in urls)
            assert [path for path, _ in calls] == ["hermes/auth"]  # storing credentials must not start a turn
            screenshots(page, "configured")
            page.reload()
            page.get_by_text("其他設定方式", exact=True).click()
            expect(panel.locator(".section-heading").get_by_role("status")).to_have_text("已儲存，尚未驗證")
            expect(panel.locator(".hermes-login-form")).to_have_count(0)
            expect(panel.locator(".setup-facts span")).to_have_text("已登入 · OpenRouter")
            expect(panel.locator("details").filter(has=page.locator("summary", has_text="技術資料"))).to_contain_text("模型 fixture/model")
            assert secret not in stored(page)

            # The authoritative backend, not the save operation, supplies verified/reauth states.
            # The panel reads hermes again every 10s; the old bottom 重新檢查 button is gone.
            expect(panel.get_by_role("button", name="重新檢查", exact=True)).to_have_count(0)
            status = panel.locator(".section-heading").get_by_role("status")
            runtime["state"] = "ready"
            expect(status).to_have_text("已可對話", timeout=15000)
            runtime["state"] = "needs_reauth"
            expect(status).to_have_text("需要重新登入", timeout=15000)
            runtime["state"] = "unavailable"
            expect(status).to_have_text("暫時無法使用", timeout=15000)
            runtime["state"] = "configured"

            page.locator(".bootstrap-manual").get_by_role("button", name="稍後再設定", exact=True).click()
            mode = page.get_by_label("對話方式", exact=True)
            expect(mode).to_have_value("hermes")
            assert page.get_by_label("AI", exact=True).count() == 0
            assert page.get_by_label("執行位置", exact=True).count() == 0
            question = "請讀取主機狀態（UI fixture）"
            page.get_by_label("對話訊息", exact=True).fill(question)
            page.get_by_role("button", name="送出訊息", exact=True).click()
            page.get_by_role("heading", name=question, exact=True).wait_for()
            first = messages[0]
            assert calls[-1][1]["coordinator"] == "hermes" and calls[-1][1]["reply_to"] is None
            page.get_by_label("對話訊息", exact=True).fill("接續說明還缺少什麼觀測")
            page.get_by_role("button", name="送出訊息", exact=True).click()
            page.get_by_role("heading", name="接續說明還缺少什麼觀測", exact=True).wait_for()
            second = messages[0]
            assert calls[-1][1]["reply_to"] == first["id"]
            assert second["case_id"] == first["case_id"] and second["thread_id"] == first["thread_id"]
            page.reload()
            page.wait_for_function("document.getElementById('gateway-message')?.placeholder === '繼續說…'")
            assert json.loads(page.evaluate("sessionStorage.getItem('agentic-auth-conversations')")) == {
                "hermes-host-codex": second["id"]}
            assert secret not in stored(page) and question not in stored(page)
            screenshots(page, "gateway")

            page.get_by_text("進階", exact=True).click()
            mode.select_option("direct")
            page.get_by_text("需要協助時，直接告訴管家。", exact=True).wait_for()
            expect(page.get_by_label("AI", exact=True)).to_have_value("codex")
            page.get_by_label("AI", exact=True).select_option("grok")
            expect(page.get_by_label("AI", exact=True)).to_have_value("grok")
            page.get_by_label("執行位置", exact=True).select_option("haos")
            page.get_by_label("對話訊息", exact=True).fill("受控 Grok 的獨立需求")
            page.get_by_role("button", name="送出訊息", exact=True).click()
            page.get_by_role("heading", name="受控 Grok 的獨立需求", exact=True).wait_for()
            direct = messages[0]
            assert calls[-1][1] == {"message": "受控 Grok 的獨立需求", "runtime": "haos", "provider": "grok", "reply_to": None}
            assert direct["case_id"] != first["case_id"] and direct["coordinator"] == "direct"

            mode.select_option("hermes")
            page.wait_for_function("document.getElementById('gateway-message')?.placeholder === '繼續說…'")
            page.get_by_label("對話訊息", exact=True).fill("回到原本的 Hermes case")
            page.get_by_role("button", name="送出訊息", exact=True).click()
            page.get_by_role("heading", name="回到原本的 Hermes case", exact=True).wait_for()
            assert calls[-1][1]["reply_to"] == second["id"]
            assert calls[-1][1]["runtime"] == "host" and calls[-1][1]["provider"] == "codex"
            assert messages[0]["case_id"] == first["case_id"]
            page.get_by_role("button", name="新對話", exact=True).click()
            page.get_by_text("需要協助時，直接告訴管家。", exact=True).wait_for()
            page.get_by_label("對話訊息", exact=True).fill("另一個 Hermes case")
            page.get_by_role("button", name="送出訊息", exact=True).click()
            page.get_by_role("heading", name="另一個 Hermes case", exact=True).wait_for()
            assert calls[-1][1]["reply_to"] is None and messages[0]["case_id"] != first["case_id"]

            click_navigation(page, 'Agent 環境')
            expect(panel.locator(".section-heading").get_by_role("status")).to_have_text("已儲存，尚未驗證")
            expect(panel.get_by_role("button", name="重新檢查", exact=True)).to_have_count(0)
            panel.get_by_role("button", name="登出", exact=True).click()
            expect(panel.locator(".section-heading").get_by_role("status")).to_have_text("尚未登入")
            assert secret not in stored(page) and not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_signed_in_hermes_summary_and_login_poll_stops_after_errors():
    root = Path(__file__).parents[1]
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(root / "controller/static")))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    runtime = {"state": "configured", "provider": "openai-codex", "model": "gpt-6-astra", "version": "fixture",
               "execution_state": "idle", "default_models": {"openai-codex": "gpt-6-astra"}}
    pending = {"auth_id": "pending-auth", "state": "pending", "provider": "openai-codex", "model": "gpt-6-astra",
               "verification_uri": "https://auth.openai.com/codex/device", "user_code": "POLL-CODE",
               "error": None, "reason": None}
    device_gets, errors = [], []
    phase = {"poll": False}

    def route(route):
        request = route.request
        path = request.url.split("/api/")[1].split("?")[0]
        if request.method == "POST":
            if path != "hermes/auth" or not phase.get("replace"):
                raise AssertionError("Unexpected API write: " + path)
            sent = request.post_data_json
            assert sent["provider"] == "openrouter" and sent["model"] == "vendor/model-a"
            runtime.update(provider="openrouter", model=sent["model"])
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"state": "configured"}))
            return
        if path == "deployment":
            result = {"mode": "host", "authenticated": True}
        elif path == "session":
            result = {"principal": "host-owner", "csrf": "fixture-csrf", "mode": "host"}
        elif path == "status":
            result = {"ha": {"status": "connected"}, "runner": {"protocol": 1, "providers": []},
                      "version": "fixture", "paused": False}
        elif path == "tasks":
            result = {"tasks": []}
        elif path == "runtimes":
            result = {"host": {"protocol": 1, "providers": []}, "haos": {"status": "unpaired"}}
        elif path == "hermes":
            shown = dict(runtime)
            if phase["poll"]:
                shown["pending_auth"] = pending
            result = {"runtime": shown, "workers": []}
        elif path.startswith("hermes/auth/device/"):
            device_gets.append(path)
            if len(device_gets) <= 3:
                route.fulfill(status=503, content_type="application/json",
                              body=json.dumps({"detail": "暫時無法回應"}))
                return
            result = {**pending, "state": "configured", "user_code": None, "verification_uri": None}
        else:
            raise AssertionError("Unexpected API route: " + path)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(result))

    def shot(page, name):
        evidence = root / "evidence/ui/native-home"
        for width, height in ((1440, 900), (390, 844)):
            page.set_viewport_size({"width": width, "height": height})
            page.wait_for_timeout(80)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), name + " " + str(width)
            page.screenshot(path=str(evidence / f"hermes-{name}-{width}.png"), full_page=True)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/api/**", route)
            page.goto(f"http://127.0.0.1:{server.server_port}/#agents")
            panel = page.get_by_role("region", name="智能管家登入")
            expect(panel.locator(".section-heading").get_by_role("status")).to_have_text("已儲存，尚未驗證")
            expect(panel.get_by_text("已可對話", exact=True)).to_have_count(0)
            expect(panel.get_by_role("button", name="重新檢查", exact=True)).to_have_count(0)
            expect(panel.locator(".setup-facts span")).to_have_text("已登入 · ChatGPT 訂閱")
            expect(panel.locator(".setup-facts small")).to_have_count(0)
            facts = panel.locator("details").filter(has=page.locator("summary", has_text="技術資料"))
            expect(facts).to_contain_text("模型 gpt-6-astra")
            expect(panel.locator(".hermes-login-form")).to_have_count(0)
            expect(panel.get_by_role("button", name="開始登入", exact=True)).to_have_count(0)
            shot(page, "signed-in")
            panel.get_by_role("button", name="更換登入方式", exact=True).click()
            expect(panel.get_by_text("更換後會先登出目前的帳號。", exact=True)).to_be_visible()
            expect(panel.locator(".hermes-login-form")).to_be_visible()
            expect(panel.get_by_role("radio", name="ChatGPT 訂閱", exact=True)).to_be_checked()
            shot(page, "replace-login")
            # A finished replacement folds back into the signed-in line.
            panel.get_by_role("radio", name="OpenRouter 金鑰", exact=True).check()
            panel.get_by_label("模型", exact=True).fill("vendor/model-a")
            panel.locator("input[type=password]").fill("fixture-key-not-a-secret")
            phase["replace"] = True
            panel.get_by_role("button", name="儲存登入", exact=True).click()
            expect(panel.locator(".setup-facts span")).to_have_text("已登入 · OpenRouter")
            expect(facts).to_contain_text("模型 vendor/model-a")
            expect(panel.locator(".hermes-login-form")).to_have_count(0)
            expect(panel.get_by_text("更換後會先登出目前的帳號。", exact=True)).to_have_count(0)
            phase["replace"] = False
            runtime.update(provider="openai-codex", model="gpt-6-astra")

            runtime["state"] = "needs_auth"
            phase["poll"] = True
            page.reload()
            expect(panel.get_by_text("POLL-CODE", exact=True)).to_be_visible()
            expect(panel.get_by_text("暫時無法讀取登入狀態。", exact=True)).to_be_visible(timeout=12000)
            shot(page, "poll-down")
            stalled = len(device_gets)
            assert stalled == 3
            page.wait_for_timeout(2500)
            assert len(device_gets) == stalled
            panel.get_by_role("button", name="重新檢查", exact=True).click()
            expect(panel.get_by_text("暫時無法讀取登入狀態。", exact=True)).to_have_count(0)
            assert len(device_gets) == 4
            shot(page, "poll-recheck")
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
