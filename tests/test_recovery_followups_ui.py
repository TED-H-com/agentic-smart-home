"""Reboot recovery against synthetic local API state; no accounts or real pairing."""
import copy
import json
import os
from collections import Counter
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright
from test_recovery_ui import _server

ROOT = Path(__file__).parents[1]


@pytest.fixture
def recovery_browser(tmp_path, monkeypatch):
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    server, thread = _server()
    data = json.loads((ROOT / 'web/preview/fixtures.json').read_text())
    data['session'].pop('resume', None)
    data['hermes']['runtime'].update(provider='openai-codex', state='ready', model='fixture-model')
    data['worker/jobs'] = {'jobs': [], 'active_job_id': None}
    state = {'data': data, 'fail': {}, 'reads': Counter(), 'writes': [], 'handlers': {}, 'hold': {}, 'errors': []}

    def route(route):
        request = route.request
        path = request.url.split('/api/')[1].split('?')[0]
        if request.method == 'GET':
            state['reads'][path] += 1
            if path in state['hold']:
                state['hold'][path].append(route)
                return
        else:
            state['writes'].append((path, request.post_data_json, request.headers.get('idempotency-key')))
        if path in state['handlers']:
            state['handlers'][path](route)
        elif path in state['fail']:
            route.fulfill(status=state['fail'][path], json={'detail': '暫時讀不到資料'})
        elif request.method == 'GET' and path in data:
            route.fulfill(json=data[path])
        else:
            raise AssertionError(f'Unexpected {request.method} {path}')

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={'width': 1440, 'height': 900})
            page.clock.install()
            page.route('**/api/**', route)
            page.on('pageerror', lambda error: state['errors'].append(str(error)))
            yield page, f'http://127.0.0.1:{server.server_port}', state
            assert not state['errors']
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def tick(page, milliseconds=5100):
    page.clock.run_for(milliseconds)
    page.wait_for_timeout(50)


def shots(page, name):
    destination = os.environ.get('AGENTIC_RECOVERY_SCREENSHOTS')
    if not destination:
        return
    output = Path(destination)
    output.mkdir(parents=True, exist_ok=True)
    for width in (390, 768, 1440):
        page.set_viewport_size({'width': width, 'height': 900 if width == 1440 else 844})
        page.evaluate('window.scrollTo(0,0)')
        page.wait_for_timeout(100)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(output / f'{name}-{width}.png'), full_page=True)


def test_selected_cli_recovers_without_restarting_setup(recovery_browser):
    page, origin, state = recovery_browser
    state['data']['bootstrap'].update(state='waiting_input', provider='codex', current_step='cli_auth',
                                      human_action={'kind': 'cli_login', 'provider': 'codex'})
    state['fail']['runtimes'] = 503
    page.goto(origin + '/#onboarding')
    button = page.locator('.bootstrap-auth').get_by_role('button', name='開始登入', exact=True)
    expect(button).to_be_disabled()
    del state['fail']['runtimes']
    state['data']['runtimes']['host'] = {'status': 'unavailable', 'providers': []}
    tick(page)
    expect(button).to_be_disabled()
    state['data']['runtimes']['host'] = {'protocol': 1, 'providers': [{'provider': 'codex', 'status': 'needs_reauth'}]}
    tick(page)
    expect(button).to_be_enabled()
    expect(page.locator('.bootstrap-auth .notice.error')).to_have_count(0)
    assert state['reads']['runtimes'] >= 3
    assert not state['writes']


