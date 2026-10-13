"""Home-worker enable flow against protocol fixtures. No real account, HAOS, or device."""
import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).parents[1]
STEPS = ['network', 'runner_pairing', 'cli_auth', 'cli_assist', 'hermes_install', 'hermes_auth',
         'channel', 'ha_provision', 'ha_native']
AUTH_PATH = '/api/workers/home-coder/providers/codex/auth'
POLL_PATH = '/api/workers/home-coder/auth/{id}'


@pytest.fixture
def ui_page():
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(SimpleHTTPRequestHandler, directory=str(ROOT / 'controller/static')))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 900})
            yield page, f'http://127.0.0.1:{server.server_port}'
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def shot(page, name):
    output = ROOT / 'evidence/ui/home-worker'
    output.mkdir(parents=True, exist_ok=True)
    for width in (1440, 768, 390):
        height = 900 if width == 1440 else 844
        page.set_viewport_size({'width': width, 'height': height})
        page.evaluate('window.scrollTo(0,0)')
        page.wait_for_timeout(100)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
        if width != 768:
            page.screenshot(path=str(output / f'{name}-{width}.png'), full_page=True)


def in_view(page, locator, height):
    page.evaluate('window.scrollTo(0,0)')
    box = locator.bounding_box()
    assert box and box['y'] >= 0 and box['y'] + box['height'] <= height, box


def coder(worker_id, credential, available=True):
    return {'worker_id': worker_id, 'available': available, 'state': 'ready', 'reason': '',
            'runtime_location': 'host_worker_container' if worker_id == 'host-coder' else 'haos_app',
            'modes': ['research', 'development'],
            'providers': [{'id': 'codex', 'supported': True, 'models': [], 'auth_methods': ['device_code', 'api_key'],
                           'credential_state': credential}]}


def maintenance(state, **extra):
    record = {'contract': 1, 'state': state, 'stage': extra.get('stage'), 'code': extra.get('code'),
              'enabled': state != 'not_authorized', 'managed': state != 'not_authorized',
              'release': {'repository': 'local', 'app': 'agentic_coder', 'version': extra.get('release', '1.2.3')},
              'credential_state': extra.get('credential_state', 'needs_auth'), 'worker_available': extra.get('worker_available', False),
              'runtime_location': 'haos_app',
              'login': {'worker_id': 'home-coder', 'provider': 'codex', 'show': state in ('login_ready', 'ready'),
                        'auth_path': AUTH_PATH, 'auth_poll_path': POLL_PATH},
              'restart_allowed': extra.get('restart_allowed', False),
              'limitations': extra.get('limitations', ['workspace_has_no_network']),
              'version': extra.get('version', '9.9.9'), 'slug': extra.get('slug', 'agentic_coder'),
              'app_state': extra.get('app_state', 'started'), 'fingerprint': extra.get('fingerprint'),
              'observed_at': extra.get('observed_at', '2026-10-13T01:02:03Z'), 'operation': extra.get('operation')}
    return record


