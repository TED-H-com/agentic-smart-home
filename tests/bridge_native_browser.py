"""Actual native HA/Ingress browser helper; no API routes or identity headers mocked.

Credentials remain in the disposable browser/process. The caller supplies a real
HAOS test instance and controls fixture inference. Returned evidence contains
only fixed checks, public asset hashes and synthetic conversation identifiers.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import async_playwright


class NativeBridgeBrowserError(RuntimeError):
    def __init__(self, step: str, error_type: str, evidence: dict):
        self.evidence = evidence
        # Never include Playwright call logs, locators containing private paths,
        # request bodies, browser storage, credentials or original exception text.
        super().__init__(f"native_bridge_browser_failed:{step}:{error_type}")


async def exercise_native_bridge(
    *,
    base_url: str,
    username: str,
    password: str,
    slug: str,
    ingress_url: str,
    screenshot: Path,
    expected_answer: str = "HAOS transport inference fixture",
    expected_case_id: str | None = None,
    expected_thread_id: str | None = None,
) -> dict:
    """Log in via native HA, open its App panel and verify real UI continuation.

    `base_url` is a loopback-forwarded disposable HAOS origin, not a production
    household. `ingress_url` is the current Supervisor-provided relative path.
    A native sidebar click is preferred; the official HA App route is a fallback
    and is reported distinctly. No bearer is injected into browser storage.
    """
    started = time.monotonic()
    phase = "validate_fixture_inputs"
    report = {"kind": "native_ha_ingress_browser", "status": "running",
              "observed_from": "chromium_native_ha_to_actual_ingress_app",
              "no_api_or_header_mocking": True, "checks": {}}
    browser = None
    listeners: set[asyncio.Task] = set()
    messages: dict[str, dict] = {}
    observed: dict = {}
    errors: set[str] = set()
    try:
        origin = urlsplit(base_url)
        if (origin.scheme not in {"http", "https"} or origin.hostname not in {"127.0.0.1", "localhost"}
                or origin.username or origin.password or origin.path not in {"", "/"}
                or origin.query or origin.fragment):
            raise ValueError("disposable_loopback_origin_required")
        base_url = base_url.rstrip("/")
        if not re.fullmatch(r"[a-z0-9_]{1,100}", slug):
            raise ValueError("invalid_app_slug")
        if not re.fullmatch(r"/api/hassio_ingress/[A-Za-z0-9_-]+/?", ingress_url):
            raise ValueError("invalid_ingress_path")
        ingress_path = ingress_url.rstrip("/")
        for value, size in ((expected_case_id, 32), (expected_thread_id, 64)):
            if value is not None and not re.fullmatch(rf"[a-f0-9]{{{size}}}", value):
                raise ValueError("invalid_expected_identity")
        if not username or not password or not expected_answer:
            raise ValueError("missing_fixture_input")
        screenshot = Path(screenshot)
        if screenshot.suffix != ".png":
            raise ValueError("screenshot_must_be_png")

        async def observe(response):
            try:
                url = urlsplit(response.url)
                if url.netloc != origin.netloc or not url.path.startswith(ingress_path + "/"):
                    return
                path = url.path[len(ingress_path):]
                if path.startswith("/assets/") and path.endswith(".js") and response.status == 200:
                    body = await response.body()
                    if len(body) <= 2 * 1024 * 1024:
                        observed["compiled_js_sha256"] = hashlib.sha256(body).hexdigest()
                    return
                if response.status != 200 or response.request.method != "GET":
                    return
                if path not in {"/api/deployment", "/api/session", "/api/gateway/messages"}:
                    return
                data = await response.json()
                if path == "/api/deployment":
                    observed["deployment_ready"] = (data.get("mode") == "bridge" and
                        data.get("authenticated") is True and data.get("bridge_state") == "ready")
                elif path == "/api/session":
                    observed["bridge_session"] = data.get("mode") == "bridge"
                    observed["resume"] = data.get("resume")
                else:
                    for item in data.get("messages", []):
                        identifier = item.get("id", "")
                        if re.fullmatch(r"[a-f0-9]{64}", identifier):
                            messages[identifier] = {key: item.get(key) for key in
                                ("id", "thread_id", "case_id", "reply_to", "state", "coordinator", "source")}
            except Exception as error:
                # An observation error is diagnosed without logging response data.
                errors.add(type(error).__name__)

        def response_seen(response):
            task = asyncio.create_task(observe(response))
            listeners.add(task)
            task.add_done_callback(listeners.discard)

        async def wait_for_message(identifier):
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                value = messages.get(identifier)
                if value and value.get("state") == "succeeded" and value.get("case_id"):
                    return value
                if value and value.get("state") in {"failed", "cancelled", "expired"}:
                    raise AssertionError("conversation_did_not_succeed")
                await asyncio.sleep(0.1)
            raise TimeoutError("conversation_observation_timeout")

        async with async_playwright() as playwright:
            phase = "launch_chromium"
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="en-US")
            page = await context.new_page()
            page.set_default_timeout(60000)
            page.on("response", response_seen)
            page.on("pageerror", lambda error: errors.add(type(error).__name__))
            phase = "native_owner_login"
            await page.goto(base_url + "/", wait_until="domcontentloaded")
            username_field = page.get_by_label("Username", exact=True)
            await username_field.or_(page.locator("home-assistant-main")).first.wait_for()
            if await username_field.is_visible():
                await username_field.fill(username)
                await page.get_by_label("Password", exact=True).fill(password)
                await page.get_by_role("button", name="Log in", exact=True).click()
                report["checks"]["native_login_form_used"] = True
            else:
                raise AssertionError("fresh_browser_must_use_native_login")
            await page.locator("home-assistant-main").wait_for()
            report["checks"]["native_owner_login"] = True

            phase = "native_http_settings_confirmation"
            confirmation = page.get_by_role("button", name="Confirm", exact=True)
            try:
                await confirmation.wait_for(timeout=10000)
            except BrowserTimeout:
                report["checks"]["native_http_confirmation_needed"] = False
            else:
                await confirmation.click()
                await confirmation.wait_for(state="hidden")
                report["checks"]["native_http_confirmation_needed"] = True

            phase = "open_native_app_panel"
            sidebar = page.locator("ha-sidebar").get_by_role("link", name="智能管家", exact=True)
            try:
                await sidebar.wait_for(timeout=10000)
            except BrowserTimeout:
                await page.goto(base_url + "/hassio/ingress/" + slug, wait_until="domcontentloaded")
                report["entry_method"] = "official_native_app_route"
            else:
                await sidebar.click()
                report["entry_method"] = "native_sidebar_link"
            iframe = page.locator(f'iframe[src*="{ingress_path}"]')
            await iframe.wait_for()
            frame = page.frame_locator(f'iframe[src*="{ingress_path}"]')
            phase = "compiled_bridge_ui"
            await frame.locator(".bridge-shell").wait_for()
            await frame.get_by_role("heading", name="智能管家", exact=True).wait_for()
            composer = frame.get_by_label("對話訊息", exact=True)
            await composer.wait_for()
            if await frame.locator(".sidebar, .assistant-options").count():
                raise AssertionError("outer_controls_in_bridge")
            # Response-body observers are asynchronous; a visible composer does
            # not guarantee the observer callback has finished reading its body.
            observation_deadline = time.monotonic() + 10
            while time.monotonic() < observation_deadline:
                if (observed.get("deployment_ready") and observed.get("bridge_session") and
                        observed.get("compiled_js_sha256")):
                    break
                await asyncio.sleep(0.05)
            if not observed.get("deployment_ready") or not observed.get("bridge_session"):
                raise AssertionError("bridge_identity_not_observed")
            if not re.fullmatch(r"[a-f0-9]{64}", observed.get("compiled_js_sha256", "")):
                raise AssertionError("compiled_asset_not_observed")
            report["checks"]["compiled_bridge_ui"] = True
            report["compiled_js_sha256"] = observed["compiled_js_sha256"]
            initial = observed.get("resume") or {}
            if expected_case_id is not None and initial.get("case_id") != expected_case_id:
                raise AssertionError("initial_case_not_resumed")
            if expected_thread_id is not None and initial.get("thread_id") != expected_thread_id:
                raise AssertionError("initial_thread_not_resumed")
            report["checks"]["existing_case_resumed"] = expected_case_id is not None

            async def send(text):
                await composer.fill(text)
                async with page.expect_response(lambda response:
                    urlsplit(response.url).path == ingress_path + "/api/gateway/messages" and
                    response.request.method == "POST") as response_event:
                    await frame.get_by_role("button", name="送出訊息", exact=True).click()
                response = await response_event.value
                if response.status != 202:
                    raise AssertionError("native_ingress_post_rejected")
                result = await response.json()
                identifier = result.get("id", "")
                if not re.fullmatch(r"[a-f0-9]{64}", identifier):
                    raise AssertionError("conversation_handle_invalid")
                article = frame.locator(".assistant-message").filter(has_text=text)
                await article.get_by_text(expected_answer, exact=True).wait_for()
                return await wait_for_message(identifier)

            suffix = secrets.token_hex(4)
            phase = "native_ingress_first_send"
            first = await send("側欄瀏覽器連線測試 " + suffix)
            if expected_case_id is not None and first["case_id"] != expected_case_id:
                raise AssertionError("first_send_changed_case")
            if expected_thread_id is not None and first["thread_id"] != expected_thread_id:
                raise AssertionError("first_send_changed_thread")
            report["checks"]["actual_ingress_post_csrf_and_origin"] = True

            phase = "native_panel_reload"
            await page.reload(wait_until="domcontentloaded")
            await iframe.wait_for()
            await composer.wait_for()
            await frame.get_by_role("heading", name="側欄瀏覽器連線測試 " + suffix, exact=True).wait_for()
            phase = "native_ingress_continue"
            second = await send("接續同一份側欄工作 " + suffix)
            if (second["thread_id"] != first["thread_id"] or second["case_id"] != first["case_id"] or
                    second["reply_to"] != first["id"] or second["coordinator"] != "hermes" or second["source"] != "web"):
                raise AssertionError("native_continuation_changed_scope")
            report["checks"]["reload_and_same_case_continuation"] = True
            report["case_id"], report["thread_id"] = second["case_id"], second["thread_id"]
            report["message_ids"] = [first["id"], second["id"]]
            phase = "native_fullframe_screenshot"
            if await page.get_by_label("Password", exact=True).is_visible():
                raise AssertionError("login_form_still_visible")
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(screenshot), full_page=True)
            report["checks"]["native_fullframe_screenshot"] = True
            report["browser_error_types"] = sorted(errors)
            report["status"] = "passed"
            report["duration_seconds"] = round(time.monotonic() - started, 3)
            await context.close()
            await browser.close()
            browser = None
        return report
    except Exception as error:
        report.update(status="failed", failed_step=phase, error_type=type(error).__name__,
                      duration_seconds=round(time.monotonic() - started, 3), browser_error_types=sorted(errors))
        raise NativeBridgeBrowserError(phase, type(error).__name__, report) from None
    finally:
        if listeners:
            # A closed browser cannot finish outstanding body reads. Do not let
            # diagnostics delay the VM caller's cleanup indefinitely.
            remaining = tuple(listeners)
            for task in remaining:
                task.cancel()
            await asyncio.gather(*remaining, return_exceptions=True)
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
