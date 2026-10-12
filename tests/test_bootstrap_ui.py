"""Guided setup UI against protocol fixtures; no real OAuth/HA acceptance claim."""
import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

STEPS = ['network', 'runner_pairing', 'cli_auth', 'cli_assist', 'hermes_install',
         'hermes_auth', 'channel', 'ha_provision', 'ha_native']


def test_cli_first_bootstrap_resumes_and_waits_for_real_backend_states():
    root = Path(__file__).parents[1]
    server = ThreadingHTTPServer(('127.0.0.1', 0),
        partial(SimpleHTTPRequestHandler, directory=str(root / 'controller/static')))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    flow = {'contract': 1, 'id': None, 'state': 'idle', 'provider': None, 'channel': 'line',
            'current_step': None, 'steps': [{'id': step, 'state': 'pending', 'code': None,
                                          'verified_at': None} for step in STEPS],
            'human_action': None, 'updated_at': None}
    gateway = {'step': 0, 'selected': False, 'web': {'status': 'connected'},
               'preferences': {'primary': 'line', 'runtime': 'host', 'provider': 'codex', 'coordinator': 'hermes'},
               'channels': [{'id': name, 'status': 'disabled', 'enabled': False, 'chats': [],
                             'senders': [], 'has_secret': False} for name in ['line', 'telegram', 'discord']]}
    runtime = {'state': 'needs_auth', 'provider': 'openai-codex', 'model': '', 'auth_verified_at': None,
               'default_models': {'openai-codex': 'gpt-6-astra'}}
    cli_auth = {'id': 'cli-fixture-login', 'status': 'pending',
                'url': 'https://accounts.x.ai/device', 'code': 'CLI-FIXTURE'}
    device = {'auth_id': 'hermes-fixture-login', 'provider': 'openai-codex', 'model': 'gpt-6-astra',
              'state': 'pending', 'verification_uri': 'https://auth.openai.com/codex/device',
              'user_code': 'HERMES-FIXTURE', 'expires_at': '2026-10-12T09:00:00Z', 'error': None}
    line = {'status': 'idle', 'channels': []}
    pairing = {channel: {'state': 'idle', 'code': None, 'expires_at': None, 'chat': None, 'sender': None}
               for channel in ['line', 'telegram']}
    authorization = {'state': 'idle', 'error_code': None}
    failure = {'read': False, 'start_reply': True, 'authorize_url': True}
    calls, errors, urls = [], [], []

    def stage(step, state='running', action=None):
        index = STEPS.index(step)
        flow.update(id='bootstrap-fixture', state=state, current_step=step, human_action=action,
                    updated_at='2026-10-12T08:00:00Z')
        for number, item in enumerate(flow['steps']):
            item.update(state='completed' if number < index else state if number == index else 'pending',
                        verified_at='2026-10-12T08:00:00Z' if number < index else None)
            if item['id'] == 'channel' and flow['channel'] == 'web' and number < index:
                item['state'] = 'skipped'

    def route(route):
        request = route.request
        path = request.url.split('/api/')[1]
        urls.append(request.url)
        body = request.post_data_json if request.method == 'POST' else None
        if body is not None:
            assert request.headers.get('x-agentic-csrf') == 'fixture-csrf'
            assert request.headers.get('idempotency-key')
            calls.append((path, body, request.headers['idempotency-key']))
        if path == 'deployment':
            result = {'mode': 'host', 'authenticated': True}
        elif path == 'host/home-entry':
            result = {'contract_version': 1, 'state': 'ready' if flow['state'] == 'completed' else 'setup', 'native_path': '/', 'reason_code': None}
        elif path == 'session':
            result = {'principal': 'host-owner', 'csrf': 'fixture-csrf'}
        elif path == 'status':
            result = {'ha': {'status': 'unavailable'}, 'runner': {'protocol': 1, 'providers': []},
                      'gateway': gateway, 'version': 'fixture', 'paused': False}
        elif path == 'tasks':
            result = {'tasks': []}
        elif path == 'bootstrap':
            if failure['read']:
                route.fulfill(status=503, content_type='application/json', body='{"detail":"fixture disconnected"}')
                return
            result = flow
        elif path == 'bootstrap/start':
            assert body == {'provider': 'grok', 'channel': 'line'}
            flow['provider'] = body['provider']
            stage('cli_auth', 'waiting_input', {'kind': 'cli_login', 'provider': 'grok',
                                               'message': '請完成選定 CLI 的官方授權。'})
            if failure['start_reply']:
                failure['start_reply'] = False
                route.abort('failed')
                return
            result = flow
        elif path == 'bootstrap/retry':
            assert body == {}
            stage('hermes_install')
            result = flow
        elif path == 'bootstrap/preferences':
            assert set(body) == {'channel'}
            if flow['channel'] in pairing and pairing[flow['channel']]['state'] == 'pending':
                pairing[flow['channel']].update(state='cancelled', code=None)
            flow['channel'] = body['channel']
            if body['channel'] == 'web':
                stage('ha_provision')
                flow['steps'][6]['state'] = 'skipped'
            result = flow
        elif path == 'runtimes':
            result = {'host': {'protocol': 1, 'providers': [{'provider': 'grok', 'status': cli_auth['status']}]}}
        elif path == 'runtimes/host/providers/grok/auth':
            assert body == {'method': 'device_code', 'secret': ''}
            result = cli_auth
        elif path == 'runtimes/host/auth/cli-fixture-login':
            result = cli_auth
        elif path == 'hermes':
            result = {'runtime': runtime, 'workers': [{'worker_id': 'host-coder', 'available': False},
                                                     {'worker_id': 'home-coder', 'available': False}]}
        elif path == 'hermes/auth/device':
            assert body == {'provider': 'openai-codex', 'model': 'gpt-6-astra'}
            result = device
        elif path == 'hermes/auth/device/hermes-fixture-login':
            result = device
        elif path == 'gateway':
            result = gateway
        elif path == 'gateway/line/login':
            if body is not None:
                line.update(status='authenticating', qr='https://fixture.example/qr', pin='123456')
            result = line
        elif path == 'gateway/preferences':
            gateway.update(preferences=body, selected=True)
            result = gateway
        elif path.startswith('gateway/channels/') and '/pairing' in path:
            channel = path.split('/')[2]
            if body is not None:
                if path.endswith('/cancel'):
                    assert body == {}
                    pairing[channel].update(state='cancelled', code=None)
                else:
                    config = next(item for item in gateway['channels'] if item['id'] == channel)
                    assert body == ({'chat': 'c11111111111111111111111111111111'} if channel == 'line' else
                                    {'secret': '' if config['has_secret'] else '123456:fixture-private-telegram-token'})
                    config['has_secret'] = True
                    pairing[channel].update(state='pending', code='PAIR-FIXTURE', expires_at=1791806580,
                                            chat=body.get('chat'))
            result = pairing[channel]
        elif path == 'host/authorize':
            result = {'url': 'https://unexpected.example/auth/authorize' if failure['authorize_url'] else
                      'http://127.0.0.1:8123/auth/authorize?state=fixture-state'}
        elif path == 'host/authorization':
            result = authorization
        elif path == 'host/connect':
            assert body == {'token': 'fixture-private-home-token'}
            result = {'connected': True}
        else:
            raise AssertionError('Unexpected API: ' + path)
        route.fulfill(status=200, content_type='application/json', body=json.dumps(result))

    def shots(page, name):
        if name == 'complete':
            primary = page.get_by_role('region', name='家庭入口').get_by_role('link', name='進入家庭')
            expect(primary).to_have_css('background-color', 'rgb(33, 100, 83)')
            expect(primary).to_have_css('color', 'rgb(255, 254, 250)')
            expect(primary).to_have_css('opacity', '1')
        for width in [1440, 768, 390]:
            page.set_viewport_size({'width': width, 'height': 900 if width == 1440 else 844})
            page.evaluate('window.scrollTo(0,0)')
            page.wait_for_timeout(100)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(root / f'evidence/ui/native-home/bootstrap-{name}-{width}.png'), full_page=True)

    def stored(page):
        return page.evaluate('JSON.stringify({session:{...sessionStorage},local:{...localStorage}})')

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 900})
            page.on('pageerror', lambda error, errors=errors: errors.append(str(error)))
            page.route('**/api/**', route)
            page.goto(f'http://127.0.0.1:{server.server_port}/#onboarding')
            page.get_by_role('heading', name='先選一個 AI 助手', exact=True).wait_for()
            assert page.locator('article.provider').count() == 0
            expect(page.locator('.onboarding-body').get_by_role('heading')).to_have_count(1)
            expect(page.locator('.topbar .ha-status')).to_have_count(0)
            expect(page.get_by_role('list', name='自動設定進度').locator('li')).to_have_count(5)
            expect(page.get_by_label('使用的助手')).to_have_value('codex')
            page.get_by_label('使用的助手').select_option('claude')
            expect(page.get_by_text('Claude Code 目前使用官方 setup-token', exact=False)).to_be_visible()
            page.get_by_label('使用的助手').select_option('agy')
            expect(page.get_by_text('Google 訂閱登入尚未整合。', exact=False)).to_be_visible()
            page.get_by_label('使用的助手').select_option('grok')
            shots(page, 'choose')
            start = page.locator('.onboarding-body').get_by_role('button', name='開始設定', exact=True)
            assert start.bounding_box()['y'] + start.bounding_box()['height'] <= page.viewport_size['height']
            expect(page.get_by_role('list', name='自動設定進度')).to_contain_text('網路與主機')
            expect(page.get_by_role('button', name='選單', exact=True)).to_have_attribute('aria-expanded', 'false')
            page.locator('.onboarding-body').get_by_role('button', name='開始設定', exact=True).click()
            # Server accepted the start but the response was lost: GET resumes the persisted flow.
            page.get_by_role('button', name='開始官方登入', exact=True).wait_for()
            assert len([call for call in calls if call[0] == 'bootstrap/start']) == 1
            assert page.locator('article.provider').count() == 1
            expect(page.locator('article.provider').get_by_role('heading')).to_have_text('Grok Build')
            page.get_by_role('button', name='開始官方登入', exact=True).click()
            page.get_by_text('CLI-FIXTURE', exact=True).wait_for()
            page.context.grant_permissions(['clipboard-read', 'clipboard-write'])
            page.get_by_role('button', name='複製授權碼', exact=True).click()
            assert page.evaluate('navigator.clipboard.readText()') == 'CLI-FIXTURE'
            expect(page.get_by_label('登入方式', exact=True)).not_to_be_visible()
            expect(page.get_by_role('link', name='開啟官方授權頁')).to_have_attribute('href', 'https://accounts.x.ai/device')
            page.reload()
            page.get_by_text('CLI-FIXTURE', exact=True).wait_for()
            assert 'CLI-FIXTURE' not in stored(page) and 'https://accounts.x.ai' not in stored(page)
            assert page.get_by_role('heading', name='設定完成').count() == 0
            shots(page, 'cli-auth')
            cli_auth['status'] = 'connected'
            stage('cli_assist')
            page.get_by_text('助手正在確認設定…', exact=True).wait_for()
            stage('hermes_install')
            page.get_by_text('正從官方來源安裝 Hermes', exact=False).wait_for()
            shots(page, 'install')
            stage('hermes_install', 'blocked', {'kind': 'retry', 'message': '官方候選相容性檢查未通過。'})
            page.get_by_role('button', name='重試這一步', exact=True).wait_for()
            expect(page.get_by_text('官方候選相容性檢查未通過。', exact=True)).to_be_visible()
            page.get_by_role('button', name='重試這一步', exact=True).click()
            page.get_by_text('正從官方來源安裝 Hermes', exact=False).wait_for()
            stage('hermes_auth', 'waiting_input', {'kind': 'hermes_login'})
            oauth = page.get_by_role('region', name='Hermes 協調服務')
            oauth.get_by_role('button', name='開始 Hermes 官方登入', exact=True).wait_for()
            expect(oauth.get_by_label('Hermes 模型 ID')).to_have_value('gpt-6-astra')
            oauth.get_by_label('Hermes 登入方式').select_option('openrouter')
            expect(oauth.get_by_text('系統會呼叫模型一次來驗證連線，供應商可能計費。', exact=False)).to_be_visible()
            oauth.get_by_label('Hermes 登入方式').select_option('openai-codex')
            expect(oauth.get_by_label('Hermes 模型 ID')).to_have_value('gpt-6-astra')
            oauth.get_by_role('button', name='開始 Hermes 官方登入', exact=True).click()
            page.get_by_text('HERMES-FIXTURE', exact=True).wait_for()
            shots(page, 'hermes-auth')
            page.reload()
            page.get_by_text('HERMES-FIXTURE', exact=True).wait_for()
            assert 'HERMES-FIXTURE' not in stored(page)
            device['verification_uri'] = 'https://unexpected.example/steal'
            expect(page.get_by_role('link', name='開啟 Hermes 官方授權頁')).to_have_count(0)
            device['state'] = 'expired'
            page.get_by_role('alert').filter(has_text='官方授權已過期').wait_for()
            expect(oauth.get_by_role('button', name='開始 Hermes 官方登入', exact=True)).to_be_enabled()
            device.update(state='pending', verification_uri='https://auth.openai.com/codex/device')
            oauth.get_by_role('button', name='開始 Hermes 官方登入', exact=True).click()
            page.get_by_text('HERMES-FIXTURE', exact=True).wait_for()
            runtime.update(state='configured', model='gpt-6-astra')
            device['state'] = 'configured'
            expect(oauth.locator('.section-heading').get_by_role('status')).to_have_text('已儲存，待實際回覆驗證')
            assert page.get_by_role('heading', name='選擇你的通訊入口').count() == 0
            runtime.update(state='ready', auth_verified_at='2026-10-12T08:01:00Z')
            stage('channel', 'waiting_input', {'kind': 'channel_setup', 'channel': 'line'})
            page.get_by_role('heading', name='選擇你的通訊入口', exact=True).wait_for()
            expect(page.get_by_role('button', name='LINE 預設推薦', exact=True)).to_have_attribute('aria-pressed', 'true')
            assert page.get_by_role('button', name='Discord 自己的 Discord bot', exact=True).count() == 0
            page.get_by_role('button', name='開始 LINE 掃碼登入').click()
            page.get_by_alt_text('用 LINE 掃描此登入 QR code').wait_for()
            page.get_by_text('123456', exact=True).wait_for()
            shots(page, 'line')
            line.update(status='ready', qr=None, pin=None,
                        channels=[{'channelId': 'c11111111111111111111111111111111', 'name': '家庭測試聊天室'}])
            page.get_by_label('家庭聊天室', exact=True).select_option('c11111111111111111111111111111111')
            expect(page.get_by_label('允許的使用者 ID')).not_to_be_visible()
            page.get_by_role('button', name='產生通訊配對碼', exact=True).click()
            page.get_by_label('通訊配對指令').wait_for()
            expect(page.get_by_label('通訊配對指令')).to_have_text('/pair PAIR-FIXTURE')
            page.get_by_role('button', name='複製配對指令', exact=True).click()
            assert page.evaluate('navigator.clipboard.readText()') == '/pair PAIR-FIXTURE'
            assert 'PAIR-FIXTURE' not in stored(page)
            page.reload()
            page.get_by_label('通訊配對指令').wait_for()
            assert len([call for call in calls if call[0] == 'gateway/channels/line/pairing']) == 1
            expect(page.get_by_label('家庭聊天室', exact=True)).to_have_value('c11111111111111111111111111111111')
            expect(page.get_by_label('家庭聊天室', exact=True)).to_be_disabled()
            shots(page, 'line-pairing')
            pairing['line'].update(state='expired', code=None)
            page.get_by_role('alert').filter(has_text='通訊配對碼已過期').wait_for()
            page.get_by_label('家庭聊天室', exact=True).select_option('c11111111111111111111111111111111')
            page.get_by_role('button', name='產生通訊配對碼', exact=True).click()
            page.get_by_role('button', name='取消這次配對', exact=True).click()
            page.get_by_role('button', name='產生通訊配對碼', exact=True).wait_for()
            page.get_by_role('button', name='Telegram 自己的 Telegram bot').click()
            page.get_by_label('Telegram bot token').fill('123456:fixture-private-telegram-token')
            expect(page.get_by_label('允許的聊天室 ID')).not_to_be_visible()
            page.get_by_role('button', name='產生通訊配對碼', exact=True).click()
            page.get_by_label('通訊配對指令').wait_for()
            expect(page.get_by_label('Telegram bot token')).to_have_value('')
            assert '123456:fixture-private-telegram-token' not in stored(page)
            page.get_by_role('button', name='取消這次配對', exact=True).click()
            expect(page.get_by_role('button', name='產生通訊配對碼', exact=True)).to_be_enabled()
            page.get_by_role('button', name='產生通訊配對碼', exact=True).click()
            page.get_by_label('通訊配對指令').wait_for()
            pairing['telegram'].update(state='paired', code=None, chat='123', sender='456')
            page.get_by_text('帳號已配對。正在確認通訊連線', exact=False).wait_for()
            # Paired audience alone must not claim the external channel is connected.
            expect(page.get_by_role('heading', name='選擇你的通訊入口', exact=True)).to_be_visible()
            page.get_by_role('button', name='Web 先用 Web，稍後連接通訊', exact=True).click()
            page.get_by_text('正在建立家庭環境', exact=False).wait_for()
            assert calls[-1][0:2] == ('bootstrap/preferences', {'channel': 'web'})
            failure['read'] = True
            page.get_by_role('alert').filter(has_text='fixture disconnected').wait_for()
            assert page.get_by_role('heading', name='設定完成').count() == 0
            shots(page, 'disconnected')
            failure['read'] = False
            stage('ha_native', 'waiting_input', {'kind': 'ha_native_onboarding'})
            page.get_by_role('button', name='重試連線', exact=True).click()
            page.get_by_role('heading', name='設定你的家庭', exact=True).wait_for()
            expect(page.get_by_role('link', name='開啟 Home Assistant 設定')).to_have_attribute('href', 'http://127.0.0.1:8123/')
            expect(page.get_by_text('授權有效一年，可隨時在 Home Assistant 撤銷。', exact=False)).to_be_visible()
            shots(page, 'native-home')
            page.get_by_role('button', name='授權連接家庭', exact=True).click()
            page.get_by_role('alert').filter(has_text='家庭授權位置不正確').wait_for()
            assert '/#onboarding' in page.url
            failure['authorize_url'] = False
            page.route('http://127.0.0.1:8123/auth/authorize?**', lambda route: route.fulfill(body='<h1>Native HA fixture</h1>'))
            page.get_by_role('button', name='授權連接家庭', exact=True).click()
            page.wait_for_url('http://127.0.0.1:8123/auth/authorize?state=fixture-state')
            authorization.update(state='failed', error_code='ha_owner_required')
            page.go_back()
            page.get_by_role('heading', name='設定你的家庭', exact=True).wait_for()
            page.get_by_role('alert').filter(has_text='家庭授權尚未完成').wait_for()
            authorization.update(state='expired', error_code=None)
            page.get_by_role('alert').filter(has_text='家庭授權已過期').wait_for()
            authorization.update(state='connected', error_code=None)
            page.get_by_text('已授權，正在確認連線…', exact=True).wait_for()
            flow['human_action']['code'] = 'ha_owner_required'
            page.get_by_role('heading', name='連接你的家庭', exact=True).wait_for()
            shots(page, 'native-owner')
            page.get_by_text('進階：使用 HA owner 權杖', exact=True).click()
            page.get_by_label('HA owner 權杖').fill('fixture-private-home-token')
            page.get_by_label('HA owner 權杖').press('Enter')
            expect(page.get_by_label('HA owner 權杖')).to_have_value('')
            assert 'fixture-private-home-token' not in stored(page)
            assert page.get_by_role('heading', name='設定完成').count() == 0
            flow.update(state='completed', current_step=None, human_action=None)
            for step in flow['steps']:
                step['state'] = 'skipped' if step['id'] == 'channel' else 'completed'
            page.get_by_role('heading', name='設定完成', exact=True).wait_for()
            shots(page, 'complete')
            expect(page.get_by_role('link', name='進入家庭')).to_have_attribute('href', 'http://127.0.0.1:8123/')
            expect(page.get_by_role('region', name='家庭入口').get_by_role('button', name='智能管家', exact=True)).to_be_visible()
            expect(page.get_by_role('list', name='自動設定進度').get_by_text('已略過', exact=True)).to_be_visible()
            assert all('fixture-private-' not in url for url in urls)
            assert all(not any(key in body for key in ['step', 'completed', 'succeeded']) for _, body, _ in calls)
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_desktop_ticket_is_consumed_once_and_never_persisted():
    """Protocol fixture: desktop unlock succeeds or offers a safe manual fallback."""
    root = Path(__file__).parents[1]
    server = ThreadingHTTPServer(('127.0.0.1', 0),
        partial(SimpleHTTPRequestHandler, directory=str(root / 'controller/static')))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            for scenario in ['valid', 'rejected', 'malformed']:
                page = browser.new_page(viewport={'width': 390, 'height': 844})
                ticket = 'FixtureDesktopTicket' + 'A' * 23
                assert len(ticket) == 43
                fragment = ticket if scenario != 'malformed' else ticket + '-invalid'
                calls, errors = [], []
                authenticated = False
                page.on('pageerror', lambda error, errors=errors: errors.append(str(error)))

                def route(route, _request, *, calls=calls, ticket=ticket, page=page, scenario=scenario):
                    nonlocal authenticated
                    path = route.request.url.split('/api/')[1]
                    calls.append(path)
                    assert ticket not in route.request.url
                    assert ticket not in page.url
                    if path == 'host/desktop-login':
                        assert calls == ['host/desktop-login']
                        assert route.request.post_data_json == {'ticket': ticket}
                        assert scenario != 'malformed'
                        if scenario == 'rejected':
                            route.fulfill(status=401, content_type='application/json',
                                          body='{"detail":"桌面登入無效或已過期"}')
                            return
                        authenticated = True
                        result = {'authenticated': True}
                    elif path == 'deployment':
                        result = {'mode': 'host', 'authenticated': authenticated}
                    elif path == 'host/home-entry':
                        result = {'contract_version': 1, 'state': 'setup', 'native_path': '/', 'reason_code': 'bootstrap_incomplete'}
                    elif path == 'session':
                        assert authenticated
                        result = {'principal': 'host-owner', 'csrf': 'fixture-csrf'}
                    elif path == 'status':
                        result = {'ha': {'status': 'unavailable'}, 'version': 'fixture', 'paused': False}
                    elif path == 'tasks':
                        result = {'tasks': []}
                    elif path == 'bootstrap':
                        result = {'contract': 1, 'id': None, 'state': 'idle', 'provider': None,
                                  'channel': 'line', 'current_step': None, 'human_action': None,
                                  'steps': [{'id': step, 'state': 'pending'} for step in STEPS]}
                    else:
                        raise AssertionError('Unexpected desktop startup API: ' + path)
                    route.fulfill(status=200, content_type='application/json', body=json.dumps(result))

                page.route('**/api/**', route)
                page.goto(f'http://127.0.0.1:{server.server_port}/#desktop-login={fragment}')
                if scenario == 'valid':
                    page.get_by_role('heading', name='先選一個 AI 助手', exact=True).wait_for()
                    assert calls[0] == 'host/desktop-login'
                    expect(page.get_by_label('主機登入金鑰')).to_have_count(0)
                else:
                    page.get_by_role('alert').filter(has_text='桌面登入已過期或未完成').wait_for()
                    expect(page.get_by_label('主機登入金鑰')).to_be_visible()
                    assert not any(path in calls for path in ['status', 'tasks', 'bootstrap', 'session'])
                    if scenario == 'rejected':
                        for width in [1440, 768, 390]:
                            page.set_viewport_size({'width': width, 'height': 900 if width == 1440 else 844})
                            page.wait_for_timeout(100)
                            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                            page.screenshot(path=str(root / f'evidence/ui/native-home/bootstrap-desktop-retry-{width}.png'), full_page=True)
                assert page.url.endswith('/#onboarding')
                assert ticket not in page.locator('body').inner_text()
                assert ticket not in page.evaluate('JSON.stringify({session:{...sessionStorage},local:{...localStorage}})')
                first_count = calls.count('host/desktop-login')
                assert first_count == (0 if scenario == 'malformed' else 1)
                page.reload()
                if scenario == 'valid':
                    page.get_by_role('heading', name='先選一個 AI 助手', exact=True).wait_for()
                else:
                    page.get_by_label('主機登入金鑰').wait_for()
                assert calls.count('host/desktop-login') == first_count
                assert not errors, errors
                page.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.fixture
