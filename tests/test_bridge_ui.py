"""Browser/Ingress protocol fixtures; not a real HAOS App or cloud acceptance test."""
import base64
import hashlib
import json
import re
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).parents[1]
PREFIX = "/api/hassio_ingress/fixture-ingress/"
MID, NEXT, CASE, JOB, ART, PREVIEW = "a" * 64, "b" * 64, "c" * 32, "d" * 32, "e" * 32, "f" * 32
TASK, PID, DIGEST = "7" * 32, "8" * 32, "5" * 64
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jVZkAAAAASUVORK5CYII=")


@pytest.fixture
def bridge_browser():
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(ROOT / "controller/static"), **kwargs)

        def do_GET(self):
            if self.path.startswith(PREFIX):
                self.path = "/" + self.path[len(PREFIX):]
            super().do_GET()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    text = '<script>window.untrustedExecuted=true</script>\n<main>修正後的畫面</main>'
    digest = hashlib.sha256(text.encode()).hexdigest()
    state = {"bridge_state": "ready", "owner": "owner-a", "scope": "1" * 64, "offline": False,
             "revoked": False, "wrong_digest": False, "fail_send": False, "fail_approve": False,
             "approved": False, "preview_state": "ready", "content": text, "mode": "bridge", "expired": False, "expected_parent": MID, "reply_id": NEXT,
             "maintenance": {"contract": 1, "state": "not_authorized", "auto_repair": False}}
    messages = [{"id": MID, "thread_id": MID, "case_id": CASE, "source": "web", "coordinator": "hermes",
                 "runtime": "host", "provider": "codex", "state": "awaiting_approval", "task_id": TASK,
                 "message": "調整測試燈的規則", "answer": "已整理提案與修正成果，請先確認。", "created_at": "2026-10-12T12:00:00Z"}]
    calls, mutations, errors = [], [], []
    artifact = {"id": ART, "name": "index.html", "kind": "file", "media_type": "text/html",
                "size": len(text.encode()), "sha256": digest}
    proposal = {"id": PID, "mode": "action", "digest": DIGEST, "expires": 4102444800,
                "action": {"entity_id": "input_boolean.agentic_lamp_test", "action": "turn_on"}}

    def route(route):
        request = route.request
        path = urlsplit(request.url).path
        if not path.startswith(PREFIX + "api/"):
            route.continue_()
            return
        assert not urlsplit(request.url).query
        path = path[len(PREFIX + "api/"):]
        calls.append((request.method, path))
        body = request.post_data_json if request.method == "POST" else None
        if body is not None:
            assert request.headers.get("x-agentic-csrf") == "fixture-bridge-csrf"
            assert request.headers.get("idempotency-key")
            mutations.append((path, body, request.headers["idempotency-key"]))
        status = 200
        if path == "deployment":
            result = {"mode": state["mode"], "authenticated": state["bridge_state"] == "ready",
                      "bridge_state": state["bridge_state"]}
        elif state["offline"]:
            status, result = 503, {"detail": "fixture_unavailable"}
        elif state["revoked"]:
            status, result = 403, {"detail": "fixture_owner_revoked"}
        elif path == "session":
            result = {"csrf": "fixture-bridge-csrf", "mode": state["mode"], "principal": state["owner"],
                      "storage_scope": state["scope"], "resume": {"message_id": messages[0]["id"],
                      "thread_id": messages[0]["thread_id"], "case_id": messages[0]["case_id"]} if messages else None}
        elif state["mode"] == "host" and path in {"status", "tasks", "services", "retention", "workers"}:
            result = {"ha": {"status": "connected"}, "runner": {"protocol": 1}, "version": "0.7.0 fixture",
                      "tasks": [], "services": [], "audit_days": 30, "context_days": 7}
        elif state["mode"] == "host" and path.startswith("host/bridge/maintenance"):
            if body is not None:
                action = path.rsplit("/", 1)[1]
                if action == "authorize":
                    assert body == {"auto_repair": True}
                    state["maintenance"].update(state="healthy", auto_repair=True, managed=True)
                elif action == "policy":
                    state["maintenance"]["auto_repair"] = body["auto_repair"]
                elif action == "disable":
                    assert body == {}
                    state["maintenance"].update(state="disabled", auto_repair=False)
                elif action == "update":
                    assert body == {}
                    held = state.get("hold_update")
                    if held is not None:
                        held.append((route, request.headers["idempotency-key"], body))
                        return
                else:
                    assert action == "repair" and body == {}
            result = state["maintenance"]
        elif state["mode"] == "host" and path == "gateway":
            result = {"preferences": {"runtime": "host", "provider": "codex", "coordinator": "hermes"},
                      "channels": []}
        elif path == "gateway/messages":
            if body is None:
                result = {"messages": messages}
            else:
                assert body["coordinator"] == "hermes" and body["runtime"] == "host" and body["provider"] == "codex"
                assert body["reply_to"] == state["expected_parent"]
                result = {**messages[-1], "id": state["reply_id"], "message": body["message"], "answer": "已接續同一份工作。",
                          "state": "succeeded", "created_at": "2026-10-12T12:01:00Z"}
                if not any(m["id"] == state["reply_id"] for m in messages):
                    messages.insert(0, result)
                if state["fail_send"]:
                    state["fail_send"] = False
                    status, result = 409, {"detail": "fixture_response_lost_retry_same_request"}
        elif path == f"hermes/cases/{CASE}":
            result = {"id": CASE, "jobs": [{"id": JOB, "state": "succeeded", "worker_id": "host-coder",
                      "artifact_count": 1, "evidence_count": 1, "content_expired": False}]}
        elif path == f"worker/jobs/{JOB}":
            result = {"id": JOB, "case_id": CASE, "worker_id": "host-coder", "state": "succeeded",
                      "goal": "調查並修正畫面", "stop_confirmed": True, "artifacts": [artifact],
                      "facts": ["測試環境已重現問題"], "runtime_location": "host_fixture"}
            if state.get("web_preview"):
                result["verification"] = {"observed_from": "independent_host_verifier", "state": "passed",
                                          "web_preview": state["web_preview"]}
        elif path == f"worker/jobs/{JOB}/artifacts/{ART}":
            result = {"sha256": "0" * 64 if state["wrong_digest"] else hashlib.sha256(state["content"].encode()).hexdigest(),
                      "content_base64": base64.b64encode(state["content"].encode()).decode()}
        elif path == f"worker/jobs/{JOB}/previews" or path == f"worker/previews/{PREVIEW}":
            result = {"id": PREVIEW, "state": state["preview_state"], "preview_urls": {
                "390": "https://untrusted.invalid/candidate.html"}}
        elif path == f"worker/previews/{PREVIEW}/revoke":
            state["preview_state"] = "revoked"
            result = {"id": PREVIEW, "state": "revoked"}
        elif path.startswith(f"bridge/previews/{PREVIEW}/"):
            assert path.rsplit("/", 1)[1] in ("1440.png", "768.png", "390.png")
            route.fulfill(status=200, content_type="image/png", body=PNG,
                          headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})
            return
        elif path == f"tasks/{TASK}":
            result = {"id": TASK, "state": "succeeded" if state["approved"] else "awaiting_approval",
                      "request": "開啟測試燈", "message": "確認這次單次操作。",
                      "proposal": {**proposal, "expires": 1 if state["expired"] else proposal["expires"]}}
        elif path == f"proposals/{PID}/approve":
            assert body == {"digest": DIGEST}
            if state["fail_approve"]:
                state["fail_approve"] = False
                status, result = 409, {"detail": "fixture_approval_reply_lost"}
            else:
                state["approved"] = True
                result = {"message": "已核對執行結果。"}
        else:
            raise AssertionError("Bridge UI requested a non-allowlisted path: " + path)
        route.fulfill(status=status, content_type="application/json", body=json.dumps(result))

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            context.route("**/*", route)
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            # LAN HTTP lacks SubtleCrypto; preserve getRandomValues for idempotency keys.
            page.add_init_script("Object.defineProperty(crypto, 'subtle', {value: undefined});")
            yield page, context, f"http://127.0.0.1:{server.server_port}{PREFIX}", state, calls, mutations, messages, artifact
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def shots(page, name):
    for width in (1440, 768, 390):
        page.set_viewport_size({"width": width, "height": 900 if width == 1440 else 844})
        page.wait_for_timeout(250)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(ROOT / f"evidence/ui/bridge/{name}-{width}.png"), full_page=True)


