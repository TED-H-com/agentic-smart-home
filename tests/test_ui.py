"""Browser interaction and responsive layout with explicit API fixtures, not hardware evidence."""
import json
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from agentic_controller.models import compile_automation, digest, utcnow
from conftest import SOURCE, TARGET
from playwright.sync_api import expect, sync_playwright
from ui_helpers import click_navigation


def test_browser_approval_and_responsive_layout(intent):
    root = Path(__file__).parents[1]
    if not (root / "controller/static/index.html").exists():
        pytest.skip("Build the web UI before the browser test")
    handler = partial(SimpleHTTPRequestHandler, directory=str(root / "controller/static"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    resource = "agentic_" + "f" * 32
    final = compile_automation(intent, resource)
    proposal = {"id": "fixture-proposal", "intent": intent.model_dump(), "mode": "upsert", "prior": None,
        "final": final, "digest": digest(final), "expires": time.time() + 600, "action": None}
    task = {"id": "fixture-task", "provider": "codex", "request": "當測試動作感應器啟動，就打開測試燈",
        "state": "awaiting_approval", "message": "請檢查變更內容；批准前不會寫入 Home Assistant",
        "created_at": utcnow(), "proposal_id": proposal["id"], "proposal": proposal, "events": []}
    requests = []
    errors = []

    def route(route):
        request = route.request
        path = request.url.split("/api/")[1]
        data = None
        if request.method == "POST":
            data = request.post_data_json
            requests.append((path, data, request.headers))
        if path == "session":
            result = {"csrf": "fixture-csrf", "principal": "fixture-owner"}
        elif path == "status":
            result = {"version": "0.1.0", "ha": {"location_name": "介面測試資料", "status": "connected", "version": "fixture"},
                      "runner": {"protocol": 1, "providers": [{"provider": "codex", "status": "connected", "version": "0.157.1"},
                      {"provider": "claude", "status": "needs_reauth", "version": "2.1.296"}]}, "events_connected": True}
        elif path == "tasks":
            result = task if request.method == "POST" else {"tasks": [task]}
        elif path.startswith("tasks/"):
            result = task
        elif path.endswith("/approve"):
            assert data["digest"] == proposal["digest"]
            assert request.headers.get("x-agentic-csrf") == "fixture-csrf"
            assert request.headers.get("idempotency-key")
            task["state"], task["message"] = "verifying", "已部署並啟用，正在等待真實狀態觸發"
            proposal["deployed_at"] = utcnow()
            result = task
        elif path == "inventory":
            result = {"entities": [{"entity_id": id, "name": name, "area": "專用測試區", "domain": "input_boolean",
                       "state": "off", "availability": "available", "actions": ["turn_on", "turn_off"], "observed_at": utcnow()}
                       for id, name in [(SOURCE, "測試動作感應器"), (TARGET, "測試燈")]], "policy": {"entities": []}}
        elif path == "services":
            from agentic_controller.app import SERVICES
            result = {"services": [{"id": id, **s, "state": "not_started"} for id, s in SERVICES.items()]}
        elif path == "retention":
            result = {"audit_days": 30, "context_days": 7}
        else:
            result = {}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(result))

    evidence = root / "evidence/ui"
    evidence.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.add_init_script("Object.defineProperty(crypto, 'randomUUID', {value: undefined});")
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/api/**", route)
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            page.get_by_role("button", name="填入 test helpers 範例").click()
            page.get_by_role("button", name="送出需求").click()
            page.get_by_role("button", name="批准並執行").wait_for()
            assert not any(p.endswith("/approve") for p, _, _ in requests)
            for width in (1440, 768, 390):
                page.set_viewport_size({"width": width, "height": 900 if width == 1440 else 844})
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), f"Overflow at {width}"
                page.screenshot(path=str(evidence / f"workbench-{width}.png"), full_page=True)
            page.get_by_role("button", name="批准並執行").click()
            page.get_by_role("button", name="執行 helper 測試").wait_for()
            assert len([p for p, _, _ in requests if p.endswith("/approve")]) == 1
            click_navigation(page, '設備與權限')
            page.get_by_role("checkbox", name=f"分享 {SOURCE}").check()
            page.get_by_role("combobox", name=f"{SOURCE} 操作權限").select_option("dedicated_helper")
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(evidence / "inventory-390.png"), full_page=True)
            click_navigation(page, '連線與設定')
            page.get_by_role("button", name="繼續編輯").wait_for()
            page.get_by_role("button", name="繼續編輯").click()
            page.get_by_role("button", name="儲存修改", exact=True).click()
            page.get_by_role("status").filter(has_text="設備授權已儲存").wait_for()
            click_navigation(page, '連線與設定')
            page.get_by_role("heading", name="登入你的 Agent").wait_for()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(evidence / "settings-390.png"), full_page=True)
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_host_login_offline_management_and_responsive_layout():
    root = Path(__file__).parents[1]
    handler = partial(SimpleHTTPRequestHandler, directory=str(root / 'controller/static'))
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    authenticated = False
    actions, errors = [], []

    def route(route):
        nonlocal authenticated
        path = route.request.url.split('/api/')[1]
        if path == 'deployment':
            result = {'mode': 'host', 'authenticated': authenticated}
        elif path == 'host/login':
            authenticated = True
            result = {'authenticated': True}
        elif path == 'session':
            result = {'principal': 'host-owner', 'csrf': 'fixture-host-csrf', 'mode': 'host'}
        elif path == 'status':
            result = {'version': '0.5.1', 'ha': {'status': 'unavailable'}, 'runner': {'status': 'unpaired'}}
        elif path == 'tasks':
            result = {'tasks': []}
        elif path == 'services':
            result = {'services': []}
        elif path == 'retention':
            result = {'audit_days': 30, 'context_days': 7}
        elif path == 'host/status':
            result = {'vm': 'shut off', 'provisioned': True, 'kvm': True, 'ha_connected': False,
                      'profile': {'label': '筆電（介面 fixture）', 'memory_mib': 4096, 'vcpus': 2},
                      'snapshots': [], 'jobs': []}
        elif path == 'host/actions':
            assert route.request.headers.get('x-agentic-csrf') == 'fixture-host-csrf'
            actions.append(route.request.post_data_json)
            result = {'state': 'running'}
        else:
            result = {}
        route.fulfill(status=200, content_type='application/json', body=json.dumps(result))

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 900})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/api/**', route)
            page.goto(f'http://127.0.0.1:{server.server_port}/#home')
            page.get_by_label('主機登入金鑰').fill('fixture-local-machine-key')
            page.screenshot(path=str(root / 'evidence/ui/host-login-1440.png'), full_page=True)
            page.get_by_role('button', name='開啟管理中心').click()
            page.get_by_role('region', name='開始使用').wait_for()
            click_navigation(page, 'Linux 主機')
            page.get_by_role('heading', name='Home Assistant OS', exact=True).wait_for()
            assert page.get_by_role('button', name='正常關機', exact=True).is_disabled()
            for width in (1440, 768, 390):
                page.set_viewport_size({'width': width, 'height': 900 if width == 1440 else 844})
                page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                page.screenshot(path=str(root / f'evidence/ui/host-{width}.png'), full_page=True)
            page.get_by_role('button', name='啟動 HAOS', exact=True).click()
            page.get_by_role('status').filter(has_text='操作已交給主機處理').wait_for()
            assert actions == [{'action': 'start', 'snapshot': None}]
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_setup_links_session_recovery_and_login_resume():
    """Exercise the actual UI transitions; HA/provider responses are protocol fixtures."""
    root = Path(__file__).parents[1]
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(SimpleHTTPRequestHandler, directory=str(root / 'controller/static')))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    authenticated, connected, agent_connected = True, False, False
    expire = False
    hold_old_task = False
    pending = []
    auth_starts = 0
    errors = []

    def route(route):
        nonlocal authenticated, connected, agent_connected, expire, auth_starts, hold_old_task
        path = route.request.url.split('/api/')[1]
        if hold_old_task and path == 'tasks':
            hold_old_task = False
            pending.append(route)
            return
        if expire and path == 'status':
            expire = False
            authenticated = False
            route.fulfill(status=401, content_type='application/json', body='{"detail":"登入已過期"}')
            return
        if path == 'deployment':
            result = {'mode':'host', 'authenticated':authenticated}
        elif path == 'session':
            result = {'principal':'owner' if connected else 'host-owner', 'csrf':'connected-csrf' if connected else 'initial-csrf'}
        elif path == 'host/login':
            authenticated = True
            result = {'authenticated':True}
        elif path == 'status':
            result = {'version':'0.5.1','ha':{'status':'connected' if connected else 'unavailable', 'location_name':'介面測試家庭'},
                      'runner':{'protocol':1,'providers':[{'provider':'codex','status':'connected' if agent_connected else 'needs_reauth'}]},
                      'onboarding':{'policy_confirmed':False},'policy_entities':0}
        elif path == 'tasks':
            result = {'tasks':[]}
        elif path == 'host/status':
            result = {'vm':'running','provisioned':True,'ha_connected':connected,'snapshots':[],'jobs':[],
                      'storage_free_bytes':120*1024**3,'profile':{'label':'Ubuntu 家庭主機（介面 fixture）','memory_mib':4096,'vcpus':2}}
        elif path == 'host/connect':
            assert route.request.headers['x-agentic-csrf'] == 'initial-csrf'
            connected = True
            result = {'connected':True}
        elif path == 'services':
            from agentic_controller.app import SERVICES
            result = {'services':[{'id':id, **item, 'state':'not_started'} for id,item in SERVICES.items()]}
        elif path == 'automations':
            result = {'automations':[]}
        elif path == 'retention':
            result = {'audit_days':30,'context_days':7}
        elif path == 'runtimes':
            result = {'host': {'protocol':1, 'providers':[{'provider':'codex',
                'status':'connected' if agent_connected else 'needs_reauth'}]}, 'haos': {'status':'unpaired'}}
        elif path == 'hermes':
            result = {'runtime': {'state':'needs_auth', 'execution_state':'idle'}, 'workers': []}
        elif path == 'runtimes/host/providers/codex/auth':
            assert route.request.headers['x-agentic-csrf'] == 'connected-csrf'
            assert route.request.post_data_json == {'method':'device_code', 'secret':''}
            auth_starts += 1
            result = {'id':'fixtureauth','status':'pending','url':'https://auth.openai.com/codex/device','code':'ABCD-1234'}
        elif path == 'runtimes/host/auth/fixtureauth':
            result = {'id':'fixtureauth','status':'connected' if agent_connected else 'pending','url':'https://auth.openai.com/codex/device','code':'ABCD-1234'}
        else:
            result = {}
        route.fulfill(status=200, content_type='application/json', body=json.dumps(result))

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width':1440,'height':900})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/api/**', route)
            page.goto(f'http://127.0.0.1:{server.server_port}/#home')
            guide = page.get_by_role('region', name='開始使用')
            guide.get_by_text('設定尚未完成', exact=True).wait_for()
            expect(guide.get_by_role('button', name='繼續設定', exact=True)).to_be_visible()
            navigation = page.get_by_role('navigation', name='主選單')
            expect(navigation.get_by_role('button')).to_have_count(5)
            for label in ('智能管家', '設備', '自動化', '紀錄', '設定'):
                expect(navigation.get_by_role('button', name=label)).to_have_count(1)
            assert page.locator('.brand').get_attribute('href') == '#gateway'
            for width in (1440,768,390):
                page.set_viewport_size({'width':width,'height':900 if width==1440 else 844})
                page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(root/f'evidence/ui/setup-{width}.png'), full_page=True)
            click_navigation(page, 'Linux 主機')
            assert page.get_by_role('link', name='開啟 Home Assistant').get_attribute('href') == 'http://127.0.0.1:8123/'
            assert page.get_by_role('link', name='個人資料 → 安全性').get_attribute('href') == 'http://127.0.0.1:8123/profile/security'
            page.get_by_label('HA owner 權杖').fill('fixture-owner-token')
            page.get_by_role('button', name='驗證並連接').click()
            page.get_by_role('button', name='設定 Agent →', exact=True).wait_for()
            page.get_by_role('button', name='設定 Agent →', exact=True).click()
            hub = page.get_by_role('navigation', name='設定入口')
            expect(hub.get_by_role('link')).to_have_count(6)
            assert hub.get_by_role('link', name='通訊入口').get_attribute('href') == '#gateway/channels'
            for width in (1440,768,390):
                page.set_viewport_size({'width':width,'height':900 if width==1440 else 844})
                page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(root/f'evidence/ui/settings-host-{width}.png'), full_page=True)
            hub.get_by_role('link', name='助手帳號').click()
            runtime = page.get_by_role('region', name='Agent 執行環境', exact=True)
            runtime.get_by_role('heading', name='登入外層 Agent', exact=True).wait_for()
            codex = runtime.locator('article').filter(has=page.get_by_role('heading', name='Codex', exact=True))
            codex.get_by_role('button', name='開始官方登入', exact=True).click()
            page.get_by_text('ABCD-1234', exact=True).wait_for()
            click_navigation(page, '工作台')
            click_navigation(page, 'Agent 環境')
            page.get_by_text('ABCD-1234', exact=True).wait_for()
            assert auth_starts == 1, 'Returning to the page must resume the same login'
            assert page.evaluate("sessionStorage.getItem('agentic-auth-host-codex')") == 'fixtureauth'
            click_navigation(page, '連線與設定')
            page.get_by_text('家庭服務', exact=True).click()
            links = page.get_by_role('link', name='開啟設定')
            assert links.count() == 3
            for link in links.all():
                expect(link).to_be_visible()
                assert link.get_attribute('href').startswith('http://127.0.0.1:8123/')
                assert link.get_attribute('target') == '_blank'
            page.get_by_text('備份與復原', exact=True).click()
            backup = page.get_by_role('link', name='HA 備份')
            expect(backup).to_be_visible()
            assert backup.get_attribute('href') == 'http://127.0.0.1:8123/config/backup/overview'
            click_navigation(page, '家庭自動化')
            assert page.get_by_role('link', name='開啟 HA 原生自動化').get_attribute('href') == 'http://127.0.0.1:8123/config/automation/dashboard'
            page.reload()
            page.get_by_role('heading', name='自動化', exact=True).wait_for()
            click_navigation(page, '工作台')
            page.get_by_label('家庭需求').fill('這段尚未送出的需求需要保留')
            expire = True
            hold_old_task = True
            page.get_by_role('heading', name='歡迎回來').wait_for(timeout=20000)
            page.get_by_label('主機登入金鑰').fill('fixture-local-machine-key')
            page.get_by_role('button', name='開啟管理中心').click()
            page.get_by_label('家庭需求').wait_for()
            assert page.get_by_label('家庭需求').input_value() == '這段尚未送出的需求需要保留'
            assert len(pending) == 1
            with page.expect_response(lambda response: response.url.endswith('/api/tasks') and response.status == 401) as late:
                pending.pop().fulfill(status=401, content_type='application/json', body='{"detail":"舊連線回覆"}')
            late.value.finished()
            page.wait_for_timeout(200)  # Allow the stale response's async JSON/error handler to run.
            assert page.get_by_label('家庭需求').is_visible(), 'A late 401 from before login must not invalidate the new session'
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_bootstrap_failure_shows_retry_without_guessing_deployment():
    root = Path(__file__).parents[1]
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(SimpleHTTPRequestHandler, directory=str(root/'controller/static')))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    retry = False

    def route(route):
        if not retry:
            route.abort()
        else:
            route.fulfill(status=200, content_type='application/json', body='{"mode":"host","authenticated":false}')

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.route('**/api/deployment', route)
            page.goto(f'http://127.0.0.1:{server.server_port}/#home')
            page.get_by_role('heading', name='管理中心尚未回應').wait_for()
            retry = True
            page.get_by_role('button', name='重試連線').click()
            page.get_by_label('主機登入金鑰').wait_for()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