@pytest.mark.parametrize('kind', ['hermes', 'worker'])
def test_device_login_backs_off_then_recovers_same_handle(recovery_browser, kind):
    page, origin, state = recovery_browser
    auth_id = 'a' * 32
    if kind == 'hermes':
        auth = {'auth_id': auth_id, 'state': 'pending', 'user_code': 'FICTIONAL-CODE',
                'verification_uri': 'https://auth.openai.com/codex/device'}
        state['data']['hermes']['runtime'].update(state='needs_auth', pending_auth=auth)
        path, storage, view = f'hermes/auth/device/{auth_id}', 'agentic-auth-hermes-device', 'agents'
    else:
        auth = {'id': auth_id, 'status': 'pending', 'code': 'FICTIONAL-CODE',
                'url': 'https://auth.openai.com/codex/device'}
        worker = state['data']['workers']['workers'][0]
        worker['providers'][0].update(credential_state='needs_auth', auth_methods=['device_code'])
        path, storage, view = f'workers/host-coder/auth/{auth_id}', 'agentic-coding-auth-host-coder', 'workers'
        page.add_init_script(f'sessionStorage.setItem({json.dumps(storage)},{json.dumps(auth_id)})')
    state['data'][path] = auth
    page.goto(origin + '/#' + view)
    expect(page.get_by_text('FICTIONAL-CODE', exact=True)).to_be_visible()
    state['fail'][path] = 503
    for _ in range(3):
        tick(page, 2100)
    expect(page.get_by_text('暫時無法讀取登入狀態。', exact=True)).to_be_visible()
    before = state['reads'][path]
    tick(page, 5100)
    assert state['reads'][path] == before
    shots(page, kind + '-login-waiting')
    del state['fail'][path]
    auth.update({'state': 'configured'} if kind == 'hermes' else {'status': 'connected'})
    if kind == 'hermes':
        state['data']['hermes']['runtime'].update(state='configured', pending_auth=None)
    tick(page, 5100)
    expect(page.get_by_text('暫時無法讀取登入狀態。', exact=True)).to_have_count(0)
    expect(page.get_by_text('FICTIONAL-CODE', exact=True)).to_have_count(0)
    assert state['reads'][path] == before + 1
    assert page.evaluate('key => sessionStorage.getItem(key)', storage) is None
    terminal = state['reads'][path]
    tick(page, 30100)
    assert state['reads'][path] == terminal
    assert not state['writes']


@pytest.mark.parametrize('kind', ['hermes', 'runtime'])
def test_account_read_failure_hides_stale_ready_and_preserves_action_error(recovery_browser, kind):
    page, origin, state = recovery_browser
    page.goto(origin + '/#agents')
    panel = page.get_by_role('region', name='智能管家登入' if kind == 'hermes' else '開發助手帳號')
    heading = panel.locator(':scope > .section-heading')
    expect(heading).to_contain_text('已可對話' if kind == 'hermes' else '已連線')
    path = 'hermes' if kind == 'hermes' else 'runtimes'
    state['fail'][path] = 503
    tick(page, 10100)
    expect(heading).to_contain_text('等待')
    expect(heading).not_to_contain_text('已可對話' if kind == 'hermes' else '已連線')
    mutation = 'hermes/auth/logout' if kind == 'hermes' else 'runtimes/host/providers/codex/logout'
    state['handlers'][mutation] = lambda route: route.fulfill(status=409, json={'detail': '保留這個操作錯誤'})
    panel.get_by_role('button', name='登出', exact=True).first.click()
    expect(panel.get_by_text('保留這個操作錯誤', exact=True)).to_be_visible()
    del state['fail'][path]
    tick(page, 10100)
    expect(heading).to_contain_text('已可對話' if kind == 'hermes' else '已連線')
    expect(panel.get_by_text('暫時讀不到資料', exact=True)).to_have_count(0)
    expect(panel.get_by_text('保留這個操作錯誤', exact=True)).to_be_visible()
    assert [item[0] for item in state['writes']] == [mutation]


