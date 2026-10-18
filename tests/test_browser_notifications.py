"""Host notification settings against local fixtures; never connects to accounts."""
import copy
import json
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect, sync_playwright


@pytest.fixture
def notifications_browser(tmp_path, monkeypatch):
    # Chromium profile/temp files stay on the pytest-disktmp disk basetemp.
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    root = Path(__file__).parents[1]
    state = {
        "config": {"enabled": True, "revision": "a" * 64, "recipients": [
            {"alias": "family", "label": "家庭聊天室", "channel": "line", "chat": "fixture-family"}]},
        "integration": {"state": "not_installed", "source_id": None, "last_seen": None, "reason": None},
        "channels": [{"channel": "line", "transport": "connected", "login": "needs_login"}],
        "destinations": [{"channel": "line", "chat": "fixture-family", "label": "家庭聊天室"},
                         {"channel": "telegram", "chat": "fixture-telegram", "label": "家人群組"}],
        "deliveries": [
            {"id": "1" * 32, "recipient": "family", "channel": "line", "state": "submitted",
             "created_at": "2026-10-17T10:00:00Z", "updated_at": "2026-10-17T10:00:01Z", "reason": None},
            {"id": "2" * 32, "recipient": "family", "channel": "line", "state": "unknown",
             "created_at": "2026-10-17T10:01:00Z", "updated_at": "2026-10-17T10:01:01Z", "reason": "transport_unknown"},
        ],
    }
    gateway = {"step": 4, "web": {"status": "connected"}, "selected": True,
               "preferences": {"primary": "line", "runtime": "host", "provider": "codex", "coordinator": "hermes"},
               "channels": [{"id": name, "status": "connected" if name == "line" else "disabled",
                             "enabled": name == "line", "chats": ["fixture-family"] if name == "line" else [],
                             "senders": ["fixture-owner"] if name == "line" else [], "has_secret": False}
                            for name in ["line", "telegram", "discord"]]}
    fixtures = {
        "deployment": {"mode": "host", "authenticated": True},
        "session": {"principal": "host-owner", "csrf": "fixture-csrf"},
        "bootstrap": {"contract": 1, "id": None, "state": "idle", "provider": None,
                      "channel": "line", "current_step": None, "steps": [], "human_action": None, "updated_at": None},
        "status": {"ha": {"status": "connected"}, "runner": {"protocol": 1, "providers": []},
                   "gateway": gateway, "version": "0.10.0"},
        "services": {"services": []}, "retention": {"audit_days": 30, "context_days": 7},
        "tasks": {"tasks": []}, "workers": {"workers": []},
        "gateway": gateway, "gateway/messages": {"messages": []},
        "runtimes": {"host": {"protocol": 1, "providers": []}, "haos": {"protocol": 1, "providers": []}},
    }
    control = {"read_error": False, "conflict": False, "lose_response": False, "malformed": False}
    writes, unexpected, errors, notification_reads = [], [], [], []

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(root / "controller/static")))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"

    def route(request):
        url, method = request.request.url, request.request.method
        if not url.startswith(origin + "/"):
            unexpected.append((method, url))
            request.abort()
            return
        path = urlparse(url).path
        if not path.startswith("/api/"):
            request.continue_()
            return
        path = path[len("/api/"):]
        if path == "notifications/config" and method == "POST":
            body = request.request.post_data_json
            writes.append(copy.deepcopy(body))
            assert request.request.headers["x-agentic-csrf"] == "fixture-csrf"
            assert len(request.request.headers["idempotency-key"]) == 32
            if control["conflict"] or body["expected_revision"] != state["config"]["revision"]:
                request.fulfill(status=409, content_type="application/json", body='{"detail":"notification_revision_stale"}')
                return
            state["config"] = {"enabled": body["enabled"], "revision": "b" * 64, "recipients": body["recipients"]}
            if control["lose_response"]:
                request.abort("failed")
                return
            result = state
        elif path == "notifications" and method == "GET":
            notification_reads.append(path)
            if control["read_error"]:
                request.fulfill(status=503, content_type="application/json", body='{"detail":"notification_unavailable"}')
                return
            result = {} if control["malformed"] else state
        elif path in fixtures and method == "GET":
            result = fixtures[path]
        else:
            unexpected.append((method, path))
            request.fulfill(status=404, content_type="application/json", body='{"detail":"fixture_missing"}')
            return
        request.fulfill(status=200, content_type="application/json", body=json.dumps(result))

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                page.clock.install()
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.route("**/*", route)
                yield {"page": page, "panel": page.get_by_role("region", name="家庭通知"),
                       "state": state, "control": control, "writes": writes,
                       "fixtures": fixtures, "notification_reads": notification_reads, "origin": origin,
                       "open": lambda: page.goto(origin + "/#gateway/channels")}
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
    assert not thread.is_alive()
    assert not errors
    assert not unexpected