def test_bridge_resumes_outer_case_results_png_and_exact_approval(bridge_browser):
    page, context, base, state, calls, mutations, _, _ = bridge_browser
    page.goto(base)
    expect(page.get_by_role("heading", name="智能管家", exact=True)).to_be_visible()
    expect(page.get_by_text("已整理提案與修正成果，請先確認。", exact=True)).to_be_visible()
    assert not page.locator(".sidebar").count()
    assert not page.get_by_text("進階", exact=True).count()
    assert not page.locator(".assistant-options").count()
    assert not page.get_by_role("button", name="暫停 AI").count()
    assert not page.get_by_role("link", name="設定", exact=True).count()
    assert not page.get_by_role("link", name="通訊設定", exact=True).count()
    assert not mutations
    shots(page, "conversation")

    page.get_by_label("對話訊息", exact=True).fill("這是尚未送出的草稿")
    page.get_by_role("link", name="檢查家庭操作").click()
    expect(page.get_by_role("heading", name="開啟測試燈", exact=True)).to_be_visible()
    assert not mutations
    shots(page, "approval")
    state["fail_approve"] = True
    page.get_by_role("button", name="批准並執行", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("這次沒有完成，請稍後再試。")
    assert "fixture_approval_reply_lost" not in (page.locator("body").inner_text() or "")
    page.get_by_role("button", name="批准並執行", exact=True).click()
    expect(page.get_by_text("已核對執行結果。", exact=True)).to_be_visible()
    approvals = [m for m in mutations if m[0].endswith("/approve")]
    assert len(approvals) == 2 and approvals[0] == approvals[1]
    page.get_by_role("link", name="← 回到對話").click()
    expect(page.get_by_label("對話訊息", exact=True)).to_have_value("這是尚未送出的草稿")
    page.get_by_role("button", name="查看成果", exact=True).click()
    page.get_by_role("button", name="查看內容", exact=True).click()
    expect(page.locator(".worker-artifact pre")).to_contain_text("<script>")
    assert not page.evaluate("Boolean(window.untrustedExecuted)")
    assert not page.get_by_role("button", name="下載接手檢查點").count()
    page.get_by_role("button", name="產生畫面預覽").click()
    link = page.get_by_role("link", name="手機預覽")
    expect(link).to_have_attribute("href", f"api/bridge/previews/{PREVIEW}/390.png")
    with context.expect_page() as popup:
        link.click()
    preview = popup.value
    preview.wait_for_load_state()
    assert preview.url == base + f"api/bridge/previews/{PREVIEW}/390.png"
    preview.close()
    shots(page, "results")
    page.get_by_role("button", name="關閉這份預覽").click()
    expect(link).to_have_count(0)

    state["fail_send"] = True
    page.get_by_label("對話訊息", exact=True).fill("接續檢查，不另開 case")
    page.get_by_role("button", name="送出訊息").click()
    expect(page.get_by_role("alert")).to_contain_text("這次沒有完成，請稍後再試。")
    assert "fixture_response_lost_retry_same_request" not in (page.locator("body").inner_text() or "")
    expect(page.get_by_label("對話訊息", exact=True)).to_have_value("接續檢查，不另開 case")
    # Polling already sees the accepted result; retry must retain its original parent/key.
    expect(page.get_by_text("已接續同一份工作。", exact=True)).to_be_visible(timeout=7000)
    page.get_by_role("button", name="送出訊息").click()
    expect(page.get_by_text("已接續同一份工作。", exact=True)).to_be_visible()
    sends = [m for m in mutations if m[0] == "gateway/messages"]
    assert len(sends) == 2 and sends[0] == sends[1]
    assert sends[0][1]["reply_to"] == MID
    stored = page.evaluate("JSON.stringify({...sessionStorage,...localStorage})")
    assert "接續檢查" not in stored and "fixture-bridge-csrf" not in stored
    assert "1" * 64 in stored
    assert not any(path.startswith(("host/", "bootstrap", "runtimes", "gateway/preferences")) for _, path in calls)


def test_bridge_expiry_reconnect_identity_and_readonly_artifact_failures(bridge_browser):
    page, _, base, state, calls, mutations, messages, artifact = bridge_browser
    # A pasted desktop secret is scrubbed before requests and never forwarded by an App.
    ticket = "A" * 43
    page.goto(base + "#desktop-login=" + ticket)
    expect(page.get_by_label("對話訊息", exact=True)).to_be_visible()
    assert ticket not in page.url and not any("desktop-login" in p for _, p in calls)
    page.get_by_label("對話訊息", exact=True).fill("草稿在同一家庭重新連線後保留")
    state["offline"] = True
    expect(page.get_by_role("region", name="管家連線")).to_be_visible(timeout=7000)
    expect(page.get_by_label("對話訊息", exact=True)).not_to_be_visible()
    shots(page, "offline")
    state["offline"] = False
    page.get_by_role("button", name="重新連線", exact=True).click()
    expect(page.get_by_label("對話訊息", exact=True)).to_have_value("草稿在同一家庭重新連線後保留")
    state["wrong_digest"] = True
    page.get_by_role("button", name="查看成果", exact=True).click()
    page.get_by_role("button", name="查看內容", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("檔案指紋與工作證據不符")
    expect(page.locator(".worker-artifact pre")).to_have_count(0)
    state["wrong_digest"] = False
    artifact["size"] = 2 * 1024 * 1024 + 1
    page.get_by_role("button", name="收起成果", exact=True).click()
    page.get_by_role("button", name="查看成果", exact=True).click()
    before = len([p for _, p in calls if "/artifacts/" in p])
    page.get_by_role("button", name="查看內容", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("超過瀏覽器預覽大小")
    assert len([p for _, p in calls if "/artifacts/" in p]) == before
    state["revoked"] = True
    expect(page.get_by_role("region", name="管家連線")).to_be_visible(timeout=7000)
    expect(page.get_by_text("已整理提案與修正成果，請先確認。", exact=True)).not_to_be_visible()
    shots(page, "revoked")
    state.update(revoked=False, owner="owner-b", scope="2" * 64)
    messages.clear()
    page.get_by_role("button", name="重新連線", exact=True).click()
    expect(page.get_by_label("對話訊息", exact=True)).to_have_value("")
    expect(page.get_by_text("需要協助時，直接告訴管家。", exact=True)).to_be_visible()
    assert not mutations


@pytest.mark.parametrize("bridge_state", ["unpaired", "owner_required", "unavailable"])
def test_bridge_blocked_states_never_open_outer_controls(bridge_browser, bridge_state):
    page, _, base, state, calls, mutations, _, _ = bridge_browser
    state["bridge_state"] = bridge_state
    page.goto(base + "#host")
    expect(page.get_by_role("region", name="管家連線")).to_be_visible()
    expect(page.get_by_role("button", name="重新連線", exact=True)).to_be_visible()
    assert calls and all(path == "deployment" for _, path in calls)
    assert not mutations and not page.locator('input[type="password"]').count()
    shots(page, bridge_state)


@pytest.mark.parametrize("content,expected", [
    ("", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
    ("abc", "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"),
    ("a" * 1000000, "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0"),
], ids=["empty", "abc", "million-a"])
def test_bridge_http_hash_fallback_known_vectors(bridge_browser, content, expected):
    page, _, base, state, _, _, _, artifact = bridge_browser
    state["content"] = content
    artifact.update(size=len(content), sha256=expected)
    page.goto(base)
    page.get_by_role("button", name="查看成果", exact=True).click()
    assert page.evaluate("crypto.subtle === undefined")
    page.get_by_role("button", name="查看內容", exact=True).click()
    expect(page.locator(".worker-artifact pre")).to_be_visible()
    assert page.locator(".worker-artifact pre").text_content() == content
    expect(page.get_by_role("alert")).to_have_count(0)


def test_bridge_expired_proposal_deeplink_never_approves(bridge_browser):
    page, _, base, state, calls, mutations, _, _ = bridge_browser
    state["expired"] = True
    page.goto(base + f"#home/{TASK}")
    expect(page.get_by_role("heading", name="開啟測試燈", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="批准並執行", exact=True)).to_be_disabled()
    assert page.url.endswith(f"#home/{TASK}")
    assert not mutations
    assert not any(path == "gateway/messages" for _, path in calls)


def test_outer_sidebar_maintenance_explicit_authority_and_revocation(bridge_browser):
    page, _, base, state, calls, mutations, _, _ = bridge_browser
    state["mode"] = "host"
    page.goto(base + "#settings")
    expect(page.get_by_role("heading", name="設定", exact=True)).to_be_visible()
    assert not any("bridge/maintenance" in path for _, path in calls)
    page.get_by_text("HA 智能管家側欄", exact=True).click()
    panel = page.get_by_role("region", name="側欄維護")
    expect(panel.get_by_text("尚未啟用", exact=True)).to_be_visible()
    assert not mutations
    panel.get_by_role("button", name="啟用側欄並自動維護").click()
    expect(panel.get_by_text("側欄服務可用", exact=True)).to_be_visible()
    panel.get_by_text("更多", exact=True).click()
    panel.get_by_role("button", name="停止自動維護", exact=True).click()
    expect(panel.get_by_text("自動維護已停止，側欄連接保留。", exact=False)).to_be_visible()
    assert state["maintenance"]["state"] == "healthy"
    state["maintenance"]["state"] = "user_removed"
    expect(panel.get_by_text("你已移除 App", exact=True)).to_be_visible(timeout=7000)
    expect(panel.get_by_role("button", name="檢查側欄連線")).to_have_count(0)
    panel.get_by_text("撤銷側欄連接", exact=True).click()
    panel.get_by_role("button", name="撤銷連接", exact=True).click()
    expect(panel.get_by_text("已撤銷連接", exact=True)).to_be_visible()
    assert [m[0].rsplit("/", 1)[1] for m in mutations] == ["authorize", "policy", "disable"]
    panel.get_by_role("button", name="重新啟用側欄並自動維護", exact=True).click()
    expect(panel.get_by_text("側欄服務可用", exact=True)).to_be_visible()
    assert mutations[-1][1] == {"auto_repair": True}


def test_outer_return_uses_latest_same_thread_after_sidebar_continues(bridge_browser):
    page, _, base, state, _, mutations, messages, _ = bridge_browser
    state["mode"] = "host"
    page.goto(base + "#gateway")
    expect(page.get_by_text("已整理提案與修正成果，請先確認。", exact=True)).to_be_visible()
    storage_key = "agentic-auth-conversations-" + state["scope"]
    assert json.loads(page.evaluate("key => sessionStorage.getItem(key)", storage_key))["hermes-host-codex"] == MID
    # The same outer store now contains a turn sent from the HA sidebar.
    messages.insert(0, {**messages[0], "id": NEXT, "message": "從側欄補充的限制", "answer": "已記住側欄補充。",
                       "state": "succeeded", "created_at": "2026-10-12T12:01:00Z"})
    # A newer, distinct thread must not steal the user's existing selection.
    messages.insert(0, {**messages[0], "id": "6" * 64, "thread_id": "6" * 64, "case_id": "4" * 32,
                       "message": "另一段獨立需求", "answer": "不應切換至這段對話。", "created_at": "2026-10-12T12:03:00Z"})
    state.update(expected_parent=NEXT, reply_id="9" * 64)
    page.reload()
    expect(page.get_by_text("已記住側欄補充。", exact=True)).to_be_visible()
    expect(page.get_by_text("不應切換至這段對話。", exact=True)).not_to_be_visible()
    page.get_by_label("對話訊息", exact=True).fill("回到外層繼續，保留側欄的補充")
    page.get_by_role("button", name="送出訊息", exact=True).click()
    expect(page.get_by_text("已接續同一份工作。", exact=True)).to_be_visible()
    assert mutations[-1][1]["reply_to"] == NEXT
    assert messages[0]["thread_id"] == MID and messages[0]["case_id"] == CASE


def _bridge_record(state_name, code, update=None, operation=None):
    return {"contract": 1, "state": state_name, "code": code, "auto_repair": False, "version": "0.8.0",
            "app_state": "started", "release": {"version": "0.9.0", "repository": "local"},
            "observed_at": "2026-10-13T01:02:03Z", "update": update, "operation": operation}


def test_sidebar_app_update_states_keys_and_poll(bridge_browser):
    """Fixture maintenance. Not a real HAOS App update."""
    page, _, base, state, calls, _, _, _ = bridge_browser
    state["mode"] = "host"
    app = "側欄 App"
    offered = {"available": True, "from": "0.8.0", "to": "0.9.0", "blocked_code": None}
    quiet = {"available": False, "from": "0.8.0", "to": "0.9.0", "blocked_code": None}
    rows = [
        ("update_available", "bridge_update_available", "有新版本", f"{app} 可以更新到 0.9.0（目前 0.8.0）。",
         "更新側欄 App", offered, None),
        ("update_available", "bridge_update_failed", "更新未完成", "上次更新沒有完成，目前仍是 0.8.0，可以再更新一次。",
         "更新側欄 App", offered, None),
        ("running", "bridge_observation_pending", "更新中", f"正在更新{app}，通常需要幾分鐘。", None, quiet,
         {"kind": "update", "state": "running", "job_id": "job-upd-1", "deadline": "2026-10-13T03:00:00Z"}),
        ("running", "bridge_update_starting", "啟動中", f"{app} 已更新，正在啟動。", None, quiet, None),
        ("waiting", "bridge_release_not_published", "等待中", "新版本還沒出現在 App 商店，稍後會自動再檢查。", None, quiet, None),
        ("waiting", "bridge_release_ahead", "需要更新主機", "App 商店的版本比主機新，請先更新主機。", None, quiet, None),
        ("needs_reconcile", "bridge_release_channel_changed", "需要處理",
         f"目前裝的是舊發行頻道的{app}。請在 Home Assistant 移除它，再回來重新啟用。", "檢查側欄連線", quiet, None),
        ("needs_reconcile", "bridge_update_version_mismatch", "需要處理", f"更新後的{app}和預期不同，需要你確認。",
         "檢查側欄連線", quiet, None),
        ("needs_reconcile", "bridge_update_options_mismatch", "需要處理", f"更新後的{app}和預期不同，需要你確認。",
         "檢查側欄連線", quiet, None),
        ("needs_reconcile", "bridge_update_app_missing", "需要處理", f"更新後的{app}和預期不同，需要你確認。",
         "檢查側欄連線", quiet, None),
        ("needs_reconcile", "bridge_release_unparsable", "需要處理", "App 商店的版本資料無法判讀，需要你確認。",
         "檢查側欄連線", {"available": False, "from": None, "to": None, "blocked_code": "bridge_release_unparsable"}, None),
    ]

    def show(record):
        state["maintenance"] = record
        # Same-URL goto does not reload, and a second click would close the fold.
        page.goto(base + "#settings")
        page.reload()
        page.get_by_text("HA 智能管家側欄", exact=True).click()
        panel = page.get_by_role("region", name="側欄維護")
        expect(panel).to_be_visible()
        return panel

    def maintenance_gets(start):
        return [item for item in calls[start:] if item == ("GET", "host/bridge/maintenance")]

    panel = show(_bridge_record(*rows[0][:2], update=rows[0][5], operation=rows[0][6]))
    expect(panel.locator(".status")).to_have_text(rows[0][2])
    sentence = panel.get_by_text(rows[0][3], exact=True)
    expect(sentence).to_be_visible()
    expect(sentence).to_have_attribute("data-reason", rows[0][1])
    assert rows[0][1] not in panel.inner_text()
    shots(page, "app-update")
    page.set_viewport_size({"width": 1440, "height": 900})
    tech = panel.locator("details").filter(has=page.locator("summary", has_text="技術資料"))
    tech.locator("summary").click()
    expect(tech).to_contain_text("0.8.0 → 0.9.0")
    expect(tech).to_contain_text(rows[0][1])
    expect(tech).to_contain_text("started")
    tech.locator("summary").click()

    state["hold_update"] = []
    button = panel.get_by_role("button", name=re.compile(r"^更新(側欄 App|中…)$"))
    expect(button).to_have_text("更新側欄 App")
    button.click()
    expect(button).to_be_disabled()
    expect(button).to_have_text("更新中…")
    held, key, body = state["hold_update"].pop()
    assert body == {} and len(key) == 32
    held.fulfill(status=409, content_type="application/json",
                 body=json.dumps({"detail": "這次更新沒有送出，請再試一次。"}, ensure_ascii=False))
    expect(panel.locator(".notice.error")).to_have_text("這次更新沒有送出，請再試一次。")
    assert panel.locator(".notice.error").get_attribute("data-reason") in (None, "")
    button.click()
    expect(button).to_have_text("更新中…")
    held, retry_key, retry_body = state["hold_update"].pop()
    assert retry_body == {} and retry_key == key
    before = len(calls)
    held.fulfill(status=200, content_type="application/json", body="{}")
    expect(button).to_be_enabled()
    assert maintenance_gets(before)
    button.click()
    held, new_key, new_body = state["hold_update"].pop()
    assert new_body == {} and new_key != key
    held.fulfill(status=200, content_type="application/json", body="{}")
    state["hold_update"] = None
    expect(panel.get_by_role("button", name="檢查側欄連線")).to_have_count(0)

    updating = rows[2]
    panel = show(_bridge_record(updating[0], updating[1], update=updating[5], operation=updating[6]))
    expect(panel.locator(".status")).to_have_text("更新中", timeout=8000)
    assert "job-upd-1" not in panel.inner_text()
    mark = len(calls)
    page.wait_for_timeout(6500)
    assert 1 <= len(maintenance_gets(mark)) <= 2
    state["maintenance"].update(state="healthy", code="bridge_ready", operation=None,
                                update={"available": False, "from": "0.9.0", "to": "0.9.0", "blocked_code": None})
    expect(panel.get_by_text("側欄服務可用", exact=True)).to_be_visible(timeout=8000)
    mark = len(calls)
    page.wait_for_timeout(3000)
    assert len(maintenance_gets(mark)) <= 2
    tech = panel.locator("details").filter(has=page.locator("summary", has_text="技術資料"))
    state["maintenance"] = _bridge_record(updating[0], updating[1], update=updating[5], operation=updating[6])
    expect(panel.locator(".status")).to_have_text("更新中", timeout=8000)
    tech.locator("summary").click()
    expect(tech).to_contain_text("job-upd-1")
    expect(tech).to_contain_text("0.8.0 → 0.9.0")
    expect(tech).to_contain_text("bridge_observation_pending")

    for state_name, code, chip, sentence, button_name, update, operation in rows:
        panel = show(_bridge_record(state_name, code, update=update, operation=operation))
        expect(panel.locator(".status")).to_have_text(chip)
        line = panel.get_by_text(sentence, exact=True)
        expect(line).to_have_attribute("data-reason", code)
        visible = panel.inner_text()
        assert code not in visible
        assert "job-upd-1" not in visible
        if button_name:
            expect(panel.get_by_role("button", name=button_name, exact=True)).to_be_visible()
        for absent in ("更新側欄 App", "檢查側欄連線"):
            if absent != button_name:
                expect(panel.get_by_role("button", name=absent, exact=True)).to_have_count(0)

    blocked = _bridge_record("update_available", "bridge_update_available", update={
        "available": False, "from": "0.8.0", "to": "0.9.0", "blocked_code": "bridge_update_not_allowed"})
    panel = show(blocked)
    expect(panel.get_by_text("有新版本", exact=True)).to_be_visible()
    expect(panel.get_by_role("button", name="更新側欄 App")).to_have_count(0)
    assert "bridge_update_available" not in panel.inner_text()
    assert "bridge_update_not_allowed" not in panel.inner_text()
    generic = _bridge_record("needs_reconcile", "bridge_operation_unconfirmed")
    panel = show(generic)
    expect(panel.get_by_text("需要核對", exact=True)).to_be_visible()
    expect(panel.get_by_role("button", name="檢查側欄連線", exact=True)).to_be_visible()
    expect(panel.get_by_role("button", name="更新側欄 App")).to_have_count(0)
    assert "bridge_operation_unconfirmed" not in panel.inner_text()
    assert "bridge_supervisor_job_failed" not in panel.inner_text()


def test_bridge_view_skips_host_maintenance_and_interactive_preview(bridge_browser):
    page, _, base, state, calls, mutations, _, _ = bridge_browser
    state["web_preview"] = {"state": "built", "reason": None, "artifact_digest": "a" * 64, "files": 2, "bytes": 80}
    page.goto(base)
    page.get_by_role("button", name="查看成果", exact=True).click()
    expect(page.get_by_role("heading", name="調查並修正畫面", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="開啟互動預覽")).to_have_count(0)
    page.wait_for_timeout(400)
    assert not any(path.startswith("host/") for _, path in calls)
    assert not any(body.get("mode") == "interactive" for _, body, _ in mutations if isinstance(body, dict))