def test_bridge_read_failure_hides_stale_healthy_without_clearing_action_error(recovery_browser):
    page, origin, state = recovery_browser
    path = 'host/bridge/maintenance'
    state['handlers'][path + '/repair'] = lambda route: route.fulfill(status=409, json={'detail': '保留這個操作錯誤'})
    page.goto(origin + '/#settings')
    page.locator('summary').filter(has_text='智能管家側欄').click()
    panel = page.get_by_role('region', name='側欄維護')
    expect(panel.get_by_text('側欄服務可用', exact=True)).to_be_visible()
    panel.get_by_role('button', name='檢查側欄連線').click()
    expect(panel.get_by_text('保留這個操作錯誤', exact=True)).to_be_visible()
    state['fail'][path] = 503
    tick(page)
    expect(panel.get_by_text('等待側欄恢復連線…', exact=True)).to_be_visible()
    expect(panel.get_by_text('側欄服務可用', exact=True)).to_have_count(0)
    shots(page, 'bridge-waiting')
    del state['fail'][path]
    tick(page)
    expect(panel.get_by_text('側欄服務可用', exact=True)).to_be_visible()
    expect(panel.get_by_text('保留這個操作錯誤', exact=True)).to_be_visible()
    assert len(state['writes']) == 1


def test_manual_onboarding_read_retries_without_hiding_failed_action(recovery_browser):
    page, origin, state = recovery_browser
    state['fail']['gateway'] = 503
    state['data']['gateway']['step'] = 2
    state['handlers']['gateway/onboarding'] = lambda route: route.fulfill(status=409, json={'detail': '保留這個操作錯誤'})
    page.goto(origin + '/#onboarding')
    page.locator('summary').filter(has_text='其他設定方式').click()
    panel = page.locator('.bootstrap-manual')
    expect(panel.get_by_text('暫時讀不到資料', exact=True)).to_be_visible()
    panel.get_by_role('button', name='下一步', exact=True).click()
    expect(panel.get_by_text('保留這個操作錯誤', exact=True)).to_be_visible()
    del state['fail']['gateway']
    tick(page)
    expect(panel.get_by_text('暫時讀不到資料', exact=True)).to_have_count(0)
    expect(panel.get_by_role('button', name='3 通訊', exact=True)).to_have_attribute('aria-current', 'step')
    expect(panel.get_by_text('保留這個操作錯誤', exact=True)).to_be_visible()
    assert len(state['writes']) == 1


@pytest.mark.parametrize('view', ['inventory', 'automations'])
def test_initial_page_read_retries_while_home_remains_connected(recovery_browser, view):
    page, origin, state = recovery_browser
    state['fail'][view] = 503
    page.goto(origin + '/#' + view)
    expect(page.get_by_text('暫時讀不到資料', exact=True)).to_be_visible()
    del state['fail'][view]
    tick(page)
    expect(page.get_by_text('暫時讀不到資料', exact=True)).to_have_count(0)
    result = page.locator('.entity-row') if view == 'inventory' else page.locator('.automation-row')
    expect(result.get_by_text('客廳燈' if view == 'inventory' else '天黑後打開客廳燈', exact=True)).to_be_visible()
    assert state['data']['status']['ha']['status'] == 'connected'
    assert state['reads'][view] >= 2
    assert not state['writes']


def test_inventory_late_reload_preserves_unsaved_permissions(recovery_browser):
    page, origin, state = recovery_browser
    page.goto(origin + '/#inventory')
    permission = page.get_by_label('分享 light.demo_living_room', exact=True)
    expect(permission).not_to_be_checked()
    state['hold']['inventory'] = []
    page.get_by_role('button', name='重新盤點', exact=True).click()
    expect(page.get_by_role('button', name='盤點中…', exact=True)).to_be_visible()
    permission.check()
    held = state['hold'].pop('inventory')
    assert len(held) == 1
    held[0].fulfill(json=copy.deepcopy(state['data']['inventory']))
    tick(page, 10100)
    expect(permission).to_be_checked()
    expect(page.get_by_text('設備授權有未儲存的修改', exact=True)).to_be_visible()
    assert state['reads']['inventory'] == 2
    assert not state['writes']