def test_notification_states_history_and_responsive_layout(notifications_browser):
    f = notifications_browser
    f["open"]()
    panel, page = f["panel"], f["page"]
    expect(panel.get_by_text("尚未安裝", exact=True)).to_be_visible()
    expect(panel.get_by_text("本機傳輸", exact=True)).to_be_visible()
    expect(panel.get_by_text("已連線", exact=True)).to_be_visible()
    expect(panel.get_by_text("LINE 登入", exact=True)).to_be_visible()
    expect(panel.get_by_text("需要登入", exact=True)).to_be_visible()
    panel.get_by_text("安裝與使用", exact=True).click()
    expect(panel.get_by_role("link", name="下載通知整合")).to_have_attribute("href", "downloads/agentic-home-notifications.zip")
    expect(panel.get_by_role("link", name="安裝說明 ↗")).to_have_attribute("href", "https://github.com/TED-H-com/agentic-smart-home/blob/main/docs/HA_NOTIFICATIONS.md")
    panel.get_by_text("安裝與使用", exact=True).click()
    panel.get_by_text("送出紀錄 · 2", exact=True).click()
    expect(panel.get_by_text("已交給通訊服務", exact=True)).to_be_visible()
    expect(panel.get_by_text("結果不明", exact=True)).to_be_visible()
    expect(panel.get_by_text("交給通訊服務不代表已送達或已讀；結果不明時不會自動重送。", exact=True)).to_be_visible()
    assert not panel.get_by_role("button", name="傳送", exact=True).count()
    assert not panel.get_by_role("button", name="重送", exact=True).count()
    for width, height in [(390, 844), (768, 844), (1440, 900)]:
        page.set_viewport_size({"width": width, "height": height})
        panel.evaluate("node => node.scrollIntoView({block: 'start'})")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        folder = os.environ.get("NOTIFICATION_UI_SCREENSHOTS")
        if folder:
            destination = Path(folder)
            destination.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(destination / f"notifications-{width}.png"), full_page=False)
    assert not f["writes"]


