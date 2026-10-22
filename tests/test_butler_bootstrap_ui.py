"""Compiled browser UI with synthetic setup/auth responses; no cloud acceptance."""
import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright


@pytest.mark.parametrize("stale_failure", [False, True])
def test_primary_butler_setup_resumes_and_ignores_older_progress(tmp_path, stale_failure):
    root = Path(__file__).parents[1]
    server = ThreadingHTTPServer(("127.0.0.1", 0),
        partial(SimpleHTTPRequestHandler, directory=str(root / "controller/static")))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    steps = ["network", "runner_pairing", "ha_provision", "hermes_install", "hermes_auth", "ha_native",
             "cli_auth", "cli_assist", "channel"]
    flow = {"contract": 1, "id": None, "state": "idle", "provider": None, "channel": "web",
            "current_step": None, "human_action": None, "updated_at": None,
            "steps": [{"id": step, "state": "pending", "code": None, "verified_at": None} for step in steps]}
    calls, errors, held = [], [], []
    control = {"hold": False}

    def stage(step, state="running", action=None):
        flow.update(id="fixture-new-setup", provider="codex", current_step=step, state=state, human_action=action)
        index = steps.index(step)
        for i, item in enumerate(flow["steps"]):
            item.update(state="completed" if i < index else state if i == index else "pending", code=None)
            if item["id"] in {"cli_auth", "cli_assist", "channel"}:
                item.update(state="skipped", code="coding_setup_deferred" if item["id"] != "channel"
                            else "channel_explicitly_skipped")

    def route(route):
        request = route.request
        path = request.url.split("/api/")[1]
        calls.append((request.method, path))
        if path == "deployment":
            result = {"mode": "host", "authenticated": True}
        elif path == "session":
            result = {"principal": "host-owner", "csrf": "fixture-csrf"}
        elif path == "status":
            result = {"ha": {"status": "unavailable"}, "version": "fixture", "paused": False}
        elif path == "tasks":
            result = {"tasks": []}
        elif path == "bootstrap":
            if control["hold"]:
                control["hold"] = False
                held.append((route, json.dumps(flow)))
                return
            result = flow
        elif path == "bootstrap/start":
            assert request.post_data_json == {"provider": "codex", "channel": "web"}
            assert request.headers["x-agentic-csrf"] == "fixture-csrf"
            assert request.headers["idempotency-key"]
            stage("ha_provision")
            # An accepted setup with a lost response must resume from status.
            route.abort("failed")
            return
        elif path == "bootstrap/retry":
            assert request.post_data_json == {}
            stage("hermes_auth", "waiting_input", {"kind": "hermes_login"})
            result = flow
        elif path == "hermes":
            result = {"runtime": {"state": "needs_auth", "provider": "openai-codex", "model": "",
                                  "default_models": {"openai-codex": "fixture-model"}}, "workers": []}
        else:
            raise AssertionError("Unexpected new setup request: " + path)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(result))

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/api/**", route)
            page.goto(f"http://127.0.0.1:{server.server_port}/#onboarding")
            page.get_by_role("heading", name="開始準備你的家庭", exact=True).wait_for()
            expect(page.get_by_role("radiogroup", name="使用的助手")).to_have_count(0)
            expect(page.get_by_role("list", name="自動設定進度").locator("li")).to_have_count(3)
            for width in (1440, 768, 390):
                page.set_viewport_size({"width": width, "height": 900 if width == 1440 else 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                start = page.get_by_role("button", name="開始設定", exact=True)
                assert start.bounding_box()["y"] + start.bounding_box()["height"] <= page.viewport_size["height"]
                page.screenshot(path=str(tmp_path / f"butler-start-{width}.png"), full_page=True)
            page.get_by_role("button", name="開始設定", exact=True).click()
            page.get_by_text("正在建立家庭環境，請保持電源與網路。", exact=True).wait_for()
            assert calls.count(("POST", "bootstrap/start")) == 1
            expect(page.locator("article.provider")).to_have_count(0)
            stage("ha_provision", "blocked", {"kind": "retry", "message": "家庭準備需要重試。"})
            retry = page.get_by_role("button", name="重試這一步", exact=True)
            retry.wait_for()
            control["hold"] = True
            for _ in range(35):
                if held:
                    break
                page.wait_for_timeout(100)
            assert len(held) == 1
            retry.click()
            login = page.get_by_role("region", name="智能管家登入")
            login.wait_for()
            old, body = held.pop()
            old.fulfill(status=503 if stale_failure else 200, content_type="application/json",
                        body='{"detail":"stale fixture failure"}' if stale_failure else body)
            page.wait_for_timeout(100)
            expect(login).to_be_visible()
            expect(page.get_by_role("button", name="重試這一步", exact=True)).to_have_count(0)
            expect(page.get_by_text("請重試連線。", exact=True)).to_have_count(0)
            expect(page.get_by_role("heading", name="選擇你的通訊入口")).to_have_count(0)
            page.reload()
            page.get_by_role("region", name="智能管家登入").wait_for()
            assert calls.count(("POST", "bootstrap/start")) == 1
            assert not any(path.startswith("runtimes/") or path.startswith("gateway/") for _, path in calls)
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