@pytest.mark.parametrize('surface', ['host-settings', 'host-runtime', 'haos-runtime', 'ingress-settings'])
def test_unavailable_runner_repair_is_explicit(recovery_browser, surface):
    page, origin, state = recovery_browser
    unavailable = {'status': 'unavailable', 'providers': []}
    state['data']['status']['runner'] = unavailable
    state['data']['runtimes'] = {'host': unavailable, 'haos': unavailable}
    if surface == 'ingress-settings':
        state['data']['deployment']['mode'] = 'ingress'
    page.goto(origin + ('/#agents' if surface.endswith('runtime') else '/#settings'))
    if surface == 'host-settings':
        panel = page.locator('.settings-fold').filter(has=page.locator('summary', has_text='主機連線'))
        panel.locator(':scope > summary').click()
    elif surface.endswith('runtime'):
        panel = page.get_by_role('region', name='開發助手帳號')
        if surface == 'haos-runtime':
            panel.get_by_role('button', name='家庭現場', exact=False).click()
    else:
        panel = page.locator('.settings-section').filter(has=page.get_by_role('heading', name='執行環境', exact=True))
    tick(page, 10100)
    assert not state['writes']
    panel.locator('summary').filter(has_text='重新連接').click()
    assert not state['writes']
    if surface == 'haos-runtime':
        panel.locator('summary').filter(has_text='手動安裝與配對').click()
        panel.get_by_label('配對資料', exact=True).fill(json.dumps({'fingerprint': 'fixture', 'code': 'fixture-code'}))
        endpoint, button = 'runtimes/haos/pair', '配對家庭現場'
    elif surface == 'ingress-settings':
        panel.get_by_label('一次性配對資料', exact=True).fill(json.dumps({'host': 'fixture', 'port': 8443, 'fingerprint': 'fixture', 'code': 'fixture-code'}))
        endpoint, button = 'pair', '配對執行環境'
    else:
        endpoint, button = 'host/pair', '重新配對主機'
    if surface == 'host-settings':
        shots(page, 'explicit-repair')
    state['handlers'][endpoint] = lambda route: route.fulfill(status=409, json={'detail': '配對資料需要重新確認'})
    panel.get_by_role('button', name=button, exact=True).click()
    expect(page.get_by_text('配對資料需要重新確認', exact=True)).to_be_visible()
    tick(page, 10100)
    expect(page.get_by_text('配對資料需要重新確認', exact=True)).to_be_visible()
    assert [item[0] for item in state['writes']] == [endpoint]


def test_preview_retry_keeps_key_after_lost_reply(recovery_browser):
    page, origin, state = recovery_browser
    job_id, old_id, new_id = 'b' * 32, 'c' * 32, 'd' * 32
    job = copy.deepcopy(state['data']['worker/jobs/' + 'ab' * 16])
    job.update(id=job_id, artifacts=[{'id': 'e' * 32, 'name': 'index.html', 'kind': 'source', 'size': 10, 'sha256': 'f' * 64}])
    state['data']['worker/jobs'] = {'jobs': [job], 'active_job_id': None}
    state['data']['worker/jobs/' + job_id] = job
    state['data']['worker/previews/' + old_id] = {'id': old_id, 'state': 'expired'}
    page.add_init_script(f"sessionStorage.setItem('agentic-preview-{job_id}','{old_id}')")
    preview_path = f'worker/jobs/{job_id}/previews'

    def create(route):
        if len(state['writes']) == 1:
            route.abort('failed')
        else:
            route.fulfill(json={'id': new_id, 'state': 'ready', 'preview_urls': {}})

    state['handlers'][preview_path] = create
    state['handlers'][f'worker/previews/{new_id}/revoke'] = lambda route: route.fulfill(json={'id': new_id, 'state': 'expired'})
    page.goto(origin + '/#workers/' + job_id)
    button = page.get_by_role('button', name='產生畫面預覽', exact=True)
    expect(button).to_be_enabled()
    button.click()
    expect(page.get_by_text('尚未收到操作結果。請先查看任務或主機紀錄，確認狀態後再試。', exact=True)).to_be_visible()
    button.click()
    expect(page.get_by_role('button', name='關閉這份預覽', exact=True)).to_be_visible()
    first, second = state['writes']
    assert first == second and first[2]
    page.get_by_role('button', name='關閉這份預覽', exact=True).click()
    button.click()
    expect(page.get_by_role('button', name='關閉這份預覽', exact=True)).to_be_visible()
    assert state['writes'][-1][2] != first[2]


