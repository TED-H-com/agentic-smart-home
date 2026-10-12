"""Browser behavior against protocol fixtures; no cloud/login/HAOS acceptance claims."""
import hashlib
import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


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
        elif path == "tasks":
            result = {"tasks": []}
        elif path == "gateway":
            result = gateway
        elif path == "gateway/onboarding":
            gateway["step"] = body["step"]
            result = body
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
            page.screenshot(path=str(root / f"evidence/ui/hermes-{name}-{width}.png"), full_page=True)

    def stored(page):
        return page.evaluate("JSON.stringify({session: {...sessionStorage}, local: {...localStorage}})")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/api/**", route)
            page.goto(f"http://127.0.0.1:{server.server_port}/#onboarding")
            page.get_by_text("進階：手動設定與其他登入方式", exact=True).click()
            panel = page.get_by_role("region", name="Hermes 協調服務")
            panel.get_by_role("heading", name="登入你的協調助手", exact=True).wait_for()
            expect(panel.locator(".section-heading").get_by_role("status")).to_have_text("尚待登入")
            expect(panel.get_by_role("button", name="儲存 Hermes 登入")).to_be_disabled()
            screenshots(page, "needs-auth")

            page.get_by_label("OpenRouter 模型 ID", exact=True).fill("fixture/model")
            key = page.get_by_label("OpenRouter API key", exact=True)
            assert key.get_attribute("type") == "password" and key.get_attribute("autocomplete") == "off"
            key.fill(secret)
            assert secret not in stored(page)
            key.press("Enter")
            expect(panel.locator(".section-heading").get_by_role("status")).to_have_text("已儲存，待實際回覆驗證")
            expect(key).to_have_value("")
            assert panel.get_by_text("已完成推論驗證", exact=True).count() == 0
            assert secret not in stored(page) and secret not in page.content()
            assert all(secret not in url for url in urls)
            assert [path for path, _ in calls] == ["hermes/auth"]  # storing credentials must not start a turn
            screenshots(page, "configured")
            page.reload()
            page.get_by_text("進階：手動設定與其他登入方式", exact=True).click()
            expect(panel.locator(".section-heading").get_by_role("status")).to_have_text("已儲存，待實際回覆驗證")
            expect(page.get_by_label("OpenRouter API key", exact=True)).to_have_value("")
            assert secret not in stored(page)

            # The authoritative backend, not the save operation, supplies verified/reauth states.
            runtime["state"] = "ready"
            panel.get_by_role("button", name="重新檢查", exact=True).click()
            expect(panel.locator(".section-heading").get_by_role("status")).to_have_text("已完成推論驗證")
            runtime["state"] = "needs_reauth"
            panel.get_by_role("button", name="重新檢查", exact=True).click()
            expect(panel.locator(".section-heading").get_by_role("status")).to_have_text("需要重新登入")
            runtime["state"] = "unavailable"
            panel.get_by_role("button", name="重新檢查", exact=True).click()
            expect(panel.locator(".section-heading").get_by_role("status")).to_have_text("服務尚未連線")
            runtime["state"] = "configured"

            page.get_by_role("button", name="保留進度，先開啟 Web 對話", exact=True).click()
            mode = page.get_by_label("對話方式", exact=True)
            expect(mode).to_have_value("hermes")
            assert page.get_by_label("Agent", exact=True).count() == 0
            assert page.get_by_label("執行環境", exact=True).count() == 0
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
            page.get_by_role("heading", name="接續這段對話", exact=True).wait_for()
            assert json.loads(page.evaluate("sessionStorage.getItem('agentic-auth-conversations')")) == {
                "hermes-host-codex": second["id"]}
            assert secret not in stored(page) and question not in stored(page)
            screenshots(page, "gateway")

            mode.select_option("direct")
            page.get_by_role("heading", name="向 Agent 提問", exact=True).wait_for()
            expect(page.get_by_label("Agent", exact=True)).to_have_value("codex")
            page.get_by_label("Agent", exact=True).select_option("grok")
            expect(page.get_by_label("Agent", exact=True)).to_have_value("grok")
            page.get_by_label("執行環境", exact=True).select_option("haos")
            page.get_by_label("對話訊息", exact=True).fill("受控 Grok 的獨立需求")
            page.get_by_role("button", name="送出訊息", exact=True).click()
            page.get_by_role("heading", name="受控 Grok 的獨立需求", exact=True).wait_for()
            direct = messages[0]
            assert calls[-1][1] == {"message": "受控 Grok 的獨立需求", "runtime": "haos", "provider": "grok", "reply_to": None}
            assert direct["case_id"] != first["case_id"] and direct["coordinator"] == "direct"

            mode.select_option("hermes")
            page.get_by_role("heading", name="接續這段對話", exact=True).wait_for()
            page.get_by_label("對話訊息", exact=True).fill("回到原本的 Hermes case")
            page.get_by_role("button", name="送出訊息", exact=True).click()
            page.get_by_role("heading", name="回到原本的 Hermes case", exact=True).wait_for()
            assert calls[-1][1]["reply_to"] == second["id"]
            assert calls[-1][1]["runtime"] == "host" and calls[-1][1]["provider"] == "codex"
            assert messages[0]["case_id"] == first["case_id"]
            page.get_by_role("button", name="開始新對話", exact=True).click()
            page.get_by_role("heading", name="向 Agent 提問", exact=True).wait_for()
            page.get_by_label("對話訊息", exact=True).fill("另一個 Hermes case")
            page.get_by_role("button", name="送出訊息", exact=True).click()
            page.get_by_role("heading", name="另一個 Hermes case", exact=True).wait_for()
            assert calls[-1][1]["reply_to"] is None and messages[0]["case_id"] != first["case_id"]

            page.get_by_role("button", name="Agent 環境", exact=False).click()
            expect(panel.locator(".section-heading").get_by_role("status")).to_have_text("已儲存，待實際回覆驗證")
            panel.get_by_role("button", name="登出 Hermes", exact=True).click()
            expect(panel.locator(".section-heading").get_by_role("status")).to_have_text("尚待登入")
            assert secret not in stored(page) and not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