def test_notification_recipient_edits_only_allowed_destinations(notifications_browser):
    f = notifications_browser
    f["open"]()
    panel = f["panel"]
    panel.get_by_role("button", name="新增收件人", exact=True).click()
    panel.get_by_label("收件人代稱").fill("family")
    panel.get_by_label("顯示名稱").fill('<img src=x onerror="alert(1)">')
    panel.get_by_role("combobox", name="聊天室", exact=True).select_option(label="Telegram · 家人群組")
    assert panel.get_by_role("combobox", name="聊天室", exact=True).locator("option").count() == 3
    panel.get_by_role("button", name="加入清單", exact=True).click()
    expect(panel.get_by_text("這個收件人代稱已存在。", exact=True)).to_be_visible()
    panel.get_by_label("收件人代稱").fill("parents")
    panel.get_by_role("button", name="加入清單", exact=True).click()
    expect(panel.get_by_text('<img src=x onerror="alert(1)">', exact=True)).to_be_visible()
    assert panel.locator("img").count() == 0
    panel.get_by_role("button", name="修改收件人 family", exact=True).click()
    panel.get_by_label("顯示名稱").fill("家庭")
    panel.get_by_role("button", name="套用修改", exact=True).click()
    panel.get_by_role("button", name="移除收件人 parents", exact=True).click()
    group = panel.get_by_role("group", name="移除收件人 parents？儲存後生效。")
    group.get_by_role("button", name="取消", exact=True).click()
    assert not f["writes"]
    panel.get_by_role("button", name="儲存通知設定", exact=True).click()
    expect(panel.get_by_text("設定已儲存；通知結果請看送出紀錄。", exact=True)).to_be_visible()
    assert len(f["writes"]) == 1
    assert f["writes"][0]["expected_revision"] == "a" * 64
    assert f["writes"][0]["recipients"] == [
        {"alias": "family", "label": "家庭", "channel": "line", "chat": "fixture-family"},
        {"alias": "parents", "label": '<img src=x onerror="alert(1)">', "channel": "telegram", "chat": "fixture-telegram"}]
    expect(panel.get_by_text("尚未安裝", exact=True)).to_be_visible()


def test_notification_stale_save_requires_explicit_reload(notifications_browser):
    f = notifications_browser
    f["open"]()
    panel = f["panel"]
    panel.get_by_role("checkbox", name="啟用家庭通知").uncheck()
    f["control"]["conflict"] = True
    f["state"]["config"]["revision"] = "c" * 64
    panel.get_by_role("button", name="儲存通知設定", exact=True).click()
    expect(panel.get_by_role("alert")).to_contain_text("設定已在別處更新，請重新載入後修改。")
    expect(panel.get_by_role("checkbox", name="啟用家庭通知")).not_to_be_checked()
    expect(panel.get_by_role("button", name="儲存通知設定", exact=True)).to_be_disabled()
    panel.get_by_role("button", name="捨棄修改並重新載入", exact=True).click()
    expect(panel.get_by_role("checkbox", name="啟用家庭通知")).to_be_checked()
    assert len(f["writes"]) == 1


def test_notification_poll_keeps_dirty_editor_and_fences_changed_revision(notifications_browser):
    f = notifications_browser
    f["open"]()
    panel = f["panel"]
    panel.get_by_role("button", name="修改收件人 family", exact=True).click()
    panel.get_by_label("顯示名稱").fill("尚未套用的名稱")
    f["state"]["config"]["revision"] = "d" * 64
    f["state"]["config"]["recipients"][0]["label"] = "別處更新"
    f["page"].clock.fast_forward(5100)
    expect(panel.get_by_role("alert")).to_contain_text("設定已在別處更新，請重新載入後修改。")
    expect(panel.get_by_label("顯示名稱")).to_have_value("尚未套用的名稱")
    expect(panel.get_by_role("button", name="套用修改", exact=True)).to_be_disabled()
    assert not f["writes"]


def test_notification_lost_save_response_reads_back_without_retry(notifications_browser):
    f = notifications_browser
    f["open"]()
    panel = f["panel"]
    panel.get_by_role("checkbox", name="啟用家庭通知").uncheck()
    f["control"]["lose_response"] = True
    panel.get_by_role("button", name="儲存通知設定", exact=True).click()
    expect(panel.get_by_role("alert")).to_contain_text("尚未確認設定是否儲存，請重新載入確認。")
    expect(panel.get_by_role("button", name="儲存通知設定", exact=True)).to_be_disabled()
    panel.get_by_role("button", name="捨棄修改並重新載入", exact=True).click()
    expect(panel.get_by_text("已重新讀取通知設定。", exact=True)).to_be_visible()
    expect(panel.get_by_role("checkbox", name="啟用家庭通知")).not_to_be_checked()
    assert len(f["writes"]) == 1