def resume_case(kind, state):
    auth_id = 'a' * 32
    if kind == 'hermes':
        state['data']['hermes']['runtime'].update(state='needs_auth', pending_auth=None)
        auth = {'auth_id': auth_id, 'state': 'pending', 'user_code': 'FICTIONAL-RESUME',
                'verification_uri': 'https://auth.openai.com/codex/device'}
        return f'hermes/auth/device/{auth_id}', 'agentic-auth-hermes-device', 'agents', auth
    state['data']['workers']['workers'][0]['providers'][0].update(credential_state='needs_auth', auth_methods=['device_code'])
    auth = {'id': auth_id, 'status': 'pending', 'code': 'FICTIONAL-RESUME',
            'url': 'https://auth.openai.com/codex/device'}
    return f'workers/host-coder/auth/{auth_id}', 'agentic-coding-auth-host-coder', 'workers', auth


@pytest.mark.parametrize('kind', ['hermes', 'worker'])
def test_reloaded_offline_login_keeps_and_resumes_saved_handle(recovery_browser, kind):
    page, origin, state = recovery_browser
    path, storage, view, auth = resume_case(kind, state)
    state['data'][path] = auth
    page.goto(origin + '/#' + view)
    page.evaluate('([key, id]) => sessionStorage.setItem(key, id)', [storage, 'a' * 32])
    state['fail'][path] = 503
    if kind == 'hermes':
        state['fail']['hermes'] = 503
    page.reload()
    expect(page.get_by_text('正在恢復登入狀態…', exact=True)).to_be_visible()
    for _ in range(3):
        tick(page, 2100)
    expect(page.get_by_text('暫時無法讀取登入狀態。', exact=True)).to_be_visible()
    assert page.evaluate('key => sessionStorage.getItem(key)', storage) == 'a' * 32
    before = state['reads'][path]
    tick(page, 5100)
    assert state['reads'][path] == before
    state['fail'].clear()
    tick(page, 5100)
    expect(page.get_by_text('FICTIONAL-RESUME', exact=True)).to_be_visible()
    expect(page.get_by_text('暫時無法讀取登入狀態。', exact=True)).to_have_count(0)
    assert page.evaluate('key => sessionStorage.getItem(key)', storage) == 'a' * 32
    assert not state['writes']


def test_hermes_runtime_recovery_adopts_pending_login_without_saved_handle(recovery_browser):
    page, origin, state = recovery_browser
    path, storage, _, auth = resume_case('hermes', state)
    state['data'][path] = auth
    state['data']['hermes']['runtime']['pending_auth'] = auth
    state['fail']['hermes'] = 503
    page.goto(origin + '/#agents')
    expect(page.get_by_text('暫時讀不到資料', exact=True)).to_be_visible()
    del state['fail']['hermes']
    tick(page, 10100)
    expect(page.get_by_text('FICTIONAL-RESUME', exact=True)).to_be_visible()
    assert page.evaluate('key => sessionStorage.getItem(key)', storage) == 'a' * 32
    tick(page, 2100)
    assert state['reads'][path] >= 1
    assert not state['writes']


@pytest.mark.parametrize('kind', ['hermes', 'worker'])
@pytest.mark.parametrize('outcome', ['missing', 'stop_unconfirmed'])
def test_restored_login_only_discards_confirmed_missing_handle(recovery_browser, kind, outcome):
    page, origin, state = recovery_browser
    path, storage, view, auth = resume_case(kind, state)
    if outcome == 'missing':
        state['fail'][path] = 404
    else:
        auth['state' if kind == 'hermes' else 'status'] = 'stop_unconfirmed'
        state['data'][path] = auth
    page.add_init_script(f'sessionStorage.setItem({json.dumps(storage)}, {json.dumps("a" * 32)})')
    page.goto(origin + '/#' + view)
    tick(page, 100)
    if outcome == 'missing':
        assert page.evaluate('key => sessionStorage.getItem(key)', storage) is None
        expect(page.get_by_text('官方授權已過期，請重新開始。' if kind == 'hermes' else '這次登入已過期，請重新登入。', exact=True)).to_be_visible()
    else:
        assert page.evaluate('key => sessionStorage.getItem(key)', storage) == 'a' * 32
        expect(page.get_by_text('尚未確認前一次登入已停止，請重新開始。' if kind == 'hermes' else '尚未確認登入程序停止，請先核對後再登入。', exact=True)).to_be_visible()
    count = state['reads'][path]
    tick(page, 30100)
    assert state['reads'][path] == count
    assert not state['writes']