def test_enable_flow_reuses_key_and_maps_owner_fault(ui_page):
    page, origin = ui_page
    flow = {'state': 'not_authorized', 'code': None, 'authorize': 0}
    posts, errors = [], []

    def route(route):
        request = route.request
        path = request.url.split('/api/')[1]
        body = request.post_data_json if request.method == 'POST' else None
        if body is not None:
            posts.append({'path': path, 'key': request.headers['idempotency-key'],
                          'csrf': request.headers['x-agentic-csrf'], 'body': body})
        if path == 'host/home-worker/maintenance/authorize':
            flow['authorize'] += 1
            if flow['authorize'] == 1:
                route.fulfill(status=409, content_type='application/json',
                              body=json.dumps({'detail': 'home_worker_owner_required'}))
                return
            flow.update(state='user_removed', code='home_worker_user_removed')
            route.fulfill(status=200, content_type='application/json', body='{}')
            return
        if path == 'host/home-worker/maintenance/repair':
            flow.update(state='running', code=None, stage='install')
            route.fulfill(status=200, content_type='application/json', body='{}')
            return
        if path == 'host/home-worker/maintenance/disable':
            flow.update(state='user_disabled', code='home_worker_observation_disabled')
            route.fulfill(status=200, content_type='application/json', body='{}')
            return
        value = {'deployment': {'mode': 'host', 'authenticated': True},
                 'session': {'principal': 'host-owner', 'csrf': 'fixture-csrf'},
                 'status': {'ha': {'status': 'connected'}, 'runner': {'protocol': 1, 'providers': []}, 'paused': False},
                 'tasks': {'tasks': []}, 'workers': {'workers': [coder('host-coder', 'needs_auth'), coder('home-coder', 'needs_auth', False)], 'active_job_id': None},
                 'worker/jobs': {'jobs': [], 'active_job_id': None}}.get(path)
        if path == 'host/home-worker/maintenance':
            value = maintenance(flow['state'], code=flow.get('code'), stage=flow.get('stage'))
        elif value is None:
            value = {}
        route.fulfill(status=200, content_type='application/json', body=json.dumps(value))

    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/api/**', route)
    page.goto(origin + '/#workers')
    page.get_by_role('radio', name='家庭現場', exact=True).check()
    card = page.get_by_role('region', name='家庭現場開發助手')
    card.get_by_role('button', name='啟用家庭現場', exact=True).wait_for()
    expect(page.locator('.worker-readiness')).to_contain_text('尚未啟用')
    expect(card.locator('.status')).to_have_text('尚未啟用')
    expect(card).to_contain_text('會在家庭安裝開發助手 App；你停止或移除後不會自動恢復。')
    page.set_viewport_size({'width': 390, 'height': 844})
    in_view(page, card.get_by_role('button', name='啟用家庭現場', exact=True), 844)
    shot(page, 'enable')
    page.set_viewport_size({'width': 1440, 'height': 900})
    card.locator('summary').filter(has_text='詳細說明').click()
    expect(card).to_contain_text('App 需要 SYS_ADMIN 與 /dev/fuse 來建立隔離工作區，不取得家庭管理權限。')
    expect(card).to_contain_text('程式工作在沒有網路的工作區執行，登入資料只存在家庭裡。')
    card.get_by_role('button', name='啟用家庭現場', exact=True).click()
    expect(card.locator('.notice.error')).to_contain_text('請用家庭擁有者帳號登入後再試。')
    expect(card.locator('.notice.error small')).to_have_text('home_worker_owner_required')
    card.get_by_role('button', name='啟用家庭現場', exact=True).click()
    card.get_by_role('button', name='重新安裝', exact=True).wait_for()
    card.get_by_role('button', name='重新安裝', exact=True).click()
    card.locator('summary').filter(has_text='更多').click()
    expect(card).to_contain_text('不會移除 App 或登入資料。')
    card.get_by_role('button', name='停止維護', exact=True).click()
    expect(card).to_contain_text('你已停止維護家庭現場。')
    assert [item['path'] for item in posts] == [
        'host/home-worker/maintenance/authorize', 'host/home-worker/maintenance/authorize',
        'host/home-worker/maintenance/repair', 'host/home-worker/maintenance/disable']
    assert posts[0]['key'] == posts[1]['key'] and len(posts[0]['key']) == 32
    assert posts[2]['key'] != posts[0]['key'] and posts[3]['key'] not in {posts[0]['key'], posts[2]['key']}
    assert all(item['csrf'] == 'fixture-csrf' and item['body'] == {} for item in posts)
    assert not errors


def test_home_progress_login_repair_poll_and_baseline_copy(ui_page):
    page, origin = ui_page
    flow = {'state': 'running', 'code': None, 'stage': 'repository', 'restart_allowed': False,
            'refuse': None, 'bad': False, 'signed': False, 'fingerprint': 'abcdef1234567890ffff',
            'operation': {'kind': 'repository', 'state': 'running', 'job_id': 'job-open-1', 'deadline': '2026-10-13T02:00:00Z'},
            'limitations': ['workspace_has_no_network', 'no_docker_socket']}
    job = {'id': '9' * 32, 'case_id': '8' * 32, 'worker_id': 'host-coder', 'mode': 'development', 'provider': 'codex',
           'goal': '調整首頁間距', 'state': 'succeeded', 'execution_started': True, 'stop_confirmed': True,
           'created_at': '2026-10-13T01:00:00Z', 'verification': {'observed_from': 'independent_host_verifier', 'state': 'passed', 'tests_executed': 12, 'patch_sha256': 'a' * 64}}
    posts, gets, errors = [], [], []
    show_job = {'on': False}

    def route(route):
        request = route.request
        path = request.url.split('/api/')[1]
        body = request.post_data_json if request.method == 'POST' else None
        if path == 'host/home-worker/maintenance' and request.method == 'GET':
            gets.append(path)
            if flow['bad']:
                route.fulfill(status=200, content_type='application/json', body='{"contract":2}')
                return
        if body is not None and path.startswith('host/home-worker/maintenance/'):
            posts.append(path)
            if flow['refuse']:
                route.fulfill(status=409, content_type='application/json', body=json.dumps({'detail': flow['refuse']}))
                return
            route.fulfill(status=200, content_type='application/json', body='{}')
            return
        if path == 'workers/home-coder/providers/codex/auth':
            posts.append(path)
            assert path == AUTH_PATH.removeprefix('/api/')
            route.fulfill(status=200, content_type='application/json',
                          body=json.dumps({'id': 'b' * 32, 'status': 'pending', 'url': 'https://auth.openai.com/codex/device', 'code': 'HOME-CODE'}))
            return
        home_cred = 'configured' if flow['signed'] else 'needs_auth'
        value = {'deployment': {'mode': 'host', 'authenticated': True},
                 'session': {'principal': 'host-owner', 'csrf': 'fixture-csrf'},
                 'status': {'ha': {'status': 'connected'}, 'runner': {'protocol': 1, 'providers': []}, 'paused': False},
                 'tasks': {'tasks': []},
                 'workers': {'workers': [coder('host-coder', 'needs_auth'), coder('home-coder', home_cred, flow['state'] == 'ready')], 'active_job_id': None},
                 'worker/jobs': {'jobs': [job] if show_job['on'] else [], 'active_job_id': None}}.get(path)
        if path == 'host/home-worker/maintenance':
            value = maintenance(flow['state'], code=flow.get('code'), stage=flow.get('stage'), restart_allowed=flow['restart_allowed'],
                                fingerprint=flow['fingerprint'], operation=flow['operation'], limitations=flow['limitations'])
        elif value is None:
            value = {}
        route.fulfill(status=200, content_type='application/json', body=json.dumps(value))

    def show():
        page.reload()
        page.get_by_label('要調查或改善什麼？', exact=True).wait_for()
        page.get_by_role('radio', name='家庭現場', exact=True).check()
        return page.get_by_role('region', name='家庭現場開發助手')

    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/api/**', route)
    page.goto(origin + '/#workers')
    card = show()
    expect(card.get_by_role('status')).to_have_text('加入 App 來源…')
    expect(card.get_by_role('status')).not_to_contain_text('job-open-1')
    expect(page.locator('.worker-readiness')).to_contain_text('正在設定')
    expect(page.get_by_role('button', name='開始調查', exact=True)).to_be_disabled()
    for name in ('啟用家庭現場', '重新檢查', '重新啟動 App', '重新安裝', '重新啟用'):
        expect(card.get_by_role('button', name=name, exact=True)).to_have_count(0)
    tech = card.locator('details').filter(has=page.locator('summary', has_text='技術資料'))
    tech.locator('summary').click()
    expect(tech).to_contain_text('job-open-1')
    expect(tech).to_contain_text('abcdef123456')
    expect(tech).not_to_contain_text('abcdef1234567890ffff')
    expect(tech).to_contain_text('workspace_has_no_network')
    expect(tech).to_contain_text('1.2.3')
    for stage, sentence in (('install', '安裝 App…'), ('start', '啟動 App…'), ('pair', '配對中…'), ('login', '準備登入…'), ('monitor', '確認狀態…'), ('other', '正在設定…')):
        flow.update(state='running', stage=stage, code=None)
        card = show()
        expect(card.get_by_role('status')).to_have_text(sentence)
        expect(card.get_by_role('button', name='重新檢查', exact=True)).to_have_count(0)
    waiting = {'home_worker_app_starting': 'App 啟動中…', 'home_worker_pairing_pending': '等待配對…',
               'home_worker_coding_busy': '等目前的開發工作完成。', 'home_worker_observation_pending': '正在確認…', None: '稍候會自動繼續。'}
    for code, sentence in waiting.items():
        flow.update(state='waiting', stage=None, code=code)
        card = show()
        expect(card.get_by_role('status')).to_have_text(sentence)
        expect(page.locator('.worker-readiness')).to_contain_text('等待家庭就緒')
        expect(card.get_by_role('button', name='重新檢查', exact=True)).to_have_count(0)
    unavailable = {'home_worker_app_not_published': '這個版本的家庭現場 App 尚未發布。', 'home_worker_paused': 'AI 已暫停，恢復後會繼續。',
                   'home_worker_update_in_progress': '主機更新中，完成後會繼續。', 'home_worker_supervisor_not_healthy': '家庭系統忙碌中，稍後會再試。',
                   'home_worker_worker_unavailable': '家庭現場暫時沒有回應。', 'home_worker_capability_mismatch': '家庭現場暫時沒有回應。',
                   'home_worker_other': '暫時連不上家庭現場。'}
    for code, sentence in unavailable.items():
        flow.update(state='unavailable', code=code)
        card = show()
        expect(card).to_contain_text(sentence)
        expect(card.get_by_role('button', name='重新檢查', exact=True)).to_be_visible()
    flow.update(state='needs_reconcile', code='home_worker_needs_reconcile')
    card = show()
    link = card.get_by_role('link', name='開啟 HA App 設定')
    expect(link).to_have_attribute('href', 'http://127.0.0.1:8123/hassio/dashboard')
    expect(link).to_have_attribute('target', '_blank')
    expect(link).to_have_attribute('rel', 'noreferrer')
    flow.update(state='user_modified', code='home_worker_user_modified')
    card = show()
    expect(card).to_contain_text('App 設定被修改，已停止自動維護。')
    expect(card.get_by_role('link', name='開啟 HA App 設定')).to_have_attribute('target', '_blank')
    flow.update(state='user_disabled', code='home_worker_app_stopped')
    card = show()
    expect(card).to_contain_text('你在家庭停止了 App。')
    expect(card.get_by_role('button', name='重新啟用', exact=True)).to_be_visible()
    flow.update(state='degraded', code='home_worker_app_stopped', restart_allowed=False)
    card = show()
    expect(card).to_contain_text('開發工作進行中，完成後再重新啟動。')
    expect(card.get_by_role('button', name='重新啟動 App', exact=True)).to_have_count(0)
    flow['restart_allowed'] = True
    card = show()
    expect(card).to_contain_text('App 停止運作，重新啟動後可繼續使用。')
    card.get_by_role('button', name='重新啟動 App', exact=True).click()
    assert posts[-1] == 'host/home-worker/maintenance/restart'
    flow.update(state='user_removed', code='home_worker_user_removed', restart_allowed=False)
    card = show()
    card.get_by_role('button', name='重新安裝', exact=True).click()
    assert posts[-1] == 'host/home-worker/maintenance/repair'
    flow.update(state='login_ready', code=None, signed=False)
    page.reload()
    page.get_by_label('要調查或改善什麼？', exact=True).wait_for()
    page.get_by_role('radio', name='家庭現場', exact=True).check()
    expect(page.locator('.worker-readiness')).to_contain_text('需要登入')
    expect(page.get_by_role('region', name='家庭現場開發助手')).to_have_count(0)
    expect(page.locator('.worker-inputs summary').filter(has_text='更多')).to_have_count(1)
    expect(page.get_by_role('button', name='開始調查', exact=True)).to_be_disabled()
    page.get_by_role('button', name='登入 Codex 開發助手', exact=True).click()
    page.get_by_text('HOME-CODE', exact=True).wait_for()
    assert posts[-1] == 'workers/home-coder/providers/codex/auth'
    page.set_viewport_size({'width': 390, 'height': 844})
    in_view(page, page.get_by_role('button', name='取消登入', exact=True), 844)
    shot(page, 'login-ready')
    flow.update(state='ready', signed=True)
    page.set_viewport_size({'width': 1440, 'height': 900})
    page.reload()
    page.get_by_label('要調查或改善什麼？', exact=True).wait_for()
    page.get_by_role('radio', name='家庭現場', exact=True).check()
    expect(page.locator('.worker-readiness')).to_contain_text('已就緒')
    expect(page.get_by_role('region', name='家庭現場開發助手')).to_have_count(0)
    expect(page.get_by_text('已登入 Codex', exact=True)).to_be_visible()
    page.get_by_label('要調查或改善什麼？', exact=True).fill('檢查首頁間距')
    expect(page.get_by_role('button', name='開始調查', exact=True)).to_be_enabled()
    page.locator('.worker-inputs summary').filter(has_text='更多').click()
    expect(page.get_by_role('button', name='停止維護', exact=True)).to_be_visible()
    expect(page.locator('.worker-inputs')).to_contain_text('不會移除 App 或登入資料。')
    flow.update(state='not_authorized', signed=False, bad=True)
    card = show()
    expect(page.locator('.worker-readiness')).to_contain_text('無法讀取')
    flow['bad'] = False
    card.get_by_role('button', name='重新讀取', exact=True).click()
    card.get_by_role('button', name='啟用家庭現場', exact=True).wait_for()
    flow['refuse'] = 'home_worker_setup_required'
    card.get_by_role('button', name='啟用家庭現場', exact=True).click()
    expect(card).to_contain_text('先完成初次設定。')
    expect(card.get_by_role('link', name='前往初次設定')).to_have_attribute('href', '#onboarding')
    expect(card.locator('.notice.error small')).to_have_text('home_worker_setup_required')
    flow['refuse'] = None
    show_job['on'] = True
    page.set_viewport_size({'width': 1440, 'height': 900})
    page.reload()
    section = page.locator('.worker-findings').filter(has=page.get_by_role('heading', name='測試', exact=True))
    expect(section).to_contain_text('主機基準測試通過（12 項）。')
    expect(section).to_contain_text('基準測試使用固定的模擬家庭，不等於你家的實際驗收。')
    expect(section).not_to_contain_text('獨立測試')
    page.get_by_role('radio', name='家庭現場', exact=True).check()
    page.get_by_role('button', name='啟用家庭現場', exact=True).wait_for()
    before = len(gets)
    page.wait_for_timeout(6500)
    assert len(gets) > before
    page.get_by_role('radio', name='主機', exact=True).check()
    page.wait_for_timeout(400)
    frozen = len(gets)
    page.wait_for_timeout(6500)
    assert len(gets) == frozen
    shot(page, 'workers')
    assert not errors


def test_coding_login_terminal_states_offer_retry(ui_page):
    page, origin = ui_page
    auth = {'status': 'failed'}
    errors, posts = [], []
    outcomes = {'failed': '登入未完成，請重新登入。', 'expired': '這次登入已過期，請重新登入。',
                'stop_unconfirmed': '尚未確認登入程序停止，請先核對後再登入。', 'interrupted': '登入已中斷，請重新登入。',
                'cancelled': '已取消這次登入。', 'disconnected': '已登出開發助手。', 'unverified': '尚未確認登入結果，請重新登入。',
                'connected': '登入沒有完成，請重新登入。', 'configured': '登入沒有完成，請重新登入。', 'succeeded': '登入沒有完成，請重新登入。',
                'mystery': '登入沒有完成，請重新登入。'}

    def route(route):
        request = route.request
        path = request.url.split('/api/')[1]
        if request.method == 'POST':
            posts.append(path)
        value = {'deployment': {'mode': 'host', 'authenticated': True}, 'session': {'principal': 'host-owner', 'csrf': 'fixture-csrf'},
                 'status': {'ha': {'status': 'connected'}, 'runner': {'protocol': 1, 'providers': []}, 'paused': False},
                 'tasks': {'tasks': []}, 'workers': {'workers': [coder('host-coder', 'needs_auth')], 'active_job_id': None},
                 'worker/jobs': {'jobs': [], 'active_job_id': None}}.get(path, {})
        if path == f"workers/host-coder/auth/{'a' * 32}":
            value = {'id': 'a' * 32, 'status': auth['status']}
        route.fulfill(status=200, content_type='application/json', body=json.dumps(value))

    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/api/**', route)
    page.goto(origin + '/#workers')
    page.get_by_label('要調查或改善什麼？', exact=True).wait_for()
    auth['status'] = 'verifying'
    page.evaluate("sessionStorage.setItem('agentic-coding-auth-host-coder','%s')" % ('a' * 32))
    page.reload()
    expect(page.locator('.coding-login').get_by_role('status')).to_have_text('正在確認登入…')
    expect(page.get_by_role('button', name='重新登入', exact=True)).to_have_count(0)
    for status, sentence in outcomes.items():
        auth['status'] = status
        page.evaluate("sessionStorage.setItem('agentic-coding-auth-host-coder','%s')" % ('a' * 32))
        page.reload()
        expect(page.locator('.coding-login').get_by_role('status')).to_contain_text(sentence)
        page.get_by_role('button', name='重新登入', exact=True).click()
        expect(page.get_by_label('開發助手登入方式')).to_be_visible()
    assert not posts and not errors
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def test_completion_prompt_settings_detail_and_single_host_status(ui_page):
    page, origin = ui_page
    coding = {'credential': 'needs_auth', 'available': True, 'fail': False}
    gateway = {'on': False}
    errors, held = [], []
    released = {'ok': False}
    status = {'ha': {'status': 'connected'}, 'version': '1.0.0', 'paused': False,
              'runner': {'protocol': 1, 'providers': [{'provider': 'codex', 'status': 'needs_reauth'},
                          {'provider': 'claude', 'status': 'connected'}, {'provider': 'grok', 'status': 'connected'}]}}

    def workers_body():
        return {'workers': [coder('host-coder', coding['credential'], coding['available'])], 'active_job_id': None}

    def route(route):
        request = route.request
        path = request.url.split('/api/')[1]
        if path == 'workers':
            if not released['ok']:
                held.append(route)
                return
            if coding['fail']:
                route.fulfill(status=500, content_type='application/json', body='{"detail":"workers_unavailable"}')
                return
            route.fulfill(status=200, content_type='application/json', body=json.dumps(workers_body()))
            return
        current = dict(status)
        if gateway['on']:
            current['gateway'] = {'selected': True, 'web': {'status': 'connected'}, 'preferences': {'primary': 'line'},
                                  'channels': [{'id': 'line', 'status': 'connected'}, {'id': 'telegram', 'status': 'disabled'}]}
        bootstrap = {'contract': 1, 'id': 'bootstrap-fixture', 'state': 'completed', 'provider': 'codex', 'channel': 'line',
                     'current_step': None, 'human_action': None, 'updated_at': '2026-10-13T01:00:00Z',
                     'steps': [{'id': step, 'state': 'completed', 'code': None, 'verified_at': '2026-10-13T01:00:00Z'} for step in STEPS]}
        value = {'deployment': {'mode': 'host', 'authenticated': True}, 'session': {'principal': 'host-owner', 'csrf': 'fixture-csrf'},
                 'status': current, 'tasks': {'tasks': []}, 'bootstrap': bootstrap,
                 'host/home-entry': {'contract_version': 1, 'state': 'ready', 'native_path': '/', 'reason_code': None},
                 'host/status': {'vm': 'running', 'provisioned': True, 'ha_connected': True, 'jobs': [], 'snapshots': [],
                                 'storage_free_bytes': 80 * 1024 ** 3, 'profile': {'label': '家庭筆電', 'memory_mib': 4096, 'vcpus': 2}},
                 'services': {'services': []}, 'retention': {'audit_days': 30, 'context_days': 7},
                 'worker/jobs': {'jobs': [], 'active_job_id': None}}.get(path, {})
        route.fulfill(status=200, content_type='application/json', body=json.dumps(value))

    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/api/**', route)
    page.goto(origin + '/#onboarding')
    entry = page.get_by_role('region', name='家庭入口')
    expect(entry.get_by_role('link', name='進入家庭')).to_be_visible()
    expect(entry.get_by_text('開發助手尚未登入')).to_have_count(0)
    assert held
    released['ok'] = True
    for item in held:
        item.fulfill(status=200, content_type='application/json', body=json.dumps(workers_body()))
    entry.get_by_text('開發助手尚未登入', exact=True).wait_for()
    page.set_viewport_size({'width': 390, 'height': 844})
    in_view(page, entry.get_by_role('link', name='進入家庭'), 844)
    shot(page, 'completion')
    entry.get_by_role('button', name='登入開發助手', exact=True).click()
    page.get_by_role('button', name='登入 Codex 開發助手', exact=True).wait_for()
    assert page.url.endswith('#workers')
    in_view(page, page.get_by_role('button', name='登入 Codex 開發助手', exact=True), 844)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.evaluate("location.hash='#onboarding'")
    entry.get_by_role('button', name='略過', exact=True).click()
    expect(entry.get_by_text('開發助手尚未登入')).to_have_count(0)
    assert page.evaluate('({...localStorage})') == {'agentic-skip-coding-login': '1'}
    page.reload()
    entry.get_by_role('link', name='進入家庭').wait_for()
    expect(entry.get_by_text('開發助手尚未登入')).to_have_count(0)
    page.evaluate("localStorage.removeItem('agentic-skip-coding-login')")
    for credential in ('configured', 'inference_verified', 'connected'):
        coding['credential'] = credential
        with page.expect_response(lambda response: response.url.endswith('/api/workers') and response.status == 200):
            page.reload()
        entry.get_by_role('link', name='進入家庭').wait_for()
        expect(entry.get_by_text('開發助手尚未登入')).to_have_count(0)
    coding.update(credential='needs_auth', available=False)
    with page.expect_response(lambda response: response.url.endswith('/api/workers')):
        page.reload()
    entry.get_by_role('link', name='進入家庭').wait_for()
    expect(entry.get_by_text('開發助手尚未登入')).to_have_count(0)
    coding.update(available=True, fail=True)
    with page.expect_response(lambda response: response.url.endswith('/api/workers') and response.status == 500):
        page.reload()
    entry.get_by_role('link', name='進入家庭').wait_for()
    expect(entry.get_by_text('開發助手尚未登入')).to_have_count(0)
    coding.update(credential='needs_auth', available=True, fail=False)
    page.set_viewport_size({'width': 1440, 'height': 900})
    page.goto(origin + '/#settings')
    hub = page.get_by_role('navigation', name='設定入口')
    def hub_text(href):
        return hub.locator(f'a[href="{href}"]').inner_text().replace('\n', '').strip()
    expect(hub.locator('a[href="#workers"]')).to_contain_text('需要登入')
    assert hub_text('#workers') == '開發助手需要登入'
    assert hub_text('#gateway/channels') == '通訊'
    assert hub_text('#host') == '主機維護'
    coding['credential'] = 'configured'
    page.reload()
    expect(hub.locator('a[href="#workers"]')).to_contain_text('已就緒')
    assert hub_text('#workers') == '開發助手已就緒'
    coding['fail'] = True
    with page.expect_response(lambda response: response.url.endswith('/api/workers') and response.status == 500):
        page.reload()
    expect(hub.locator('a[href="#workers"]')).to_have_text('開發助手')
    expect(page.get_by_role('alert')).to_have_count(0)
    coding['fail'] = False
    gateway['on'] = True
    page.reload()
    expect(hub.get_by_role('link', name='通訊')).to_contain_text('LINE · 已連線')
    page.goto(origin + '/#host')
    page.get_by_role('heading', name='家庭系統', exact=True).wait_for()
    assert page.get_by_text('家庭系統', exact=True).count() == 1
    expect(page.get_by_text('家庭連線', exact=True)).to_be_visible()
    expect(page.get_by_text('可用空間', exact=True)).to_be_visible()
    expect(page.get_by_label('AI 帳號').locator('option')).to_have_text(['Claude Code', 'Grok Build'])
    shot(page, 'host')
    assert not errors