def test_notification_read_failure_allows_disable_with_orphan_recipient(notifications_browser):
    f = notifications_browser
    f["state"]["destinations"] = []
    original = copy.deepcopy(f["state"]["config"]["recipients"])
    f["open"]()
    panel = f["panel"]
    expect(panel.get_by_text("尚未安裝", exact=True)).to_be_visible()
    f["control"]["read_error"] = True
    f["page"].clock.fast_forward(5100)
    expect(panel.get_by_text("讀取失敗", exact=True)).to_be_visible()
    expect(panel.get_by_text("已連線", exact=True)).not_to_be_visible()
    panel.get_by_role("checkbox", name="啟用家庭通知").uncheck()
    expect(panel.get_by_role("checkbox", name="啟用家庭通知")).to_be_disabled()
    panel.get_by_role("button", name="儲存通知設定", exact=True).click()
    expect(panel.get_by_text("設定已儲存；通知結果請看送出紀錄。", exact=True)).to_be_visible()
    assert f["writes"] == [{"enabled": False, "expected_revision": "a" * 64, "recipients": original}]


def test_notification_empty_initial_read_failure_and_recovery(notifications_browser):
    f = notifications_browser
    f["control"]["malformed"] = True
    f["open"]()
    panel = f["panel"]
    expect(panel.get_by_role("alert")).to_contain_text("通知設定格式不完整，請重新讀取。")
    expect(panel.get_by_role("checkbox", name="啟用家庭通知")).not_to_be_attached()
    f["control"]["malformed"] = False
    f["state"]["config"]["recipients"] = []
    f["state"]["deliveries"] = []
    panel.get_by_role("button", name="重新讀取", exact=True).click()
    expect(panel.get_by_text("還沒有收件人，請先加入已允許的聊天室。", exact=True)).to_be_visible()
    panel.get_by_text("送出紀錄 · 0", exact=True).click()
    expect(panel.get_by_text("還沒有通知紀錄。", exact=True)).to_be_visible()
    assert not f["writes"]


def test_notification_remove_confirmation_keeps_original_recipient_on_poll(notifications_browser):
    f = notifications_browser
    f["open"]()
    panel = f["panel"]
    panel.get_by_role("button", name="移除收件人 family", exact=True).click()
    f["state"]["config"]["revision"] = "e" * 64
    f["state"]["config"]["recipients"] = [
        {"alias": "other", "label": "另一個收件人", "channel": "line", "chat": "fixture-family"}]
    f["page"].clock.fast_forward(5100)
    expect(panel.get_by_role("alert")).to_contain_text("設定已在別處更新，請重新載入後修改。")
    confirm = panel.get_by_role("group", name="移除收件人 family？儲存後生效。")
    expect(confirm.get_by_role("button", name="移除", exact=True)).to_be_disabled()
    expect(panel.get_by_text("家庭聊天室", exact=True)).to_be_visible()
    expect(panel.get_by_text("另一個收件人", exact=True)).not_to_be_visible()
    assert not f["writes"]


@pytest.mark.parametrize("mode,route", [("host", "#gateway"), ("ingress", "#gateway/channels")])
def test_notification_panel_only_loads_in_host_channel_settings(notifications_browser, mode, route):
    f = notifications_browser
    f["fixtures"]["deployment"]["mode"] = mode
    f["page"].goto(f["origin"] + "/" + route)
    if mode == "host":
        expect(f["page"].get_by_role("button", name="對話紀錄", exact=True)).to_be_visible()
    else:
        # Legacy Ingress rejects this host-only route and opens its workbench.
        expect(f["page"].get_by_role("heading", name="工作台", exact=True)).to_be_visible()
        expect(f["page"].get_by_role("heading", name="你想讓家做什麼？", exact=True)).to_be_visible()
    expect(f["panel"]).not_to_be_attached()
    assert not f["notification_reads"]
    assert not f["writes"]