@pytest.mark.parametrize('kind', ['hermes', 'worker'])
def test_late_saved_login_read_cannot_revive_after_logout(recovery_browser, kind):
    page, origin, state = recovery_browser
    path, storage, view, auth = resume_case(kind, state)
    if kind == 'hermes':
        state['data']['hermes']['runtime']['state'] = 'ready'
        endpoint = 'hermes/auth/logout'
    else:
        state['data']['workers']['workers'][0]['providers'][0]['credential_state'] = 'connected'
        endpoint = 'workers/host-coder/providers/codex/logout'
    state['hold'][path] = []
    page.add_init_script(f'sessionStorage.setItem({json.dumps(storage)}, {json.dumps("a" * 32)})')

    def logout(route):
        if kind == 'hermes':
            state['data']['hermes']['runtime']['state'] = 'needs_auth'
        else:
            state['data']['workers']['workers'][0]['providers'][0]['credential_state'] = 'needs_auth'
        route.fulfill(json={'ok': True})

    state['handlers'][endpoint] = logout
    page.goto(origin + '/#' + view)
    tick(page, 100)
    assert len(state['hold'][path]) >= 1
    panel = page.get_by_role('region', name='智能管家登入') if kind == 'hermes' else page.locator('.coding-signed')
    panel.get_by_role('button', name='登出', exact=True).click()
    page.wait_for_timeout(100)
    held = state['hold'].pop(path)
    for response in held:
        response.fulfill(json=auth)
    tick(page, 30100)
    expect(page.get_by_text('FICTIONAL-RESUME', exact=True)).to_have_count(0)
    assert page.evaluate('key => sessionStorage.getItem(key)', storage) is None
    assert state['reads'][path] == len(held)
    assert [write[0] for write in state['writes']] == [endpoint]


def test_late_hermes_runtime_read_cannot_adopt_login_after_logout(recovery_browser):
    page, origin, state = recovery_browser
    _, storage, _, auth = resume_case('hermes', state)
    state['data']['hermes']['runtime']['state'] = 'ready'
    page.goto(origin + '/#agents')
    panel = page.get_by_role('region', name='智能管家登入')
    expect(panel.get_by_role('button', name='登出', exact=True)).to_be_visible()
    stale = copy.deepcopy(state['data']['hermes'])
    stale['runtime']['pending_auth'] = auth
    state['hold']['hermes'] = []
    tick(page, 10100)
    held = state['hold'].pop('hermes')
    assert len(held) == 1

    def logout(route):
        state['data']['hermes']['runtime']['state'] = 'needs_auth'
        route.fulfill(json={'ok': True})

    state['handlers']['hermes/auth/logout'] = logout
    panel.get_by_role('button', name='登出', exact=True).click()
    expect(panel.get_by_text('已登出。', exact=True)).to_be_visible()
    held[0].fulfill(json=stale)
    tick(page, 20100)
    expect(page.get_by_text('FICTIONAL-RESUME', exact=True)).to_have_count(0)
    assert page.evaluate('key => sessionStorage.getItem(key)', storage) is None
    assert [write[0] for write in state['writes']] == ['hermes/auth/logout']


def provider_resume_case(state):
    auth = {'id': 'a' * 32, 'status': 'pending', 'code': 'FICTIONAL-RESUME',
            'url': 'https://auth.openai.com/codex/device'}
    provider = state['data']['runtimes']['host']['providers'][0]
    provider.update(status='needs_reauth', pending_session=None)
    return provider, auth, 'runtimes/host/auth/' + 'a' * 32, 'agentic-auth-host-codex'


