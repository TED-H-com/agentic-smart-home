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
        if path == "deployment":
            result = {"mode": "ingress", "authenticated": True}
        elif path == "session":
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
            page.get_by_role("button", name="試一次這條規則").wait_for()
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
            page.get_by_role("heading", name="登入 AI 帳號").wait_for()
            assert page.locator('footer').count() == 0
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
            page.screenshot(path=str(root / 'evidence/ui/native-home/host-login-1440.png'), full_page=True)
            page.get_by_role('button', name='開啟管理中心').click()
            page.get_by_role('region', name='開始使用').wait_for()
            click_navigation(page, 'Linux 主機')
            page.get_by_role('heading', name='家庭系統', exact=True).wait_for()
            assert page.get_by_role('button', name='正常關機', exact=True).is_disabled()
            expect(page.get_by_role('button', name='家庭未連線')).to_be_visible()
            assert page.locator('footer').count() == 0
            assert page.get_by_text('家庭控制中心').count() == 0
            for width in (1440, 768, 390):
                height = 900 if width == 1440 else 844
                page.set_viewport_size({'width': width, 'height': height})
                page.evaluate('window.scrollTo(0,0)')
                page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                start = page.get_by_role('button', name='啟動', exact=True).bounding_box()
                assert start and start['y'] >= 0 and start['y'] + start['height'] <= height, (width, start)
                page.screenshot(path=str(root / f'evidence/ui/native-home/host-{width}.png'), full_page=True)
            toggle = page.get_by_role('button', name='選單', exact=True)
            toggle.click()
            expect(page.get_by_role('button', name='收起選單', exact=True)).to_have_attribute('aria-expanded', 'true')
            expect(page.get_by_role('navigation', name='主選單').get_by_role('button', name='登出', exact=True)).to_have_count(0)
            expect(page.get_by_role('button', name='登出', exact=True)).to_be_visible()
            page.keyboard.press('Escape')
            expect(page.get_by_role('button', name='選單', exact=True)).to_have_attribute('aria-expanded', 'false')
            expect(toggle).to_be_focused()
            page.get_by_role('button', name='啟動', exact=True).click()
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
    paused = False
    pending = []
    pause_calls = []
    auth_starts = 0
    errors = []

    def route(route):
        nonlocal authenticated, connected, agent_connected, expire, auth_starts, hold_old_task, paused
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
            result = {'version':'0.5.1','paused':paused,'ha':{'status':'connected' if connected else 'unavailable', 'location_name':'介面測試家庭'},
                      'runner':{'protocol':1,'providers':[{'provider':'codex','status':'connected' if agent_connected else 'needs_reauth'}]},
                      'onboarding':{'policy_confirmed':False},'policy_entities':0}
        elif path == 'pause':
            body = route.request.post_data_json
            assert route.request.headers.get('x-agentic-csrf') == 'connected-csrf'
            assert route.request.headers.get('idempotency-key')
            assert body in ({'paused': True}, {'paused': False})
            pause_calls.append(body)
            paused = body['paused']
            result = {'paused': paused}
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
            expect(navigation.get_by_role('button')).to_have_count(6)
            for label in ('家庭', '智能管家', '設備', '自動化', '紀錄', '設定'):
                expect(navigation.get_by_role('button', name=label)).to_have_count(1)
            assert page.locator('.brand').get_attribute('href') == '#entry'
            for width in (1440,768,390):
                page.set_viewport_size({'width':width,'height':900 if width==1440 else 844})
                page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(root/f'evidence/ui/native-home/setup-{width}.png'), full_page=True)
            click_navigation(page, 'Linux 主機')
            assert page.get_by_role('link', name='開啟 Home Assistant').get_attribute('href') == 'http://127.0.0.1:8123/'
            assert page.get_by_role('link', name='個人資料 → 安全性').get_attribute('href') == 'http://127.0.0.1:8123/profile/security'
            page.get_by_label('家庭權杖').fill('fixture-owner-token')
            page.get_by_role('button', name='驗證並連接').click()
            page.get_by_role('status').filter(has_text='家庭已連接').wait_for()
            page.get_by_role('button', name='登入 AI 帳號 →', exact=True).click()
            hub = page.get_by_role('navigation', name='設定入口')
            expect(hub.get_by_role('link')).to_have_count(6)
            for link, label, href in zip(hub.get_by_role('link').all(),
                    ['AI 帳號', '通訊', '開發助手', '主機維護', '家庭提案', '初次設定'],
                    ['#agents', '#gateway/channels', '#workers', '#host', '#home', '#onboarding'], strict=True):
                assert link.inner_text().startswith(label)
                assert link.get_attribute('href') == href
            expect(hub.get_by_role('link', name='AI 帳號')).to_contain_text('尚未登入')
            expect(page.get_by_role('heading', name='AI 助手', exact=True)).to_be_visible()
            expect(page.get_by_text('Agentic Home 0.5.1', exact=True)).to_be_visible()
            assert page.locator('footer').count() == 0
            for width in (1440,768,390):
                page.set_viewport_size({'width':width,'height':900 if width==1440 else 844})
                page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(root/f'evidence/ui/native-home/settings-host-{width}.png'), full_page=True)
            page.get_by_role('button', name='暫停 AI', exact=True).click()
            expect(page.get_by_text('已暫停', exact=True)).to_be_visible()
            page.get_by_role('button', name='AI 已暫停 · 恢復', exact=True).click()
            expect(page.get_by_role('button', name='暫停 AI', exact=True)).to_be_visible()
            expect(page.get_by_text('運作中', exact=True)).to_be_visible()
            assert pause_calls == [{'paused': True}, {'paused': False}]
            hub.get_by_role('link', name='AI 帳號').click()
            runtime = page.get_by_role('region', name='開發助手帳號', exact=True)
            runtime.get_by_role('heading', name='主機的 AI 帳號', exact=True).wait_for()
            codex = runtime.locator('article').filter(has=page.get_by_role('heading', name='Codex', exact=True))
            codex.get_by_role('button', name='開始登入', exact=True).click()
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
            assert page.get_by_role('link', name='開啟家庭自動化').get_attribute('href') == 'http://127.0.0.1:8123/config/automation/dashboard'
            page.reload()
            page.get_by_role('heading', name='自動化', exact=True).wait_for()
            click_navigation(page, '工作台')
            page.get_by_label('家庭需求').fill('這段尚未送出的需求需要保留')
            expire = True
            hold_old_task = True
            page.get_by_text('登入已到期，重新登入後會回到剛才的頁面。', exact=True).wait_for(timeout=20000)
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
            page.get_by_role('heading', name='服務尚未回應').wait_for()
            retry = True
            page.get_by_role('button', name='重試連線').click()
            page.get_by_label('主機登入金鑰').wait_for()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_update_source_commit_and_sync_are_plain_and_short():
    """Version fold shows a short deployed commit and a plain sync line. Fixture, not a release."""
    root = Path(__file__).parents[1]
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(SimpleHTTPRequestHandler, directory=str(root / 'controller/static')))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    deployed_sha = 'd1e5c0d' + 'a' * 33
    manifest_sha = 'b' * 40
    deployed = {'present': True, 'value': deployed_sha}
    sync = {'value': 'moved'}
    errors = []
    labels = {'pending': '程式碼同步中', 'moved': '程式碼已同步', 'reverted': '程式碼已還原', 'needs_reconcile': '程式碼需要處理'}

    def payload():
        record = {'candidate_digest': 'c' * 64, 'state': 'deployed',
                  'manifest': {'version': '0.8.0', 'source_commit': manifest_sha, 'tests': [{'name': 'browser', 'result': 'passed'}]}}
        if sync['value'] is not None:
            record['source_sync'] = sync['value']
        body = {'trusted_publisher_configured': True, 'records': [record]}
        if deployed['present']:
            body['deployed_source_commit'] = deployed['value']
        return body

    def route(route):
        path = route.request.url.split('/api/')[1]
        if path == 'deployment':
            result = {'mode': 'host', 'authenticated': True}
        elif path == 'session':
            result = {'principal': 'host-owner', 'csrf': 'fixture-csrf', 'mode': 'host'}
        elif path == 'status':
            result = {'version': 'fixture', 'paused': False, 'ha': {'status': 'connected'}, 'runner': {'protocol': 1, 'providers': []}}
        elif path == 'tasks':
            result = {'tasks': []}
        elif path == 'host/status':
            result = {'vm': 'running', 'provisioned': True, 'jobs': [], 'snapshots': [],
                      'profile': {'label': '家庭筆電', 'memory_mib': 4096, 'vcpus': 2}}
        elif path == 'updates':
            result = payload()
        else:
            raise AssertionError(path)
        route.fulfill(status=200, content_type='application/json', body=json.dumps(result))

    def refold():
        summary = page.locator('summary').filter(has_text='版本')
        summary.click()
        expect(page.get_by_text('先檢查簽署的版本說明', exact=False)).to_have_count(0)
        summary.click()
        expect(page.get_by_text('先檢查簽署的版本說明', exact=False)).to_be_visible()

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 900})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/api/**', route)
            page.goto(f'http://127.0.0.1:{server.server_port}/#host')
            page.get_by_role('heading', name='主機維護', exact=True).wait_for()
            page.locator('summary').filter(has_text='版本').click()
            code = page.locator('p').filter(has_text='目前程式版本').locator('code')
            expect(code).to_have_text(deployed_sha[:7])
            expect(code).to_have_attribute('title', deployed_sha)
            expect(page.get_by_text(labels['moved'], exact=True)).to_be_visible()
            assert deployed_sha not in page.locator('body').inner_text()
            assert manifest_sha not in page.locator('body').inner_text()
            output = root / 'evidence/ui/host-source'
            output.mkdir(parents=True, exist_ok=True)
            for width in (1440, 390):
                page.set_viewport_size({'width': width, 'height': 900 if width == 1440 else 844})
                page.evaluate('window.scrollTo(0,0)')
                page.wait_for_timeout(200)
                expect(code).to_have_text(deployed_sha[:7])
                expect(page.get_by_text(labels['moved'], exact=True)).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
                page.screenshot(path=str(output / f'sync-moved-{width}.png'), full_page=True)
            page.set_viewport_size({'width': 1440, 'height': 900})
            for key in ('pending', 'reverted', 'needs_reconcile'):
                sync['value'] = key
                refold()
                expect(page.get_by_text(labels[key], exact=True)).to_be_visible()
                for other in labels.values():
                    if other != labels[key]:
                        expect(page.get_by_text(other, exact=True)).to_have_count(0)
                visible = page.locator('details.host-fold').filter(has=page.locator('summary', has_text='版本')).inner_text()
                assert key not in visible
            expect(page.get_by_text('需要核對', exact=True)).to_have_count(0)
            for omitted in ('unavailable', None):
                sync['value'] = omitted
                refold()
                for text in labels.values():
                    expect(page.get_by_text(text, exact=True)).to_have_count(0)
                visible = page.locator('details.host-fold').filter(has=page.locator('summary', has_text='版本')).inner_text()
                assert 'unavailable' not in visible and 'needs_reconcile' not in visible
            deployed['value'] = None
            refold()
            expect(page.get_by_text('目前程式版本', exact=False)).to_have_count(0)
            deployed['present'] = False
            refold()
            expect(page.get_by_text('目前程式版本', exact=False)).to_have_count(0)
            deployed['present'] = True
            deployed['value'] = 'g' * 40
            refold()
            expect(page.get_by_text('目前程式版本', exact=False)).to_have_count(0)
            page.get_by_text('技術資料', exact=True).click()
            expect(page.get_by_text(manifest_sha, exact=True)).to_be_visible()
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_update_record_error_is_a_sentence_and_the_code_stays_folded():
    """A failed release shows a sentence. The raw code stays in 技術資料. Fixture, not a release."""
    root = Path(__file__).parents[1]
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(SimpleHTTPRequestHandler, directory=str(root / 'controller/static')))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    code = 'release_apply_failed'
    manifest_sha = 'b' * 40
    record = {'candidate_digest': 'c' * 64, 'state': 'failed', 'error_code': code,
              'manifest': {'version': '0.8.1', 'source_commit': manifest_sha, 'tests': [{'name': 'browser', 'result': 'passed'}]}}
    errors = []

    def route(route):
        path = route.request.url.split('/api/')[1]
        if path == 'deployment':
            result = {'mode': 'host', 'authenticated': True}
        elif path == 'session':
            result = {'principal': 'host-owner', 'csrf': 'fixture-csrf', 'mode': 'host'}
        elif path == 'status':
            result = {'version': 'fixture', 'paused': False, 'ha': {'status': 'connected'}, 'runner': {'protocol': 1, 'providers': []}}
        elif path == 'tasks':
            result = {'tasks': []}
        elif path == 'host/status':
            result = {'vm': 'running', 'provisioned': True, 'jobs': [], 'snapshots': [],
                      'profile': {'label': '家庭筆電', 'memory_mib': 4096, 'vcpus': 2}}
        elif path == 'updates':
            result = {'trusted_publisher_configured': True, 'records': [record]}
        else:
            raise AssertionError(path)
        route.fulfill(status=200, content_type='application/json', body=json.dumps(result))

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 900})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/api/**', route)
            page.goto(f'http://127.0.0.1:{server.server_port}/#host')
            page.get_by_role('heading', name='主機維護', exact=True).wait_for()
            page.locator('summary').filter(has_text='版本').click()
            review = page.get_by_role('region', name='更新候選版本')
            notice = review.locator('.notice.error')
            expect(notice).to_have_text('這次更新沒有完成。')
            expect(notice).to_have_attribute('data-reason', code)
            assert code not in page.locator('body').inner_text()
            output = root / 'evidence/ui/host-update'
            output.mkdir(parents=True, exist_ok=True)
            for width in (1440, 390):
                page.set_viewport_size({'width': width, 'height': 900 if width == 1440 else 844})
                page.evaluate('window.scrollTo(0,0)')
                page.wait_for_timeout(200)
                expect(notice).to_have_text('這次更新沒有完成。')
                assert code not in page.locator('body').inner_text()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
                page.screenshot(path=str(output / f'error-{width}.png'), full_page=True)
            page.set_viewport_size({'width': 1440, 'height': 900})
            review.get_by_text('技術資料', exact=True).click()
            expect(review.locator('dt', has_text='原因代碼')).to_be_visible()
            expect(review.locator('code', has_text=code)).to_be_visible()
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _ui_server():
    root = Path(__file__).parents[1]
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(SimpleHTTPRequestHandler, directory=str(root / 'controller/static')))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return root, server, thread


def _stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


def test_plain_error_hides_raw_detail_and_unknown_status():
    """Code, English, and coded failures show one sentence. Fixture API, not a live host."""
    root, server, thread = _ui_server()
    mode = {'kind': 'code'}
    task = {'id': 'fixture-task', 'provider': 'codex', 'request': '未知狀態檢查', 'state': 'not_a_state',
            'message': '狀態只顯示在標籤。', 'created_at': '2026-10-13T01:02:03Z', 'updated_at': '2026-10-13T01:02:03Z'}
    errors = []

    def route(route):
        path = route.request.url.split('/api/')[1].split('?')[0]
        if path == 'deployment':
            result = {'mode': 'host', 'authenticated': True}
        elif path == 'session':
            result = {'principal': 'host-owner', 'csrf': 'fixture-csrf', 'mode': 'host'}
        elif path == 'status':
            result = {'version': 'fixture', 'paused': False, 'ha': {'status': 'connected'},
                      'runner': {'protocol': 1, 'providers': []}}
        elif path == 'tasks':
            kind = mode['kind']
            if kind == 'code':
                route.fulfill(status=409, content_type='application/json', body='{"detail":"artifact_not_found"}')
                return
            if kind == 'english404':
                route.fulfill(status=404, content_type='application/json', body='{"detail":"missing record"}')
                return
            if kind == 'english500':
                route.fulfill(status=500, content_type='application/json', body='{"detail":"internal boom"}')
                return
            if kind == 'coded':
                body = {'detail': {'code': 'worker_queue_full', 'message': '目前工作太多，請稍後再試。'}}
                route.fulfill(status=409, content_type='application/json', body=json.dumps(body, ensure_ascii=False))
                return
            result = {'tasks': [task]}
        else:
            result = {}
        route.fulfill(status=200, content_type='application/json', body=json.dumps(result))

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 900})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/api/**', route)
            page.goto(f'http://127.0.0.1:{server.server_port}/#history')
            banner = page.locator('.global-error .notice.error')
            expect(banner).to_contain_text('這份資料已過期或不存在。')
            expect(banner).to_have_attribute('data-reason', 'artifact_not_found')
            assert 'artifact_not_found' not in (banner.inner_text() or '')
            mode['kind'] = 'english404'
            page.get_by_role('button', name='重新連線', exact=True).click()
            expect(banner).not_to_have_attribute('data-reason', 'artifact_not_found')
            expect(banner).to_contain_text('這份資料已過期或不存在。')
            assert 'missing record' not in (page.locator('body').inner_text() or '')
            mode['kind'] = 'english500'
            page.get_by_role('button', name='重新連線', exact=True).click()
            expect(banner).to_contain_text('這次沒有完成，請稍後再試。')
            assert banner.get_attribute('data-reason') in (None, '')
            assert 'internal boom' not in (page.locator('body').inner_text() or '')
            mode['kind'] = 'coded'
            page.get_by_role('button', name='重新連線', exact=True).click()
            expect(banner).to_have_attribute('data-reason', 'worker_queue_full')
            expect(banner).to_contain_text('目前工作太多，請稍後再試。')
            assert 'worker_queue_full' not in (banner.inner_text() or '')
            mode['kind'] = 'ok'
            page.get_by_role('button', name='重新連線', exact=True).click()
            status = page.locator('.history-table .status')
            expect(status).to_have_text('未知狀態')
            expect(status).to_have_attribute('data-reason', 'not_a_state')
            assert 'not_a_state' not in (status.inner_text() or '')
            output = root / 'evidence/ui/plain-error'
            output.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output / 'unknown-status-1440.png'), full_page=True)
            assert not errors, errors
            browser.close()
    finally:
        _stop(server, thread)


def test_maintenance_fault_sentences_keep_the_code_off_the_sentence():
    """Authorize faults show the mapped sentence. The code stays in data-reason. Fixture API."""
    root, server, thread = _ui_server()
    posts, errors = [], []

    def coder(worker_id):
        return {'worker_id': worker_id, 'available': True, 'state': 'ready', 'reason': '',
                'runtime_location': 'host_worker_container', 'modes': ['research', 'development'],
                'providers': [{'id': 'codex', 'supported': True, 'models': [], 'auth_methods': ['device_code'],
                               'credential_state': 'needs_auth'}]}

    def route(route):
        request = route.request
        path = request.url.split('/api/')[1].split('?')[0]
        if request.method == 'POST':
            posts.append({'path': path, 'body': request.post_data_json, 'key': request.headers.get('idempotency-key'),
                          'csrf': request.headers.get('x-agentic-csrf')})
            if path == 'host/home-worker/maintenance/authorize':
                route.fulfill(status=409, content_type='application/json', body='{"detail":"home_worker_busy"}')
                return
            if path == 'host/bridge/maintenance/authorize':
                route.fulfill(status=409, content_type='application/json', body='{"detail":"bridge_update_not_allowed"}')
                return
            raise AssertionError(path)
        if path == 'deployment':
            result = {'mode': 'host', 'authenticated': True}
        elif path == 'session':
            result = {'principal': 'host-owner', 'csrf': 'fixture-csrf', 'mode': 'host'}
        elif path == 'status':
            result = {'version': 'fixture', 'paused': False, 'ha': {'status': 'connected'},
                      'runner': {'protocol': 1, 'providers': []}}
        elif path == 'tasks':
            result = {'tasks': []}
        elif path == 'workers':
            result = {'workers': [coder('host-coder'), coder('home-coder')], 'active_job_id': None}
        elif path == 'worker/jobs':
            result = {'jobs': [], 'active_job_id': None}
        elif path == 'host/home-worker/maintenance':
            result = {'contract': 1, 'state': 'not_authorized', 'code': None}
        elif path == 'host/bridge/maintenance':
            result = {'contract': 1, 'state': 'not_authorized', 'code': None, 'auto_repair': False}
        elif path == 'services':
            result = {'services': []}
        else:
            result = {}
        route.fulfill(status=200, content_type='application/json', body=json.dumps(result))

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 900})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/api/**', route)
            origin = f'http://127.0.0.1:{server.server_port}'
            page.goto(origin + '/#workers')
            page.get_by_role('radio', name='家庭現場', exact=True).check()
            card = page.get_by_role('region', name='家庭現場開發助手')
            card.get_by_role('button', name='啟用家庭現場', exact=True).click()
            home = card.locator('.notice.error')
            expect(home).to_have_text('開發助手正在工作，完成後再試。')
            expect(home).to_have_attribute('data-reason', 'home_worker_busy')
            assert 'home_worker_busy' not in (home.inner_text() or '')
            page.goto(origin + '/#settings')
            page.locator('summary').filter(has_text='HA 智能管家側欄').click()
            panel = page.get_by_role('region', name='側欄維護')
            panel.get_by_role('button', name='啟用側欄並自動維護', exact=True).click()
            bridge = panel.locator('.notice.error')
            expect(bridge).to_have_text('目前不能更新，請重新整理後再試。')
            expect(bridge).to_have_attribute('data-reason', 'bridge_update_not_allowed')
            assert 'bridge_update_not_allowed' not in (bridge.inner_text() or '')
            assert [item['path'] for item in posts] == [
                'host/home-worker/maintenance/authorize', 'host/bridge/maintenance/authorize']
            assert posts[0]['body'] == {} and posts[1]['body'] == {'auto_repair': True}
            assert all(item['csrf'] == 'fixture-csrf' and item['key'] and len(item['key']) == 32 for item in posts)
            output = root / 'evidence/ui/plain-error'
            output.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output / 'bridge-fault-1440.png'), full_page=True)
            assert not errors, errors
            browser.close()
    finally:
        _stop(server, thread)
