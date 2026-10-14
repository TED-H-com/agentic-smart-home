import hashlib
import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from ui_helpers import click_navigation


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
    processed, request_keys = {}, []
    drop_reply = {'next': False}
    calls = []
    errors = []

    def route(route):
        path = route.request.url.split("/api/")[1]
        body = route.request.post_data_json if route.request.method == "POST" else None
        if body is not None:
            calls.append((path, body))
            assert path == "host/login" or route.request.headers.get("x-agentic-csrf") == "fixture-csrf"
        if path == "bootstrap":
            result = {"contract": 1, "id": None, "state": "idle", "provider": None,
                      "channel": "line", "current_step": None, "steps": [], "human_action": None, "updated_at": None}
        elif path == "deployment":
            result = {"mode": "host", "authenticated": True}
        elif path == "session":
            result = {"principal": "host-owner", "csrf": "fixture-csrf"}
        elif path == "status":
            result = {
                "ha": {"status": "unavailable"},
                "runner": {"protocol": 1, "providers": []},
                "gateway": gateway,
                "version": "0.5.1",
            }
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
                key = route.request.headers['idempotency-key']
                request_keys.append(key)
                if key in processed:
                    route.fulfill(status=200, content_type='application/json', body=json.dumps(processed[key]))
                    return
                messages.insert(
                    0,
                    {
                        "id": hashlib.sha256(str(len(messages)).encode()).hexdigest(),
                        "thread_id": next(m['thread_id'] for m in messages if m['id'] == body['reply_to']) if messages and body.get('reply_to') else hashlib.sha256(str(len(messages)).encode()).hexdigest(),
                        "source": "web",
                        **body,
                        "state": "succeeded",
                        "answer": "外層 Linux 仍可使用（測試回覆）",
                        "created_at": "2026-10-11T12:00:00Z",
                    },
                )
                result = messages[0]
                processed[key] = result
                if drop_reply['next']:
                    drop_reply['next'] = False
                    route.abort('failed')
                    return
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
            page.screenshot(path=str(root / f"evidence/ui/native-home/{name}-{width}.png"), full_page=True)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.route("**/api/**", route)
            page.goto(f"http://127.0.0.1:{server.server_port}/#onboarding")
            page.get_by_text("其他設定方式", exact=True).click()
            page.get_by_role("heading", name="你的家庭管理入口已經就緒").wait_for()
            screenshots(page, "onboarding-web")
            page.get_by_role("button", name="下一步", exact=True).click()
            page.get_by_text("進階：AI 帳號", exact=True).click()
            page.get_by_role("heading", name="主機的 AI 帳號", exact=True).wait_for()
            grok = page.locator("article.provider").filter(
                has=page.get_by_role("heading", name="Grok Build", exact=True)
            )
            grok.get_by_role("button", name="開始登入").click()
            grok.get_by_text("ABCD-EFGH", exact=True).wait_for()
            assert any(path == "runtimes/host/providers/grok/auth" for path, _ in calls)
            page.get_by_role("button", name="下一步", exact=True).click()
            page.get_by_role("heading", name="選擇你的通訊入口").wait_for()
            assert page.get_by_role("radio", name="LINE 建議", exact=True).is_checked()
            page.get_by_role("button", name="開始 LINE 掃碼登入").click()
            page.get_by_alt_text("用 LINE 掃描這個 QR").wait_for()
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
            page.get_by_text("進階：手動設定", exact=True).click()
            page.get_by_label("允許的使用者 ID").fill("u" + "2" * 32)
            page.get_by_role("button", name="儲存並啟用通訊").click()
            page.get_by_text("設定已儲存，正在連接通訊服務。", exact=True).wait_for()
            assert gateway["channels"][0]["enabled"] and gateway["preferences"]["primary"] == "line"
            page.get_by_role("radio", name="Web 不需帳號", exact=True).check()
            page.get_by_role("button", name="使用 Web 作為主要入口").click()
            page.get_by_text("Web 已設為主要入口。", exact=True).wait_for()
            page.reload()
            page.get_by_text("其他設定方式", exact=True).click()
            page.get_by_role("heading", name="選擇你的通訊入口").wait_for()
            assert page.get_by_role("radio", name="Web 不需帳號", exact=True).is_checked()
            page.locator(".bootstrap-manual").get_by_role("button", name="稍後再設定", exact=True).click()
            page.get_by_label("對話訊息").fill("HA 還沒啟動，也能問你嗎？")
            page.get_by_role("button", name="送出訊息", exact=True).click()
            page.get_by_text("外層 Linux 仍可使用（測試回覆）", exact=True).wait_for()
            page.wait_for_function("document.getElementById('gateway-message')?.placeholder === '繼續說…'")
            first = messages[0]['id']
            page.reload()
            page.wait_for_function("document.getElementById('gateway-message')?.placeholder === '繼續說…'")
            page.get_by_label("對話訊息").fill("那要怎麼查看主機狀態？")
            page.get_by_role("button", name="送出訊息", exact=True).click()
            page.get_by_role("heading", name="那要怎麼查看主機狀態？", exact=True).wait_for()
            assert calls[-1] == ('gateway/messages', {'message': '那要怎麼查看主機狀態？', 'runtime': 'host', 'provider': 'codex', 'reply_to': first})
            saved = page.evaluate("sessionStorage.getItem('agentic-auth-conversations')")
            assert json.loads(saved) == {'host-codex': messages[0]['id']}
            page.get_by_text('進階', exact=True).click()
            page.get_by_label('執行位置', exact=True).select_option('haos')
            page.get_by_text('需要協助時，直接告訴管家。', exact=True).wait_for()
            page.get_by_label('執行位置', exact=True).select_option('host')
            page.wait_for_function("document.getElementById('gateway-message')?.placeholder === '繼續說…'")
            page.get_by_text('進階', exact=True).click()
            screenshots(page, "gateway")
            page.get_by_role('button', name='新對話', exact=True).click()
            page.get_by_text('需要協助時，直接告訴管家。', exact=True).wait_for()
            page.get_by_label('對話訊息').fill('另一個問題')
            page.get_by_role('button', name='送出訊息', exact=True).click()
            page.get_by_role('heading', name='另一個問題', exact=True).wait_for()
            assert calls[-1][1]['reply_to'] is None
            page.get_by_role('button', name='對話紀錄', exact=False).click()
            page.get_by_role('region', name='對話紀錄').get_by_role('button').filter(has_text='那要怎麼查看主機狀態？').click()
            assert page.get_by_label('對話訊息').evaluate('(element) => element === document.activeElement')
            assert json.loads(page.evaluate("sessionStorage.getItem('agentic-auth-conversations')"))['host-codex'] == messages[1]['id']
            drop_reply['next'] = True
            count = len(messages)
            page.get_by_label('對話訊息').fill('連線中斷後重試同一則需求')
            page.get_by_role('button', name='送出訊息', exact=True).click()
            page.get_by_role('alert').filter(has_text='尚未收到操作結果').wait_for()
            assert page.get_by_label('對話訊息').input_value() == '連線中斷後重試同一則需求'
            page.get_by_role('button', name='送出訊息', exact=True).click()
            page.wait_for_function("document.getElementById('gateway-message').value === ''")
            assert request_keys[-1] == request_keys[-2] and len(messages) == count + 1
            click_navigation(page, 'Agent 環境')
            page.get_by_role("button", name="家庭現場 已配對").click()
            grok = page.locator("article.provider").filter(
                has=page.get_by_role("heading", name="Grok Build", exact=True)
            )
            assert grok.get_by_role("button", name="開始登入").is_enabled()
            grok.get_by_role("button", name="開始登入").click()
            assert any(path == "runtimes/haos/providers/grok/auth" for path, _ in calls)
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_chat_composer_keys_typing_indicator_and_scroll_to_newest():
    """Enter, Shift+Enter, IME composition, the pending indicator, and scroll pinning."""
    root = Path(__file__).parents[1]
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(root / "controller/static"))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    messages, posts, held, errors = [], [], [], []
    gateway = {"preferences": {"runtime": "host", "provider": "codex", "coordinator": "hermes"}, "channels": []}

    def route(route):
        path = route.request.url.split("/api/")[1].split("?")[0]
        body = route.request.post_data_json if route.request.method == "POST" else None
        if path == "deployment":
            result = {"mode": "host", "authenticated": True}
        elif path == "session":
            result = {"principal": "host-owner", "csrf": "fixture-csrf", "mode": "host"}
        elif path == "status":
            result = {"ha": {"status": "connected"}, "runner": {"protocol": 1, "providers": []}, "version": "fixture", "paused": False}
        elif path == "tasks":
            result = {"tasks": []}
        elif path == "gateway":
            result = gateway
        elif path == "gateway/messages" and body is None:
            result = {"messages": messages}
        elif path == "gateway/messages":
            assert route.request.headers.get("x-agentic-csrf") == "fixture-csrf"
            key = route.request.headers.get("idempotency-key")
            assert key
            posts.append({"body": body, "key": key})
            result = {
                **body,
                "id": hashlib.sha256(body["message"].encode()).hexdigest(),
                "thread_id": "b" * 32,
                "source": "web",
                "state": "succeeded",
                "answer": "已收到。\n" + ("這是較長的回覆，用來確認會捲到最新。\n" * 40),
                "created_at": "2026-10-12T12:00:00Z",
            }
            held.append((route, result))
            return
        else:
            result = {}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(result))

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.route("**/api/**", route)
            page.goto(f"http://127.0.0.1:{server.server_port}/#gateway")
            box = page.get_by_label("對話訊息", exact=True)
            box.wait_for()
            page.wait_for_function("document.getElementById('gateway-message') === document.activeElement")
            assert box.get_attribute("placeholder") == "輸入訊息…"
            box.fill("   ")
            box.press("Enter")
            page.wait_for_timeout(150)
            assert posts == [] and box.input_value() == "   "
            box.fill("第一行")
            box.press("Shift+Enter")
            box.type("第二行")
            assert box.input_value() == "第一行\n第二行" and posts == []
            cancelled = page.evaluate(
                """() => {
                  const el = document.getElementById('gateway-message');
                  const composing = new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true, isComposing: true});
                  el.dispatchEvent(composing);
                  const code = new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true});
                  Object.defineProperty(code, 'keyCode', {get: () => 229});
                  el.dispatchEvent(code);
                  return composing.defaultPrevented || code.defaultPrevented;
                }"""
            )
            page.wait_for_timeout(150)
            assert cancelled is False and posts == [] and box.input_value() == "第一行\n第二行"
            box.fill("請回覆這一段")
            box.press("Enter")
            page.get_by_role("status", name="智能管家正在回覆").wait_for()
            assert len(posts) == 1
            assert posts[0]["body"] == {
                "message": "請回覆這一段", "runtime": "host", "provider": "codex", "reply_to": None, "coordinator": "hermes",
            }
            route, result = held.pop()
            messages.insert(0, result)
            route.fulfill(status=200, content_type="application/json", body=json.dumps(result))
            page.get_by_text("已收到。", exact=False).wait_for()
            page.wait_for_function("document.getElementById('gateway-message').value === ''")
            page.wait_for_function("document.getElementById('gateway-message') === document.activeElement")
            page.wait_for_function(
                """() => {
                  const el = document.querySelector('.assistant-conversation');
                  const last = el && el.querySelector('.assistant-message:last-child');
                  if (!el || !last || el.scrollHeight <= el.clientHeight + 40) return false;
                  const near = el.scrollHeight - el.scrollTop - el.clientHeight < 8;
                  const box = last.getBoundingClientRect();
                  const view = el.getBoundingClientRect();
                  return near && box.bottom <= view.bottom + 4;
                }"""
            )
            assert page.get_by_role("status", name="智能管家正在回覆").count() == 0
            page.locator(".assistant-conversation").evaluate("el => { el.scrollTop = 0 }")
            messages.insert(0, {
                **result, "id": "c" * 64, "message": "後來的補充", "answer": "這是更新的訊息。",
                "created_at": "2026-10-12T12:05:00Z",
            })
            page.wait_for_function(
                "document.querySelector('.assistant-conversation').textContent.includes('這是更新的訊息。')",
                timeout=8000,
            )
            assert page.evaluate("document.querySelector('.assistant-conversation').scrollTop") < 80
            page.get_by_role("button", name="最新訊息 ↓", exact=True).click()
            page.wait_for_function(
                """() => {
                  const el = document.querySelector('.assistant-conversation');
                  return el.scrollHeight - el.scrollTop - el.clientHeight < 8;
                }"""
            )
            assert len(posts) == 1 and not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_interrupted_reply_can_be_sent_again():
    """An interrupted reply refills the composer on the Web page and in the sidebar. needs_reconcile has no button."""
    root = Path(__file__).parents[1]
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(root / "controller/static"))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    question = "請把走廊的燈關掉"
    reconcile_question = "請看一下玄關的感應器"
    reconcile_answer = "正在確認上一個回覆已停止，完成後可以重新送出。"
    interrupted_id, reconcile_id = "a" * 64, "b" * 64
    state = {"mode": "host", "focus": "interrupted"}
    posts, errors = [], []
    messages = {
        "interrupted": {
            "id": interrupted_id, "thread_id": interrupted_id, "source": "web", "runtime": "host",
            "provider": "codex", "coordinator": "hermes", "state": "interrupted", "error_code": None,
            "reason": "runtime_restarted", "message": question, "answer": "處理已中斷，請重新送出。",
            "created_at": "2026-10-13T08:00:00Z",
        },
        "reconcile": {
            "id": reconcile_id, "thread_id": reconcile_id, "source": "web", "runtime": "host",
            "provider": "codex", "coordinator": "hermes", "state": "needs_reconcile",
            "error_code": "stop_unconfirmed", "reason": "stop_unconfirmed", "message": reconcile_question,
            "answer": reconcile_answer, "created_at": "2026-10-13T08:05:00Z",
        },
        "ended": {
            "id": "c" * 64, "thread_id": "c" * 64, "source": "web", "runtime": "host",
            "provider": "codex", "coordinator": "hermes", "state": "failed", "error_code": None,
            "message": "請關掉客廳燈", "answer": "AI 已暫停或對話權限已變更，請檢查設定後開始新對話。",
            "created_at": "2026-10-13T08:10:00Z",
        },
        "busy": {
            "id": "d" * 64, "thread_id": "d" * 64, "source": "web", "runtime": "host",
            "provider": "codex", "coordinator": "hermes", "state": "failed", "error_code": "session_busy",
            "reason": "session_busy", "message": "請開客廳燈", "answer": "智能管家正在處理另一則訊息，請等它完成後再送出。",
            "created_at": "2026-10-13T08:15:00Z",
        },
    }
    gateway = {
        "step": 0, "selected": True, "web": {"status": "connected"},
        "preferences": {"primary": "web", "runtime": "host", "provider": "codex", "coordinator": "hermes"},
        "channels": [],
    }

    def route(route):
        path = route.request.url.split("/api/", 1)[1].split("?", 1)[0]
        body = route.request.post_data_json if route.request.method == "POST" else None
        if body is not None:
            posts.append(path)
        if path == "deployment":
            result = {"mode": state["mode"], "authenticated": True, "bridge_state": "ready"}
        elif path == "session":
            message = messages[state["focus"]]
            result = {
                "principal": "host-owner", "csrf": "fixture-csrf", "mode": state["mode"],
                "storage_scope": "1" * 64,
                "resume": {"message_id": message["id"], "thread_id": message["thread_id"], "case_id": None},
            }
        elif path == "status":
            result = {
                "ha": {"status": "connected"}, "runner": {"protocol": 1, "providers": []},
                "version": "fixture", "paused": False, "gateway": gateway,
            }
        elif path == "tasks":
            result = {"tasks": []}
        elif path == "gateway":
            result = gateway
        elif path == "gateway/messages" and body is None:
            result = {"messages": [messages[state["focus"]]]}
        elif path == "gateway/messages":
            result = {}
        else:
            raise AssertionError("gateway UI requested an unanswered path: " + path)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(result))

    def show(page, mode, focus, width):
        state["mode"] = mode
        state["focus"] = focus
        page.set_viewport_size({"width": width, "height": 900 if width == 1440 else 844})
        page.evaluate("sessionStorage.clear()")
        # The same hash does not navigate again; reload so the fixture's new reply is read.
        page.reload()

    def shot(page, name, width):
        page.wait_for_timeout(100)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(root / f"evidence/ui/{name}-{width}.png"), full_page=True)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/api/**", route)
            page.goto(f"http://127.0.0.1:{server.server_port}/#gateway")
            for width in (1440, 390):
                show(page, "host", "interrupted", width)
                button = page.get_by_role("button", name="重新送出", exact=True)
                expect(button).to_be_visible()
                expect(page.get_by_role("heading", name=question, exact=True)).to_be_visible()
                assert button.bounding_box()["height"] >= 44
                # One sentence, one action: the chip is translated and no second notice competes with the button.
                expect(page.locator(".status.s-interrupted")).to_have_text("已中斷")
                expect(page.get_by_text("這段對話暫時無法接續", exact=False)).to_have_count(0)
                shot(page, "gateway-resend", width)
                before = len(posts)
                button.click()
                expect(page.get_by_label("對話訊息", exact=True)).to_have_value(question)
                page.wait_for_timeout(400)
                assert len(posts) == before
                show(page, "host", "reconcile", width)
                expect(page.get_by_text(reconcile_answer, exact=True)).to_be_visible()
                expect(page.get_by_role("button", name="重新送出", exact=True)).to_have_count(0)
                expect(page.get_by_role("button", name="重試", exact=True)).to_have_count(0)
                # The reply already says to wait; no second notice repeats it.
                expect(page.get_by_text("這段對話暫時無法接續", exact=False)).to_have_count(0)
                shot(page, "gateway-reconcile", width)
                show(page, "bridge", "interrupted", width)
                expect(page.get_by_role("link", name="回到家庭", exact=True)).to_be_visible()
                expect(page.locator(".sidebar")).to_have_count(0)
                expect(page.get_by_text("處理已中斷，請重新送出。", exact=True)).to_be_visible()
                expect(page.get_by_text("這段對話暫時無法接續", exact=False)).to_have_count(0)
                button = page.get_by_role("button", name="重新送出", exact=True)
                expect(button).to_be_visible()
                shot(page, "bridge/gateway-resend", width)
                before = len(posts)
                button.click()
                expect(page.get_by_label("對話訊息", exact=True)).to_have_value(question)
                page.wait_for_timeout(400)
                assert len(posts) == before
                show(page, "bridge", "reconcile", width)
                expect(page.get_by_text(reconcile_answer, exact=True)).to_be_visible()
                expect(page.get_by_role("button", name="重新送出", exact=True)).to_have_count(0)
                shot(page, "bridge/gateway-reconcile", width)
            # A finished failure ends the conversation like a history item: one sentence, one action.
            show(page, "host", "ended", 1440)
            expect(page.get_by_text(messages["ended"]["answer"], exact=True)).to_be_visible()
            expect(page.get_by_text("這段對話已結束。", exact=True)).to_be_visible()
            expect(page.get_by_text("這段對話暫時無法接續", exact=False)).to_have_count(0)
            expect(page.get_by_label("對話訊息", exact=True)).to_have_count(0)
            page.get_by_role("button", name="開始新對話", exact=True).click()
            expect(page.get_by_label("對話訊息", exact=True)).to_have_attribute("placeholder", "輸入訊息…")
            expect(page.get_by_text("這段對話已結束。", exact=True)).to_have_count(0)
            # A busy assistant is worth one more try once it finishes.
            show(page, "host", "busy", 1440)
            expect(page.get_by_role("button", name="重試", exact=True)).to_be_visible()
            expect(page.get_by_text("這段對話已結束。", exact=True)).to_have_count(0)
            expect(page.get_by_text("這段對話暫時無法接續", exact=False)).to_have_count(0)
            # Starting over and retrying only prepare the composer; nothing is sent.
            assert "gateway/messages" not in posts
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_sidebar_reauth_sentence_and_plain_reply():
    """Bridge reauth is one sentence and no button. A direct reply names 開發助手. Fixture, not a real login."""
    root = Path(__file__).parents[1]
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(root / "controller/static"))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    hermes_id, direct_id, proposal_id, task_id = "a" * 64, "b" * 64, "c" * 64, "d" * 32
    state = {"mode": "host", "focus": "hermes"}
    errors = []
    messages = {
        "hermes": {
            "id": hermes_id, "thread_id": hermes_id, "source": "web", "runtime": "host", "provider": "codex",
            "coordinator": "hermes", "state": "failed", "error_code": "needs_reauth", "reason": "needs_reauth",
            "message": "請看一下走廊的燈", "answer": "這個執行環境的登入已失效。", "created_at": "2026-10-13T08:00:00Z",
        },
        "direct": {
            "id": direct_id, "thread_id": direct_id, "source": "web", "runtime": "host", "provider": "codex",
            "coordinator": "direct", "state": "failed", "error_code": "needs_reauth", "reason": "needs_reauth",
            "message": "請直接看玄關", "answer": "登入已失效。", "created_at": "2026-10-13T08:05:00Z",
        },
        "proposal": {
            "id": proposal_id, "thread_id": proposal_id, "source": "web", "runtime": "host", "provider": "codex",
            "coordinator": "direct", "state": "awaiting_approval", "message": "請看這次的家庭提案",
            "answer": "提案已準備好。", "task_id": task_id, "delivery": "unconfirmed",
            "created_at": "2026-10-13T08:10:00Z",
        },
    }
    gateway = {
        "step": 0, "selected": True, "web": {"status": "connected"},
        "preferences": {"primary": "web", "runtime": "host", "provider": "codex", "coordinator": "hermes"},
        "channels": [],
    }

    def route(route):
        path = route.request.url.split("/api/", 1)[1].split("?", 1)[0]
        if path == "deployment":
            result = {"mode": state["mode"], "authenticated": True, "bridge_state": "ready"}
        elif path == "session":
            message = messages[state["focus"]]
            result = {
                "principal": "host-owner", "csrf": "fixture-csrf", "mode": state["mode"],
                "storage_scope": "1" * 64,
                "resume": {"message_id": message["id"], "thread_id": message["thread_id"], "case_id": None},
            }
        elif path == "status":
            result = {
                "ha": {"status": "connected"}, "runner": {"protocol": 1, "providers": []},
                "version": "fixture", "paused": False, "gateway": gateway,
            }
        elif path == "tasks":
            result = {"tasks": []}
        elif path == "gateway":
            result = gateway
        elif path == "gateway/messages":
            result = {"messages": list(messages.values())}
        else:
            raise AssertionError("gateway UI requested an unanswered path: " + path)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(result))

    def show(page, mode, focus, coordinator):
        state["mode"] = mode
        state["focus"] = focus
        gateway["preferences"]["coordinator"] = coordinator
        page.evaluate("sessionStorage.clear()")
        if mode == "host":
            key = "hermes-host-codex" if coordinator == "hermes" else "host-codex"
            page.evaluate(
                "(item) => sessionStorage.setItem(item.storage, JSON.stringify({[item.key]: item.id}))",
                {"storage": "agentic-auth-conversations-" + ("1" * 64), "key": key, "id": messages[focus]["id"]},
            )
        page.reload()

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/api/**", route)
            page.goto(f"http://127.0.0.1:{server.server_port}/#gateway")
            show(page, "host", "hermes", "hermes")
            link = page.get_by_role("link", name="重新登入智能管家", exact=True)
            expect(link).to_be_visible()
            expect(link).to_have_attribute("href", "#agents")
            expect(page.get_by_text("請到主機重新登入智能管家。", exact=True)).to_have_count(0)
            expect(page.get_by_role("button", name="重試", exact=True)).to_have_count(0)
            speaker = page.locator(".assistant-speaker")
            expect(speaker).to_have_text("智能管家")
            assert speaker.get_attribute("title") is None
            assert "needs_reauth" not in (page.locator("body").inner_text() or "")

            show(page, "host", "direct", "direct")
            account = page.get_by_role("link", name="重新登入 AI 帳號", exact=True)
            expect(account).to_be_visible()
            expect(account).to_have_attribute("href", "#agents")
            expect(page.get_by_text("請到主機重新登入 AI 帳號。", exact=True)).to_have_count(0)
            expect(page.locator(".assistant-speaker")).to_have_text("開發助手")
            expect(page.locator(".assistant-speaker")).to_have_attribute("title", "主機 · Codex")

            show(page, "host", "proposal", "direct")
            proposal = page.get_by_role("link", name="查看提案 →", exact=True)
            expect(proposal).to_have_attribute("href", f"#home/{task_id}")
            expect(page.get_by_text("可能還沒送到聊天室。", exact=True)).to_be_visible()
            expect(page.locator(".assistant-answer .status")).to_have_count(0)
            expect(page.get_by_text("等待批准", exact=True)).to_have_count(0)
            expect(page.locator(".assistant-speaker")).to_have_text("開發助手")
            expect(page.locator(".assistant-speaker")).to_have_attribute("title", "主機 · Codex")

            show(page, "bridge", "hermes", "hermes")
            expect(page.locator(".bridge-shell")).to_be_visible()
            expect(page.get_by_text("請到主機重新登入智能管家。", exact=True)).to_be_visible()
            expect(page.locator(".assistant-answer").get_by_role("link")).to_have_count(0)
            expect(page.locator(".assistant-answer").get_by_role("button")).to_have_count(0)
            expect(page.get_by_role("link", name="重新登入智能管家", exact=True)).to_have_count(0)
            expect(page.get_by_role("link", name="重新登入 AI 帳號", exact=True)).to_have_count(0)
            expect(page.locator(".assistant-speaker")).to_have_text("智能管家")

            page.get_by_role("button", name="對話紀錄", exact=False).click()
            page.get_by_role("region", name="對話紀錄").get_by_role("button").filter(has_text="請直接看玄關").click()
            expect(page.get_by_text("請到主機重新登入 AI 帳號。", exact=True)).to_be_visible()
            expect(page.locator(".assistant-answer").get_by_role("link")).to_have_count(0)
            expect(page.locator(".assistant-answer").get_by_role("button")).to_have_count(0)
            expect(page.locator(".assistant-speaker")).to_have_text("開發助手")
            expect(page.locator(".assistant-speaker")).to_have_attribute("title", "主機 · Codex")

            page.get_by_role("button", name="對話紀錄", exact=False).click()
            page.get_by_role("region", name="對話紀錄").get_by_role("button").filter(has_text="請看這次的家庭提案").click()
            expect(page.get_by_role("link", name="查看提案 →", exact=True)).to_have_attribute("href", f"#home/{task_id}")
            expect(page.get_by_text("可能還沒送到聊天室。", exact=True)).to_be_visible()
            expect(page.locator(".assistant-answer .status")).to_have_count(0)
            expect(page.locator(".assistant-speaker")).to_have_text("開發助手")
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