def test_provider_reload_keeps_handle_and_backs_off_without_new_login(recovery_browser):
    page, origin, state = recovery_browser
    _, auth, path, storage = provider_resume_case(state)
    state['data'][path] = auth
    page.goto(origin + '/#agents')
    panel = page.locator('.provider').filter(has=page.get_by_role('heading', name='Codex', exact=True))
    expect(panel).to_be_visible()
    page.evaluate('([key, id]) => sessionStorage.setItem(key, id)', [storage, auth['id']])
    state['fail'][path] = 503
    page.reload()
    expect(panel.get_by_text('正在恢復登入狀態…', exact=True)).to_be_visible()
    for _ in range(3):
        tick(page, 3100)
    expect(panel.get_by_text('暫時讀不到資料', exact=True)).to_be_visible()
    assert page.evaluate('key => sessionStorage.getItem(key)', storage) == auth['id']
    before = state['reads'][path]
    tick(page, 5100)
    assert state['reads'][path] == before
    del state['fail'][path]
    tick(page, 5100)
    expect(panel.get_by_text('FICTIONAL-RESUME', exact=True)).to_be_visible()
    expect(panel.get_by_text('暫時讀不到資料', exact=True)).to_have_count(0)
    assert not state['writes']


@pytest.mark.parametrize('action', ['logout', 'cancel'])
def test_provider_late_read_and_stale_runtime_cannot_revive_cleared_login(recovery_browser, action):
    page, origin, state = recovery_browser
    provider, auth, path, storage = provider_resume_case(state)
    page.add_init_script(f'sessionStorage.setItem({json.dumps(storage)}, {json.dumps(auth["id"])})')
    if action == 'logout':
        provider['status'] = 'connected'
        state['hold'][path] = []
    else:
        state['data'][path] = auth
    page.goto(origin + '/#agents')
    panel = page.locator('.provider').filter(has=page.get_by_role('heading', name='Codex', exact=True))
    if action == 'cancel':
        expect(panel.get_by_text('FICTIONAL-RESUME', exact=True)).to_be_visible()
        state['hold'][path] = []
        tick(page, 2100)
    else:
        expect(panel.get_by_role('button', name='登出', exact=True)).to_be_visible()
        tick(page, 100)
    held = state['hold'].pop(path)
    assert held
    endpoint = f'runtimes/host/providers/codex/auth/{auth["id"]}/cancel' if action == 'cancel' else 'runtimes/host/providers/codex/logout'

    def clear(route):
        provider.update(status='needs_reauth', pending_session=auth)
        route.fulfill(json={'id': auth['id'], 'status': 'cancelled'})

    state['handlers'][endpoint] = clear
    panel.get_by_role('button', name='取消登入' if action == 'cancel' else '登出', exact=True).click()
    page.wait_for_timeout(100)
    before = state['reads'][path]
    for response in held:
        response.fulfill(json=auth)
    tick(page, 30100)
    expect(panel.get_by_text('FICTIONAL-RESUME', exact=True)).to_have_count(0)
    assert page.evaluate('key => sessionStorage.getItem(key)', storage) is None
    assert state['reads'][path] == before
    assert [write[0] for write in state['writes']] == [endpoint]


