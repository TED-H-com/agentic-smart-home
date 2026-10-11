import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright


def test_outer_onboarding_line_setup_and_shared_inbox():
    root = Path(__file__).parents[1]
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(root / "controller/static"))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    gateway = {
        "step": 0,
        "web": {"status": "connected"},
        "selected": False,
        "preferences": {"primary": "line", "runtime": "host", "provider": "codex"},
        "channels": [
            {"id": c, "status": "disabled", "enabled": False, "chats": [], "senders": [], "has_secret": False}
            for c in ["line", "discord", "telegram"]
        ],
    }
    login = {"status": "idle", "channels": []}
    messages = []
    calls = []
    errors = []

    def route(route):
        path = route.request.url.split("/api/")[1]
        body = route.request.post_data_json if route.request.method == "POST" else None
        if body is not None:
            calls.append((path, body))
            assert path == "host/login" or route.request.headers.get("x-agentic-csrf") == "fixture-csrf"
        if path == "deployment":
            result = {"mode": "host", "authenticated": True}
        elif path == "session":
            result = {"principal": "host-owner", "csrf": "fixture-csrf"}
        elif path == "status":
            result = {
                "ha": {"status": "unavailable"},
                "runner": {"protocol": 1, "providers": []},
                "gateway": gateway,
                "version": "0.3.0",
            }
        elif path == "tasks":
            result = {"tasks": []}
        elif path == "gateway":
            result = gateway
        elif path == "gateway/onboarding":
            gateway["step"] = body["step"]
            result = body
        elif path == "gateway/preferences":
            gateway.update(preferences=body, selected=True)
            result = gateway
        elif path == "runtimes":
            result = {"host": {"protocol": 1, "providers": []}, "haos": {"protocol": 1, "providers": []}}
        elif path == "gateway/line/login":
            if body is not None:
                login.update(status="authenticating", qr="https://fixture.example/qr", pin="123456")
            result = login
        elif path.startswith("gateway/channels/"):
            channel = next(c for c in gateway["channels"] if c["id"] == path.rsplit("/", 1)[1])
            channel.update({k: v for k, v in body.items() if k != "secret"}, status="connecting")
            result = gateway
        elif path == "gateway/messages":
            if body:
                messages.insert(
                    0,
                    {
                        "id": "fixture-chat",
                        "source": "web",
                        **body,
                        "state": "succeeded",
                        "answer": "外層 Linux 仍可使用（測試回覆）",
                        "created_at": "2026-10-11T12:00:00Z",
                    },
                )
                result = messages[0]
            else:
                result = {"messages": messages}
        elif path.startswith("runtimes/") and path.endswith("/auth"):
            result = {
                "id": "fixtureauth",
                "status": "pending",
                "url": "https://accounts.x.ai/device",
                "code": "ABCD-EFGH",
            }
        elif "/auth/fixtureauth" in path:
            result = {
                "id": "fixtureauth",
                "status": "pending",
                "url": "https://accounts.x.ai/device",
                "code": "ABCD-EFGH",
            }
        else:
            result = {}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(result))

    def screenshots(page, name):
        for width in (1440, 768, 390):
            page.set_viewport_size({"width": width, "height": 900 if width == 1440 else 844})
            page.wait_for_timeout(100)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(root / f"evidence/ui/{name}-{width}.png"), full_page=True)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.route("**/api/**", route)
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            page.get_by_role("heading", name="你的家庭管理入口已經就緒").wait_for()
            screenshots(page, "onboarding-web")
            page.get_by_role("button", name="下一步", exact=True).click()
            page.get_by_role("heading", name="登入外層 Agent", exact=True).wait_for()
            grok = page.locator("article.provider").filter(
                has=page.get_by_role("heading", name="Grok Build", exact=True)
            )
            grok.get_by_role("button", name="開始官方登入").click()
            grok.get_by_text("ABCD-EFGH", exact=True).wait_for()
            assert any(path == "runtimes/host/providers/grok/auth" for path, _ in calls)
            page.get_by_role("button", name="下一步", exact=True).click()
            page.get_by_role("heading", name="選擇你的通訊入口").wait_for()
            assert page.get_by_role("button", name="LINE 預設推薦").get_attribute("aria-pressed") == "true"
            page.get_by_role("button", name="開始 LINE 掃碼登入").click()
            page.get_by_alt_text("用 LINE 掃描此登入 QR code").wait_for()
            page.get_by_text("123456", exact=True).wait_for()
            screenshots(page, "onboarding-line")
            login.update(
                status="ready",
                qr=None,
                pin=None,
                profile="測試 LINE",
                channels=[{"channelId": "c" + "1" * 32, "name": "家庭（測試）", "kind": "group"}],
            )
            page.get_by_label("加入家庭聊天室").wait_for()
            page.get_by_label("加入家庭聊天室").select_option("c" + "1" * 32)
            page.get_by_label("允許的使用者 ID").fill("u" + "2" * 32)
            page.get_by_role("button", name="儲存並啟用通訊").click()
            page.get_by_text("設定已儲存，正在連接通訊服務。", exact=True).wait_for()
            assert gateway["channels"][0]["enabled"] and gateway["preferences"]["primary"] == "line"
            page.get_by_role("button", name="Web 瀏覽器直接對話").click()
            page.get_by_role("button", name="使用 Web 作為主要入口").click()
            page.get_by_text("Web 已設為主要入口。", exact=True).wait_for()
            page.reload()
            page.get_by_role("heading", name="選擇你的通訊入口").wait_for()
            assert (
                page.get_by_role("button", name="Web 瀏覽器直接對話").get_attribute("aria-pressed") == "true"
            )
            page.get_by_role("button", name="保留進度，先開啟 Web 對話").click()
            page.get_by_label("對話訊息").fill("HA 還沒啟動，也能問你嗎？")
            page.get_by_role("button", name="送出訊息", exact=True).click()
            page.get_by_text("外層 Linux 仍可使用（測試回覆）", exact=True).wait_for()
            screenshots(page, "gateway")
            page.get_by_role("button", name="Agent 環境", exact=False).click()
            page.get_by_role("button", name="HAOS 內層 已配對").click()
            grok = page.locator("article.provider").filter(
                has=page.get_by_role("heading", name="Grok Build", exact=True)
            )
            assert grok.get_by_role("button", name="開始官方登入").is_enabled()
            grok.get_by_role("button", name="開始官方登入").click()
            assert any(path == "runtimes/haos/providers/grok/auth" for path, _ in calls)
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