def native_entry_ui():
    """Actual compiled browser UI; Controller and native HA pages are explicit fixtures."""
    root = Path(__file__).parents[1]
    server = ThreadingHTTPServer(('127.0.0.1', 0),
        partial(SimpleHTTPRequestHandler, directory=str(root / 'controller/static')))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state = {'authenticated': True, 'entry': {'contract_version': 1, 'state': 'ready', 'native_path': '/', 'reason_code': None},
             'failure': False, 'held': False, 'pending': [], 'calls': [], 'native_visits': 0}
    task = {'id': 'pending-approval', 'request': '待批准的家庭操作', 'state': 'awaiting_approval',
            'provider': 'codex', 'message': '尚未執行', 'created_at': '2026-10-12T09:00:00Z',
            'updated_at': '2026-10-12T09:00:00Z',
            'proposal': {'id': 'proposal-fixture', 'mode': 'action', 'digest': 'd' * 64,
                         'expires': 4102444800, 'action': {'entity_id': 'light.fixture', 'action': 'turn_on'}}}
    def handle(route):
        request = route.request
        path = request.url.split('/api/')[1]
        state['calls'].append((request.method, path))
        if path == 'host/home-entry':
            if state['held']:
                state['pending'].append(route)
                return
            route.fulfill(status=503 if state['failure'] else 200, content_type='application/json',
                          body=json.dumps({'detail': '無法取得家庭入口'} if state['failure'] else state['entry']))
            return
        if path in ('host/login', 'host/desktop-login'):
            state['authenticated'] = True
            value = {'authenticated': True}
        elif path == 'host/logout':
            state['authenticated'] = False
            value = {'authenticated': False}
        elif path == 'deployment':
            value = {'mode': 'host', 'authenticated': state['authenticated']}
        elif path == 'session':
            value = {'principal': 'host-owner', 'csrf': 'fixture-csrf'}
        elif path == 'status':
            value = {'ha': {'status': 'connected', 'location_name': '測試家庭'},
                     'runner': {'protocol': 1, 'providers': []}, 'version': 'fixture', 'paused': False}
        elif path == 'tasks':
            value = {'tasks': [task]}
        elif path == 'tasks/' + task['id']:
            value = task
        elif path == 'bootstrap':
            value = {'contract': 1, 'id': None, 'state': 'idle', 'provider': None, 'channel': 'line',
                     'current_step': None, 'human_action': None,
                     'steps': [{'id': step, 'state': 'pending'} for step in STEPS]}
        elif path == 'gateway':
            value = {'preferences': {'runtime': 'host', 'provider': 'codex', 'coordinator': 'hermes'}, 'channels': []}
        elif path == 'gateway/messages':
            value = {'messages': []}
        elif path == 'host/status':
            value = {'vm': 'shut off', 'profile': {'label': '救援主機', 'memory_mib': 4096, 'vcpus': 2},
                     'jobs': [], 'snapshots': [], 'provisioned': True}
        else:
            raise AssertionError('Unexpected native entry API: ' + path)
        route.fulfill(status=200, content_type='application/json', body=json.dumps(value))
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={'width': 1440, 'height': 900})
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            context.route('**/api/**', handle)
            def native(route):
                state['native_visits'] += 1
                route.fulfill(content_type='text/html', body='<h1>Native Home Assistant fixture</h1>')
            context.route('http://127.0.0.1:8123/**', native)
            yield page, f'http://127.0.0.1:{server.server_port}', state
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_bare_login_enters_native_home_once_and_back_reload_stay_outer(native_entry_ui):
    page, origin, state = native_entry_ui
    state['authenticated'] = False
    page.goto(origin + '/')
    page.get_by_label('主機登入金鑰').fill('synthetic-local-login')
    page.get_by_role('button', name='開啟管理中心').click()
    page.wait_for_url('http://127.0.0.1:8123/')
    assert state['calls'].count(('GET', 'host/home-entry')) == 1
    assert state['native_visits'] == 1
    page.go_back()
    page.wait_for_url(origin + '/#entry')
    page.get_by_role('heading', name='家庭已就緒').wait_for()
    page.reload()
    page.get_by_role('heading', name='家庭已就緒').wait_for()
    assert page.url == origin + '/#entry'
    assert state['native_visits'] == 1
    primary = page.get_by_role('region', name='家庭入口').get_by_role('link', name='進入家庭')
    # Wait for the existing 150 ms secondary-to-primary transition before capture.
    expect(primary).to_have_css('background-color', 'rgb(33, 100, 83)')
    expect(primary).to_have_css('color', 'rgb(255, 254, 250)')
    expect(primary).to_have_css('opacity', '1')
    for width in (1440, 768, 390):
        page.set_viewport_size({'width': width, 'height': 900 if width == 1440 else 844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(Path(__file__).parents[1] / f'evidence/ui/native-home/ready-{width}.png'), full_page=True)
    assert not any(method == 'POST' and path != 'host/login' for method, path in state['calls'])


@pytest.mark.parametrize('entry_state', ['unavailable', 'unsupported', 'invalid_path', 'invalid_contract', 'http_error'])
def test_native_entry_failures_keep_rescue_and_never_restart_setup(native_entry_ui, entry_state):
    page, origin, state = native_entry_ui
    if entry_state == 'http_error':
        state['failure'] = True
    elif entry_state == 'invalid_path':
        state['entry']['native_path'] = 'https://untrusted.invalid/'
    elif entry_state == 'invalid_contract':
        state['entry']['contract_version'] = 2
    else:
        state['entry']['state'] = entry_state
    page.goto(origin + '/')
    page.get_by_role('heading', name='家庭暫時無法連接').wait_for()
    expect(page.locator('.topbar .ha-status')).to_have_count(0)
    fallback = page.get_by_role('region', name='家庭入口')
    expect(fallback.get_by_role('link', name='手動開啟 Home Assistant')).to_have_attribute('href', 'http://127.0.0.1:8123/')
    expect(fallback.get_by_role('button', name='智能管家', exact=True)).to_be_enabled()
    expect(fallback.get_by_role('button', name='主機維護', exact=True)).to_be_enabled()
    assert page.url == origin + '/#entry' and state['native_visits'] == 0
    assert ('GET', 'bootstrap') not in state['calls']
    assert not any(method == 'POST' for method, _ in state['calls'])
    if entry_state == 'unavailable':
        for width in (1440, 768, 390):
            page.set_viewport_size({'width': width, 'height': 900 if width == 1440 else 844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(Path(__file__).parents[1] / f'evidence/ui/native-home/unavailable-{width}.png'), full_page=True)
    state['failure'] = False
    state['entry'] = {'contract_version': 1, 'state': 'ready', 'native_path': '/', 'reason_code': None}
    fallback.get_by_role('button', name='重新檢查').click()
    page.get_by_role('heading', name='家庭已就緒').wait_for()
    assert state['native_visits'] == 0  # A retry requires an explicit entry action.
    with page.expect_popup() as opened:
        fallback.get_by_role('link', name='進入家庭').click()
    opened.value.get_by_role('heading', name='Native Home Assistant fixture').wait_for()
    assert page.url == origin + '/#entry'
    opened.value.close()


@pytest.mark.parametrize('fragment,heading', [('#gateway', '智能管家'), ('#host', '主機維護'), ('#home/pending-approval', '家庭提案')])
def test_explicit_links_and_pending_approval_never_redirect(native_entry_ui, fragment, heading):
    page, origin, state = native_entry_ui
    page.goto(origin + '/' + fragment)
    page.get_by_role('heading', name=heading, exact=True).first.wait_for()
    assert ('GET', 'host/home-entry') not in state['calls']
    assert state['native_visits'] == 0 and page.url.endswith(fragment)
    if fragment.startswith('#home/'):
        expect(page.get_by_role('button', name='批准並執行', exact=True)).to_be_enabled()
        assert not any(method == 'POST' for method, _ in state['calls'])


def test_late_entry_probe_cannot_replace_explicit_chat_and_draft_survives(native_entry_ui):
    page, origin, state = native_entry_ui
    state['held'] = True
    page.goto(origin + '/')
    page.get_by_role('heading', name='正在確認家庭入口').wait_for()
    page.get_by_role('region', name='家庭入口').get_by_role('button', name='智能管家', exact=True).click()
    page.get_by_label('對話訊息').fill('保留這份尚未送出的草稿')
    assert state['pending']
    for route in state['pending']:
        route.fulfill(content_type='application/json', body=json.dumps(state['entry']))
    state['held'] = False
    assert page.url.endswith('#gateway') and state['native_visits'] == 0
    page.get_by_role('navigation', name='主選單').get_by_role('button', name='家庭', exact=False).first.click()
    page.get_by_role('heading', name='家庭已就緒').wait_for()
    page.get_by_role('region', name='家庭入口').get_by_role('button', name='智能管家', exact=True).click()
    expect(page.get_by_label('對話訊息')).to_have_value('保留這份尚未送出的草稿')
    assert '保留這份尚未送出的草稿' not in page.evaluate('JSON.stringify({session:{...sessionStorage},local:{...localStorage}})')
    assert not any(method == 'POST' for method, _ in state['calls'])


def test_desktop_daily_entry_is_not_inherited_after_logout(native_entry_ui):
    page, origin, state = native_entry_ui
    ticket = 'FixtureDesktopTicket' + 'A' * 23
    state['authenticated'] = False
    page.goto(origin + '/#desktop-login=' + ticket)
    page.wait_for_url('http://127.0.0.1:8123/')
    assert state['calls'].count(('POST', 'host/desktop-login')) == 1
    page.go_back()
    page.wait_for_url(origin + '/#entry')
    page.get_by_role('heading', name='家庭已就緒').wait_for()
    page.get_by_role('button', name='登出', exact=True).click()
    page.get_by_label('主機登入金鑰').fill('synthetic-second-login')
    page.get_by_role('button', name='開啟管理中心').click()
    page.get_by_role('heading', name='家庭已就緒').wait_for()
    assert page.url.endswith('#entry') and state['native_visits'] == 1
    assert ticket not in page.url and ticket not in page.evaluate('JSON.stringify({session:{...sessionStorage},local:{...localStorage}})')


def test_expired_outer_session_cancels_pending_automatic_entry(native_entry_ui):
    page, origin, state = native_entry_ui
    state['held'] = True
    page.goto(origin + '/')
    page.get_by_role('heading', name='正在確認家庭入口').wait_for()
    assert state['pending']
    page.evaluate("window.dispatchEvent(new Event('agentic-session-expired'))")
    page.get_by_label('主機登入金鑰').wait_for()
    for route in state['pending']:
        route.fulfill(content_type='application/json', body=json.dumps(state['entry']))
    state['held'] = False
    page.get_by_label('主機登入金鑰').fill('synthetic-return-login')
    page.get_by_role('button', name='開啟管理中心').click()
    page.get_by_role('heading', name='家庭已就緒').wait_for()
    assert page.url.endswith('#entry') and state['native_visits'] == 0