def test_provider_poll_recovery_preserves_cancel_action_error(recovery_browser):
    page, origin, state = recovery_browser
    _, auth, path, storage = provider_resume_case(state)
    state['data'][path] = auth
    page.add_init_script(f'sessionStorage.setItem({json.dumps(storage)}, {json.dumps(auth["id"])})')
    page.goto(origin + '/#agents')
    panel = page.locator('.provider').filter(has=page.get_by_role('heading', name='Codex', exact=True))
    expect(panel.get_by_text('FICTIONAL-RESUME', exact=True)).to_be_visible()
    endpoint = f'runtimes/host/providers/codex/auth/{auth["id"]}/cancel'
    state['handlers'][endpoint] = lambda route: route.fulfill(status=409, json={'detail': '保留這個操作錯誤'})
    panel.get_by_role('button', name='取消登入', exact=True).click()
    expect(panel.get_by_text('保留這個操作錯誤', exact=True)).to_be_visible()
    state['fail'][path] = 503
    tick(page, 3100)
    expect(panel.get_by_text('暫時讀不到資料', exact=True)).to_be_visible()
    del state['fail'][path]
    tick(page, 3100)
    expect(panel.get_by_text('暫時讀不到資料', exact=True)).to_have_count(0)
    expect(panel.get_by_text('保留這個操作錯誤', exact=True)).to_be_visible()
    assert [write[0] for write in state['writes']] == [endpoint]


@pytest.mark.parametrize('kind', ['hermes', 'provider'])
def test_confirmed_missing_saved_handle_adopts_different_current_login(recovery_browser, kind):
    page, origin, state = recovery_browser
    if kind == 'hermes':
        old_path, storage, _, auth = resume_case(kind, state)
        auth['auth_id'] = 'b' * 32
        state['data']['hermes']['runtime']['pending_auth'] = auth
        new_path = 'hermes/auth/device/' + 'b' * 32
    else:
        provider, auth, old_path, storage = provider_resume_case(state)
        auth['id'] = 'b' * 32
        provider['pending_session'] = auth
        new_path = 'runtimes/host/auth/' + 'b' * 32
    state['data'][new_path] = auth
    state['fail'][old_path] = 404
    page.add_init_script(f'sessionStorage.setItem({json.dumps(storage)}, {json.dumps("a" * 32)})')
    page.goto(origin + '/#agents')
    page.wait_for_function('([key, old]) => sessionStorage.getItem(key) !== old', arg=[storage, 'a' * 32])
    tick(page, 10100)
    expect(page.get_by_text('FICTIONAL-RESUME', exact=True)).to_be_visible()
    assert page.evaluate('key => sessionStorage.getItem(key)', storage) == 'b' * 32
    assert state['reads'][old_path] == 1
    tick(page, 3100)
    assert state['reads'][new_path] >= 1
    assert not state['writes']



def test_inventory_save_reply_keeps_newer_unsaved_permission_edits(recovery_browser):
    page, origin, state = recovery_browser
    page.goto(origin + '/#inventory')
    permission = page.get_by_label('分享 light.demo_living_room', exact=True)
    expect(permission).not_to_be_checked()
    state['fail']['inventory'] = 503
    page.get_by_role('button', name='重新盤點', exact=True).click()
    expect(page.get_by_text('暫時讀不到資料', exact=True)).to_be_visible()
    permission.check()
    pending = []
    state['handlers']['policy'] = pending.append
    page.get_by_role('button', name='儲存修改', exact=True).click()
    expect(page.get_by_role('button', name='儲存中…', exact=True)).to_be_visible()
    assert len(pending) == 1
    posted = state['writes'][0][1]
    assert next(item for item in posted['entities'] if item['entity_id'] == 'light.demo_living_room')['read'] is True
    permission.uncheck()
    state['data']['inventory']['policy']['entities'] = posted['entities']
    del state['fail']['inventory']
    reads = state['reads']['inventory']
    pending[0].fulfill(json={'ok': True})
    tick(page, 10100)
    expect(permission).not_to_be_checked()
    expect(page.get_by_text('設備授權有未儲存的修改', exact=True)).to_be_visible()
    expect(page.get_by_text('已儲存。受影響的提案需要重新產生。', exact=True)).to_have_count(0)
    assert state['reads']['inventory'] == reads
    state['handlers']['policy'] = lambda route: route.fulfill(json={'ok': True})
    page.get_by_role('button', name='儲存修改', exact=True).click()
    expect(page.get_by_text('設備授權有未儲存的修改', exact=True)).to_have_count(0)
    assert next(item for item in state['writes'][1][1]['entities'] if item['entity_id'] == 'light.demo_living_room')['read'] is False
