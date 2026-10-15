"""Recovery copy for reason codes and friendly proposal names. Protocol fixtures only."""
import json
import re
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).parents[1]
HERMES_COPY = {
    'access_denied': ('登入被拒絕，請用你的帳號重新登入。', '重新登入', 'failed'),
    'slow_down': ('官方要求稍候，請一分鐘後再試。', '再試一次', 'failed'),
    'expired_token': ('代碼已過期，請重新開始。', '重新開始', 'expired'),
    'oauth_failed': ('登入沒有完成，請重新開始。', '重新開始', 'failed'),
    'stop_unconfirmed': ('尚未確認上一個登入已停止，請稍後重新檢查。', '重新檢查', 'stop_unconfirmed'),
}
BOOTSTRAP_COPY = {
    'hermes_rate_limited': '供應商暫時限制請求，請稍後再重試驗證。',
    'hermes_provider_unavailable': '模型服務暫時無法連線，請稍後再重試驗證。',
    'hermes_verification_timeout': '這次模型驗證逾時，請稍後再重試。',
    'hermes_verification_failed': '智能管家的連線驗證沒有完成，請檢查登入後重試。',
    'hermes_install_failed': '智能管家還沒準備好，請查看主機後重試。',
    'ha_provision_failed': '家庭環境尚未就緒，請查看主機後重試。',
    'cli_assist_failed': '設定確認沒有完成，請檢查登入後重試。',
    'cli_assist_expired': '這次設定確認已過期，請重試。',
    'needs_reauth': '登入已失效，請重新登入。',
    'rate_limited': '已達用量上限，請稍後再試。',
    'invalid_credentials': '登入資訊無效，請重新輸入。',
    'network': '無法連線到 AI 服務，請檢查網路後再試。',
    'cli_output_unrecognized': '無法確認登入結果，請稍後再試。',
    'timeout': 'AI 帳號回應逾時，請稍後再試。',
}
STEPS = ['network', 'runner_pairing', 'cli_auth', 'cli_assist', 'hermes_install', 'hermes_auth', 'channel', 'ha_provision', 'ha_native']
# Upper bound for changes the UI only sees on its next poll (1.5-3 s); a loaded runner needs the slack.
POLL = 10_000


def _server():
    if not (ROOT / 'controller/static/index.html').exists():
        pytest.skip('Build the web UI before the browser test')
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(SimpleHTTPRequestHandler, directory=str(ROOT / 'controller/static')))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _layouts(page, name):
    evidence = ROOT / 'evidence/ui'
    evidence.mkdir(parents=True, exist_ok=True)
    for width, height in ((1440, 900), (768, 844), (390, 844)):
        page.set_viewport_size({'width': width, 'height': height})
        page.evaluate('window.scrollTo(0,0)')
        page.wait_for_timeout(80)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), name + ' ' + str(width)
        if width in (1440, 390):
            page.screenshot(path=str(evidence / f'recovery-{name}-{width}.png'), full_page=True)


def _finish(server, thread, browser, errors):
    browser.close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)
    assert not errors, errors


def test_hermes_login_codes_and_pending_resume():
    server, thread = _server()
    runtime = {'state': 'needs_auth', 'provider': 'openai-codex', 'model': 'gpt-6-astra', 'execution_state': 'idle',
               'default_models': {'openai-codex': 'gpt-6-astra'}}
    posts, errors = [], []
    queue = [
        ('access_denied', 'failed', 200),
        ('slow_down', 'failed', 429),
        ('expired_token', 'expired', 200),
        ('oauth_failed', 'failed', 200),
        ('stop_unconfirmed', 'stop_unconfirmed', 200),
    ]
    cursor = {'n': 0}
    pending = {'auth_id': 'pending-auth', 'state': 'pending', 'provider': 'openai-codex', 'model': 'gpt-6-astra',
               'verification_uri': 'https://auth.openai.com/codex/device', 'user_code': 'RESUME-CODE',
               'expires_at': '2026-10-13T00:00:00Z', 'error': None, 'reason': None}
    current = {}

    def route(route):
        request = route.request
        path = request.url.split('/api/')[1].split('?')[0]
        body = request.post_data_json if request.method == 'POST' else None
        if body is not None:
            assert request.headers.get('x-agentic-csrf') == 'fixture-csrf'
            assert request.headers.get('idempotency-key')
        if path == 'hermes/auth/device' and body is not None:
            posts.append(body)
            code, state, status = queue[cursor['n']]
            cursor['n'] += 1
            current.clear()
            current.update(auth_id='auth-fixture', state=state, provider='openai-codex', model='gpt-6-astra',
                           verification_uri=None, user_code=None, error=code, reason=code)
            if status != 200:
                route.fulfill(status=status, content_type='application/json', body=json.dumps(
                    {'detail': {'code': code, 'message': '官方登入暫時忙碌，請稍後再試，不要重複開啟授權。', 'reason': code}}))
                return
            result = dict(current)
        elif path.startswith('hermes/auth/device/'):
            result = pending if path.endswith('pending-auth') else dict(current)
        elif path == 'hermes':
            result = {'runtime': runtime, 'workers': []}
        elif path == 'deployment':
            result = {'mode': 'host', 'authenticated': True}
        elif path == 'session':
            result = {'principal': 'host-owner', 'csrf': 'fixture-csrf', 'mode': 'host'}
        elif path == 'status':
            result = {'ha': {'status': 'connected'}, 'runner': {'protocol': 1, 'providers': []}, 'version': 'fixture', 'paused': False}
        elif path == 'tasks':
            result = {'tasks': []}
        elif path == 'runtimes':
            result = {'host': {'protocol': 1, 'providers': []}, 'haos': {'status': 'unpaired'}}
        else:
            result = {}
        route.fulfill(status=200, content_type='application/json', body=json.dumps(result))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/api/**', route)
        page.goto(f'http://127.0.0.1:{server.server_port}/#agents')
        panel = page.get_by_role('region', name='智能管家登入')
        panel.get_by_role('radio', name='ChatGPT 訂閱', exact=True).check()
        panel.get_by_role('button', name='開始登入', exact=True).click()
        for code, (sentence, action, _state) in HERMES_COPY.items():
            expect(panel.get_by_text(sentence, exact=True)).to_be_visible()
            expect(panel.locator('.recovery-line')).to_have_attribute('data-reason', code)
            expect(panel.get_by_text(code, exact=True)).to_have_count(0)
            expect(panel.get_by_role('button', name=action, exact=True)).to_have_count(1)
            if code == 'access_denied':
                _layouts(page, 'hermes')
            before = len(posts)
            panel.get_by_role('button', name=action, exact=True).click()
            if code == 'stop_unconfirmed':
                page.wait_for_timeout(300)
                assert len(posts) == before
            else:
                expect(panel.get_by_text(HERMES_COPY[queue[before][0]][0], exact=True)).to_be_visible()
        current.update(state='interrupted', error=None, reason=None)
        panel.get_by_role('button', name='重新檢查', exact=True).click()
        expect(panel.locator('.recovery-line')).to_have_count(0)
        expect(panel.get_by_text('官方登入未完成，請重新開始。', exact=True)).to_be_visible()
        expect(panel.get_by_role('button', name='重新開始', exact=True)).to_be_visible()
        before = len(posts)
        runtime['pending_auth'] = pending
        page.reload()
        expect(panel.get_by_text('RESUME-CODE', exact=True)).to_be_visible()
        expect(panel.get_by_role('link', name='開啟登入頁')).to_have_attribute('href', 'https://auth.openai.com/codex/device')
        page.wait_for_timeout(400)
        assert len(posts) == before
        _layouts(page, 'hermes-resume')
        _finish(server, thread, browser, errors)


def test_conversation_reauth_link_and_timeout_retry():
    server, thread = _server()
    reauth_id, timeout_id = 'a' * 64, 'b' * 64
    question = '請看一下走廊的燈'
    messages, posts, errors = [], [], []
    gateway = {'preferences': {'runtime': 'host', 'provider': 'codex', 'coordinator': 'hermes'}, 'channels': []}

    def seed(id, code, answer):
        return {'id': id, 'thread_id': id, 'source': 'web', 'runtime': 'host', 'provider': 'codex', 'coordinator': 'hermes',
                'state': 'failed', 'message': question, 'answer': answer, 'error_code': code, 'reason': code,
                'created_at': '2026-10-13T00:00:00Z'}

    messages.append(seed(reauth_id, 'needs_reauth', '這個執行環境的登入已失效，請在 Web 重新登入後再送出。'))

    def route(route):
        request = route.request
        path = request.url.split('/api/')[1].split('?')[0]
        body = request.post_data_json if request.method == 'POST' else None
        if path == 'deployment':
            result = {'mode': 'host', 'authenticated': True}
        elif path == 'session':
            result = {'principal': 'host-owner', 'csrf': 'fixture-csrf', 'mode': 'host'}
        elif path == 'status':
            result = {'ha': {'status': 'connected'}, 'runner': {'protocol': 1, 'providers': []}, 'version': 'fixture', 'paused': False}
        elif path == 'tasks':
            result = {'tasks': []}
        elif path == 'gateway':
            result = gateway
        elif path == 'hermes':
            result = {'runtime': {'state': 'needs_auth', 'model': 'gpt-6-astra', 'execution_state': 'idle'}, 'workers': []}
        elif path == 'runtimes':
            result = {'host': {'protocol': 1, 'providers': []}, 'haos': {'status': 'unpaired'}}
        elif path == 'gateway/messages' and body is None:
            result = {'messages': messages}
        elif path == 'gateway/messages':
            assert request.headers.get('x-agentic-csrf') == 'fixture-csrf'
            key = request.headers.get('idempotency-key')
            assert key
            posts.append({'body': body, 'key': key})
            result = {**body, 'id': format(len(posts), '064x'), 'thread_id': format(len(posts), '064x'), 'source': 'web',
                      'state': 'failed', 'answer': '這次回覆逾時，請稍後再送出。', 'error_code': 'timeout', 'reason': 'timeout',
                      'created_at': '2026-10-13T00:00:00Z'}
            messages.insert(0, result)
        else:
            result = {}
        route.fulfill(status=200, content_type='application/json', body=json.dumps(result))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/api/**', route)
        origin = f'http://127.0.0.1:{server.server_port}'
        page.goto(origin + '/#gateway')
        page.evaluate("""id => sessionStorage.setItem('agentic-auth-conversations', JSON.stringify({'hermes-host-codex': id}))""", reauth_id)
        page.reload()
        link = page.get_by_role('link', name='重新登入智能管家', exact=True)
        expect(link).to_be_visible()
        expect(page.get_by_role('button', name='重試', exact=True)).to_have_count(0)
        link.click()
        expect(page.get_by_role('heading', name='智能管家登入', exact=True)).to_be_visible()
        assert posts == []
        messages[:] = [seed(timeout_id, 'timeout', '這次回覆逾時，請稍後再送出。')]
        page.evaluate("""id => sessionStorage.setItem('agentic-auth-conversations', JSON.stringify({'hermes-host-codex': id}))""", timeout_id)
        page.evaluate("location.hash='#gateway'")
        page.reload()
        retry = page.get_by_role('button', name='重試', exact=True)
        expect(retry).to_be_visible()
        expect(page.get_by_text('這段對話暫時無法接續，請等候完成或開始新對話。', exact=True)).to_have_count(0)
        _layouts(page, 'conversation')
        retry.click()
        box = page.get_by_label('對話訊息', exact=True)
        expect(box).to_have_value(question)
        expect(box).to_be_focused()
        page.wait_for_timeout(400)
        assert posts == []
        page.get_by_role('button', name='送出訊息', exact=True).click()
        expect(page.get_by_role('button', name='重試', exact=True)).to_be_visible(timeout=POLL)
        assert len(posts) == 1 and posts[0]['body']['message'] == question and posts[0]['body']['reply_to'] is None
        page.get_by_role('button', name='重試', exact=True).click()
        expect(box).to_have_value(question)
        page.wait_for_timeout(300)
        assert len(posts) == 1
        page.get_by_role('button', name='送出訊息', exact=True).click()
        page.wait_for_timeout(300)
        assert len(posts) == 2 and posts[0]['key'] != posts[1]['key']
        _layouts(page, 'conversation-retry')
        direct_id = 'c' * 64
        gateway['preferences']['coordinator'] = 'direct'
        messages[:] = [{**seed(direct_id, 'needs_reauth', '這個執行環境的登入已失效，請在 Web 重新登入後再送出。'), 'coordinator': 'direct'}]
        page.evaluate("""id => sessionStorage.setItem('agentic-auth-conversations', JSON.stringify({'host-codex': id}))""", direct_id)
        page.reload()
        expect(page.get_by_role('link', name='重新登入 AI 帳號', exact=True)).to_be_visible()
        expect(page.get_by_role('link', name='重新登入智能管家', exact=True)).to_have_count(0)
        _finish(server, thread, browser, errors)


def test_line_qr_epoch_and_terminal_failures_restart():
    server, thread = _server()
    line = {'status': 'idle', 'channels': [], 'reason': None, 'message': None, 'qr': None, 'pin': None, 'qr_epoch': 0}
    posts, reads, errors = [], [], []
    gateway = {'step': 0, 'selected': True, 'preferences': {'primary': 'line', 'runtime': 'host', 'provider': 'codex', 'coordinator': 'hermes'},
               'channels': [{'id': name, 'status': 'disabled', 'enabled': False, 'chats': [], 'senders': [], 'has_secret': False, 'reason': None, 'message': None}
                            for name in ('line', 'telegram', 'discord')]}

    def route(route):
        request = route.request
        path = request.url.split('/api/')[1].split('?')[0]
        body = request.post_data_json if request.method == 'POST' else None
        if path == 'gateway/line/login' and body is not None:
            assert request.headers.get('x-agentic-csrf') == 'fixture-csrf'
            assert request.headers.get('idempotency-key')
            posts.append(body)
            if line['status'] in {'idle', 'qr_expired', 'pin_failed', 'login_rejected', 'service_unavailable'}:
                line.update(status='authenticating', qr=f'https://fixture.example/qr-{len(posts)}', pin=None, qr_epoch=1,
                            reason=None, message=None)
        elif path == 'deployment':
            result = {'mode': 'host', 'authenticated': True}
        elif path == 'session':
            result = {'principal': 'host-owner', 'csrf': 'fixture-csrf', 'mode': 'host'}
        elif path == 'status':
            result = {'ha': {'status': 'connected'}, 'runner': {'protocol': 1, 'providers': []}, 'version': 'fixture', 'paused': False}
        elif path == 'tasks':
            result = {'tasks': []}
        elif path == 'gateway':
            result = gateway
        elif path == 'gateway/messages':
            result = {'messages': []}
        elif path == 'gateway/line/login':
            result = line
        else:
            result = {}
        if path == 'gateway/line/login':
            if body is None:
                reads.append(line['status'])
            result = line
        route.fulfill(status=200, content_type='application/json', body=json.dumps(result))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/api/**', route)
        page.goto(f'http://127.0.0.1:{server.server_port}/#gateway/channels')
        page.get_by_role('button', name='開始 LINE 掃碼登入', exact=True).click()
        image = page.get_by_alt_text('用 LINE 掃描這個 QR')
        expect(image).to_have_attribute('src', re.compile(r'^data:image/png;base64,'))
        expect(page.get_by_text('已更新 QR code', exact=True)).to_have_count(0)
        first = image.get_attribute('src')
        line.update(qr='https://fixture.example/qr-2', qr_epoch=2)
        expect(page.get_by_text('已更新 QR code', exact=True)).to_have_count(1, timeout=POLL)
        expect(image).not_to_have_attribute('src', first)
        page.wait_for_timeout(1800)
        expect(page.get_by_text('已更新 QR code', exact=True)).to_have_count(1)
        line.update(status='qr_expired', qr=None, reason='qr_expired', message=None, qr_epoch=2)
        expect(page.get_by_text('QR 已過期，請重新開始。', exact=True)).to_be_visible(timeout=POLL)
        expect(page.get_by_role('button', name='重新開始', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='開始 LINE 掃碼登入', exact=True)).to_be_hidden()
        expect(page.locator('.line-login')).to_have_attribute('data-reason', 'qr_expired')
        expect(page.get_by_text('qr_expired', exact=True)).to_have_count(0)
        # The attempt has ended: polling stops until the owner starts again.
        settled = len(reads)
        page.wait_for_timeout(2000)
        assert len(reads) == settled
        page.get_by_role('button', name='重新開始', exact=True).click()
        expect(image).to_have_attribute('src', re.compile(r'^data:image/png;base64,'))
        assert len(posts) == 2 and posts[-1] == {}
        line.update(status='pin_failed', reason='pin_failed', message=None, qr=None)
        expect(page.get_by_text('PIN 驗證沒有通過，請重新開始。', exact=True)).to_be_visible(timeout=POLL)
        expect(page.get_by_text('已更新 QR code', exact=True)).to_have_count(0)
        _layouts(page, 'line')
        page.get_by_role('button', name='重新開始', exact=True).click()
        expect(image).to_be_visible()
        assert len(posts) == 3 and posts[-1] == {}
        line.update(status='service_unavailable', reason='service_unavailable', message=None, qr=None)
        expect(page.get_by_text('LINE 服務暫時無法使用，請稍後再試。', exact=True)).to_be_visible(timeout=POLL)
        expect(page.get_by_role('button', name='重新開始', exact=True)).to_have_count(0)
        page.get_by_role('button', name='再試一次', exact=True).click()
        expect(image).to_be_visible()
        assert len(posts) == 4 and posts[-1] == {}
        _finish(server, thread, browser, errors)


def test_telegram_token_focus_and_conflict_sentences():
    server, thread = _server()
    channels = {name: {'id': name, 'status': 'disabled', 'enabled': False, 'chats': [], 'senders': [], 'has_secret': False, 'reason': None, 'message': None}
                for name in ('line', 'telegram', 'discord')}
    channels['telegram'].update(status='invalid_token', reason='invalid_token', message=None)
    errors = []
    gateway = {'step': 0, 'selected': True, 'preferences': {'primary': 'telegram', 'runtime': 'host', 'provider': 'codex', 'coordinator': 'hermes'},
               'channels': list(channels.values())}

    def route(route):
        path = route.request.url.split('/api/')[1].split('?')[0]
        if path == 'deployment':
            result = {'mode': 'host', 'authenticated': True}
        elif path == 'session':
            result = {'principal': 'host-owner', 'csrf': 'fixture-csrf', 'mode': 'host'}
        elif path == 'status':
            result = {'ha': {'status': 'connected'}, 'runner': {'protocol': 1, 'providers': []}, 'version': 'fixture', 'paused': False}
        elif path == 'tasks':
            result = {'tasks': []}
        elif path == 'gateway':
            gateway['channels'] = list(channels.values())
            result = gateway
        elif path == 'gateway/messages':
            result = {'messages': []}
        else:
            result = {}
        route.fulfill(status=200, content_type='application/json', body=json.dumps(result))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 390, 'height': 844})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/api/**', route)
        page.goto(f'http://127.0.0.1:{server.server_port}/#gateway/channels')
        settings = page.get_by_role('region', name='對外通訊設定')
        expect(page.get_by_text('權杖無效，請重新貼上。', exact=True)).to_be_visible()
        expect(page.get_by_text('invalid_token', exact=True)).to_have_count(0)
        expect(page.get_by_label('Telegram 機器人權杖', exact=True)).to_be_focused()
        expect(settings.get_by_role('button', name='重新檢查連線', exact=True)).to_have_count(0)
        expect(settings.get_by_role('button', name='重新檢查', exact=True)).to_have_count(0)
        _layouts(page, 'telegram-token')
        channels['telegram'].update(status='webhook_conflict', reason='webhook_active', message=None)
        expect(page.get_by_text('這個機器人已接到別的服務，請先在那裡解除。', exact=True)).to_be_visible(timeout=15000)
        expect(page.get_by_text('這個機器人正被另一個程式使用，請先停止它。', exact=True)).to_have_count(0)
        channels['telegram'].update(status='webhook_conflict', reason='poller_conflict', message=None)
        expect(page.get_by_text('這個機器人正被另一個程式使用，請先停止它。', exact=True)).to_be_visible(timeout=15000)
        expect(page.get_by_text('這個機器人已接到別的服務，請先在那裡解除。', exact=True)).to_have_count(0)
        channels['discord'].update(status='unavailable', reason='unavailable', message=None)
        page.get_by_role('radio', name='Discord', exact=True).check()
        expect(page.get_by_text('Discord · 連線中斷', exact=True)).to_be_visible(timeout=15000)
        expect(page.get_by_text('在 Discord 開發者頁建立機器人，並允許讀寫訊息。', exact=True)).to_be_visible()
        expect(page.get_by_text('暫時連不上 Telegram。', exact=True)).to_have_count(0)
        _layouts(page, 'telegram-conflict')
        _finish(server, thread, browser, errors)


def _channel_gateway(channels, preferences):
    gateway = {'step': 0, 'selected': True, 'preferences': preferences, 'channels': list(channels.values())}

    def publish():
        gateway['channels'] = list(channels.values())
        return gateway

    return gateway, publish


def test_channel_disable_confirms_before_the_same_request():
    server, thread = _server()
    preferences = {'primary': 'line', 'runtime': 'host', 'provider': 'codex', 'coordinator': 'hermes'}
    channels = {name: {'id': name, 'status': 'disabled', 'enabled': False, 'chats': [], 'senders': [], 'has_secret': False, 'reason': None, 'message': None}
                for name in ('line', 'telegram', 'discord')}
    channels['line'].update(status='connected', enabled=True, chats=['room-1'], senders=['user-1'])
    gateway, publish = _channel_gateway(channels, preferences)
    posts, errors = [], []

    def route(route):
        request = route.request
        path = request.url.split('/api/')[1].split('?')[0]
        body = request.post_data_json if request.method == 'POST' else None
        if body is not None:
            posts.append({'path': path, 'body': body, 'csrf': request.headers.get('x-agentic-csrf'),
                          'key': request.headers.get('idempotency-key')})
            if path == 'gateway/channels/line':
                channels['line'].update(enabled=body['enabled'], chats=body['chats'], senders=body['senders'],
                                        status='disabled' if not body['enabled'] else 'connected')
            elif path == 'gateway/preferences':
                preferences.update(body)
        if path == 'deployment':
            result = {'mode': 'host', 'authenticated': True}
        elif path == 'session':
            result = {'principal': 'host-owner', 'csrf': 'fixture-csrf', 'mode': 'host'}
        elif path == 'status':
            result = {'ha': {'status': 'connected'}, 'runner': {'protocol': 1, 'providers': []}, 'version': 'fixture', 'paused': False}
        elif path == 'tasks':
            result = {'tasks': []}
        elif path == 'gateway':
            result = publish()
        elif path == 'gateway/messages':
            result = {'messages': []}
        else:
            result = {'ok': True}
        route.fulfill(status=200, content_type='application/json', body=json.dumps(result))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/api/**', route)
        page.goto(f'http://127.0.0.1:{server.server_port}/#gateway/channels')
        settings = page.get_by_role('region', name='對外通訊設定')
        expect(settings.get_by_label('允許的聊天室 ID')).to_have_value('room-1')
        expect(settings.get_by_label('允許的使用者 ID')).to_have_value('user-1')
        settings.get_by_role('button', name='停用此通道', exact=True).click()
        confirm = settings.get_by_role('group', name='停用這個通訊？')
        expect(confirm).to_be_visible()
        page.wait_for_timeout(300)
        assert posts == []
        confirm.get_by_role('button', name='取消', exact=True).click()
        expect(confirm).to_have_count(0)
        assert posts == []
        settings.get_by_role('button', name='停用此通道', exact=True).click()
        confirm.get_by_role('button', name='停用', exact=True).click()
        expect(settings.get_by_text('此通道已停用，Web 仍可使用。', exact=True)).to_be_visible()
        assert [item['path'] for item in posts] == ['gateway/channels/line', 'gateway/preferences']
        assert posts[0]['body'] == {'enabled': False, 'secret': '', 'chats': ['room-1'], 'senders': ['user-1']}
        assert posts[1]['body'] == {'primary': 'line', 'runtime': 'host', 'provider': 'codex', 'coordinator': 'hermes'}
        assert posts[0]['csrf'] == posts[1]['csrf'] == 'fixture-csrf'
        assert re.fullmatch(r'[0-9a-f]{32}', posts[0]['key'])
        assert re.fullmatch(r'[0-9a-f]{32}', posts[1]['key'])
        assert posts[0]['key'] != posts[1]['key']
        _finish(server, thread, browser, errors)


def test_channel_poll_waits_while_the_form_is_dirty():
    server, thread = _server()
    preferences = {'primary': 'telegram', 'runtime': 'host', 'provider': 'codex', 'coordinator': 'hermes'}
    channels = {name: {'id': name, 'status': 'disabled', 'enabled': False, 'chats': [], 'senders': [], 'has_secret': False, 'reason': None, 'message': None}
                for name in ('line', 'telegram', 'discord')}
    channels['telegram'].update(status='invalid_token', reason='invalid_token', message=None)
    gateway, publish = _channel_gateway(channels, preferences)
    posts, errors = [], []

    def route(route):
        request = route.request
        path = request.url.split('/api/')[1].split('?')[0]
        if request.method == 'POST':
            posts.append(path)
        if path == 'deployment':
            result = {'mode': 'host', 'authenticated': True}
        elif path == 'session':
            result = {'principal': 'host-owner', 'csrf': 'fixture-csrf', 'mode': 'host'}
        elif path == 'status':
            result = {'ha': {'status': 'connected'}, 'runner': {'protocol': 1, 'providers': []}, 'version': 'fixture', 'paused': False}
        elif path == 'tasks':
            result = {'tasks': []}
        elif path == 'gateway':
            result = publish()
        elif path == 'gateway/messages':
            result = {'messages': []}
        else:
            result = {}
        route.fulfill(status=200, content_type='application/json', body=json.dumps(result))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 390, 'height': 844})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/api/**', route)
        page.goto(f'http://127.0.0.1:{server.server_port}/#gateway/channels')
        expect(page.get_by_text('權杖無效，請重新貼上。', exact=True)).to_be_visible()
        page.get_by_label('Telegram 機器人權杖', exact=True).fill('fixture-token-not-saved')
        channels['telegram'].update(status='webhook_conflict', reason='webhook_active', message=None)
        page.wait_for_timeout(6000)
        expect(page.get_by_text('權杖無效，請重新貼上。', exact=True)).to_be_visible()
        expect(page.get_by_text('這個機器人已接到別的服務，請先在那裡解除。', exact=True)).to_have_count(0)
        assert posts == []
        page.get_by_role('radio', name='Discord', exact=True).check()
        page.get_by_role('radio', name='Telegram', exact=True).check()
        expect(page.get_by_text('這個機器人已接到別的服務，請先在那裡解除。', exact=True)).to_be_visible(timeout=12000)
        assert posts == []
        _finish(server, thread, browser, errors)


def test_channel_read_failure_clears_on_the_next_good_read():
    server, thread = _server()
    preferences = {'primary': 'line', 'runtime': 'host', 'provider': 'codex', 'coordinator': 'hermes'}
    channels = {name: {'id': name, 'status': 'disabled', 'enabled': False, 'chats': [], 'senders': [], 'has_secret': False, 'reason': None, 'message': None}
                for name in ('line', 'telegram', 'discord')}
    channels['line'].update(status='connected', enabled=True, chats=['room-1'], senders=['user-1'])
    gateway, publish = _channel_gateway(channels, preferences)
    failing, errors = {'gateway': False}, []

    def route(route):
        request = route.request
        path = request.url.split('/api/')[1].split('?')[0]
        assert request.method == 'GET', path
        if path == 'deployment':
            result = {'mode': 'host', 'authenticated': True}
        elif path == 'session':
            result = {'principal': 'host-owner', 'csrf': 'fixture-csrf', 'mode': 'host'}
        elif path == 'status':
            result = {'ha': {'status': 'connected'}, 'runner': {'protocol': 1, 'providers': []}, 'version': 'fixture', 'paused': False}
        elif path == 'tasks':
            result = {'tasks': []}
        elif path == 'gateway' and failing['gateway']:
            route.fulfill(status=503, content_type='application/json', body=json.dumps({'detail': '暫時無法回應'}))
            return
        elif path == 'gateway':
            result = publish()
        elif path == 'gateway/messages':
            result = {'messages': []}
        else:
            result = {}
        route.fulfill(status=200, content_type='application/json', body=json.dumps(result))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 390, 'height': 844})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/api/**', route)
        page.clock.install()
        page.goto(f'http://127.0.0.1:{server.server_port}/#gateway/channels')
        settings = page.get_by_role('region', name='對外通訊設定')
        failed = settings.get_by_role('alert').filter(has_text='暫時無法回應')
        expect(settings.get_by_role('radio', name='LINE 建議', exact=True)).to_be_checked()
        failing['gateway'] = True
        page.clock.run_for(5_500)
        expect(failed).to_be_visible()
        # The next good read clears it; nothing to press.
        failing['gateway'] = False
        page.clock.run_for(5_500)
        expect(failed).to_have_count(0)
        expect(settings.get_by_role('alert')).to_have_count(0)
        _finish(server, thread, browser, errors)


def test_bootstrap_retry_codes_use_one_friendly_sentence():
    server, thread = _server()
    flow = {'contract': 1, 'id': 'bootstrap-fixture', 'state': 'blocked', 'provider': 'codex', 'channel': 'line',
            'current_step': 'hermes_auth', 'updated_at': '2026-10-13T00:00:00Z', 'human_action': None,
            'steps': [{'id': step, 'state': 'blocked' if step == 'hermes_auth' else 'completed', 'code': None, 'verified_at': None} for step in STEPS]}
    calls, errors = [], []

    def show(code):
        for step in flow['steps']:
            step['code'] = code if step['id'] == 'hermes_auth' else None
        flow['human_action'] = {'kind': 'retry', 'code': code, 'message': 'Hermes ' + code}
        flow['state'] = 'blocked'

    show('hermes_rate_limited')

    def route(route):
        request = route.request
        path = request.url.split('/api/')[1].split('?')[0]
        body = request.post_data_json if request.method == 'POST' else None
        if path == 'deployment':
            result = {'mode': 'host', 'authenticated': True}
        elif path == 'session':
            result = {'principal': 'host-owner', 'csrf': 'fixture-csrf', 'mode': 'host'}
        elif path == 'status':
            result = {'ha': {'status': 'connected'}, 'runner': {'protocol': 1, 'providers': []}, 'version': 'fixture', 'paused': False}
        elif path == 'tasks':
            result = {'tasks': []}
        elif path == 'bootstrap':
            result = flow
        elif path == 'bootstrap/retry':
            assert body == {}
            assert request.headers.get('x-agentic-csrf') == 'fixture-csrf'
            key = request.headers.get('idempotency-key')
            assert key
            calls.append(key)
            result = flow
        else:
            result = {}
        route.fulfill(status=200, content_type='application/json', body=json.dumps(result))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/api/**', route)
        page.goto(f'http://127.0.0.1:{server.server_port}/#onboarding')
        notice = page.locator('.onboarding-body .notice.error')
        for index, (code, sentence) in enumerate(BOOTSTRAP_COPY.items()):
            show(code)
            expect(notice).to_have_text(sentence, timeout=POLL)
            expect(notice).not_to_contain_text(code)
            expect(notice).not_to_contain_text('Hermes')
            expect(page.get_by_role('button', name='重試這一步', exact=True)).to_have_count(1)
            if index == 0:
                _layouts(page, 'bootstrap')
        page.get_by_text('技術資料', exact=True).click()
        expect(page.locator('.bootstrap-log')).to_contain_text('timeout')
        page.get_by_role('button', name='重試這一步', exact=True).click()
        page.wait_for_timeout(300)
        assert len(calls) == 1 and calls[0]
        _layouts(page, 'bootstrap-retry')
        _finish(server, thread, browser, errors)


def test_rate_limited_account_keeps_one_retry():
    server, thread = _server()
    errors = []
    sentence = '已達用量上限，請稍後再試。'

    def route(route):
        path = route.request.url.split('/api/')[1].split('?')[0]
        if path == 'deployment':
            result = {'mode': 'host', 'authenticated': True}
        elif path == 'session':
            result = {'principal': 'host-owner', 'csrf': 'fixture-csrf', 'mode': 'host'}
        elif path == 'status':
            result = {'ha': {'status': 'connected'}, 'runner': {'protocol': 1, 'providers': []}, 'version': 'fixture', 'paused': False}
        elif path == 'tasks':
            result = {'tasks': []}
        elif path == 'hermes':
            result = {'runtime': {'state': 'needs_auth', 'execution_state': 'idle'}, 'workers': []}
        elif path == 'runtimes':
            result = {'host': {'protocol': 1, 'providers': [
                {'provider': 'codex', 'status': 'needs_reauth', 'reason': 'rate_limited', 'message': sentence},
            ]}, 'haos': {'status': 'unpaired'}}
        elif path == 'runtimes/host/auth/rateauth':
            result = {'id': 'rateauth', 'provider': 'codex', 'status': 'failed', 'reason': 'rate_limited', 'message': sentence,
                      'url': None, 'code': None, 'expires_in': 0}
        else:
            result = {}
        route.fulfill(status=200, content_type='application/json', body=json.dumps(result))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 390, 'height': 844})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.add_init_script("sessionStorage.setItem('agentic-auth-host-codex','rateauth')")
        page.route('**/api/**', route)
        page.goto(f'http://127.0.0.1:{server.server_port}/#agents')
        card = page.locator('article.provider').filter(has=page.get_by_role('heading', name='Codex', exact=True))
        expect(card.get_by_text(sentence, exact=True)).to_have_count(1)
        # The saved login is kept, so the card never claims it must sign in again.
        expect(card.locator('.section-heading .status')).to_have_text('稍後再試')
        expect(card.get_by_role('button', name='再試一次', exact=True)).to_have_count(1)
        expect(card.get_by_role('button', name='重新開始', exact=True)).to_have_count(0)
        expect(card.get_by_role('button', name='重新登入', exact=True)).to_have_count(0)
        _layouts(page, 'account')
        _finish(server, thread, browser, errors)


def test_proposal_names_replace_ids_and_inventory_is_read_once():
    server, thread = _server()
    errors, inventory = [], []
    failed = {'value': False}

    def proposal(trigger, action, source_state='off', target_state='on'):
        return {'id': 'proposal-' + trigger, 'mode': 'upsert', 'digest': 'd' * 64, 'expires': 4102444800,
                'intent': {'trigger': {'entity_id': trigger, 'from_state': source_state, 'to_state': target_state},
                           'actions': [{'entity_id': action, 'action': 'turn_on'}]},
                'final': {'trigger': trigger, 'action': action}}

    tasks = {
        'hall': {'id': 'hall', 'request': '走廊的燈', 'provider': 'codex', 'state': 'awaiting_approval', 'message': '請先看這條規則。',
                 'created_at': '2026-10-13T00:00:00Z', 'updated_at': '2026-10-13T00:00:00Z',
                 'proposal': proposal('binary_sensor.hallway_motion', 'light.hall')},
        'kitchen': {'id': 'kitchen', 'request': '廚房的壺', 'provider': 'codex', 'state': 'awaiting_approval', 'message': '請先看這條規則。',
                    'created_at': '2026-10-13T00:00:00Z', 'updated_at': '2026-10-13T00:00:00Z',
                    'proposal': proposal('binary_sensor.kitchen_motion', 'switch.kettle', 'unknown', 'unavailable')},
    }
    entities = [
        {'entity_id': 'binary_sensor.hallway_motion', 'name': '走廊感應器', 'area': '玄關'},
        {'entity_id': 'light.hall', 'name': '玄關燈', 'area': '玄關'},
        {'entity_id': 'binary_sensor.kitchen_motion', 'name': '廚房感應器', 'area': '廚房'},
        {'entity_id': 'switch.kettle', 'name': '熱水壺', 'area': '廚房'},
    ]

    def route(route):
        path = route.request.url.split('/api/')[1].split('?')[0]
        if path == 'inventory':
            inventory.append(path)
            if failed['value']:
                route.fulfill(status=500, content_type='application/json', body='{"detail":"盤點失敗"}')
                return
            result = {'entities': entities, 'policy': {'entities': []}}
        elif path == 'deployment':
            result = {'mode': 'ingress', 'authenticated': True}
        elif path == 'session':
            result = {'principal': 'fixture-owner', 'csrf': 'fixture-csrf'}
        elif path == 'status':
            result = {'ha': {'status': 'connected', 'location_name': '介面測試'}, 'runner': {'protocol': 1, 'providers': [{'provider': 'codex', 'status': 'connected'}]},
                      'version': 'fixture', 'paused': False, 'onboarding': {'policy_confirmed': True}, 'policy_entities': 1}
        elif path == 'tasks':
            result = {'tasks': list(tasks.values())}
        elif path.startswith('tasks/'):
            result = tasks[path.split('/', 1)[1]]
        else:
            result = {}
        route.fulfill(status=200, content_type='application/json', body=json.dumps(result))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/api/**', route)
        page.goto(f'http://127.0.0.1:{server.server_port}/#home/hall')
        summary = page.locator('.proposal-summary')
        expect(summary).to_contain_text('走廊感應器（玄關）')
        expect(summary).to_contain_text('玄關燈（玄關）')
        expect(summary).to_contain_text('從 關閉 變成 開啟')
        expect(summary).not_to_contain_text('binary_sensor.hallway_motion')
        expect(summary).not_to_contain_text('light.hall')
        expect(page.locator('.rule-step .device-label').nth(0)).to_have_text('走廊感應器（玄關）')
        expect(page.locator('.rule-step .device-label').nth(1)).to_have_text('玄關燈（玄關）')
        expect(page.locator('.proposal-diff')).to_contain_text('binary_sensor.hallway_motion')
        expect(page.locator('.proposal-diff')).to_contain_text('light.hall')
        page.evaluate("location.hash='home/kitchen'")
        expect(summary).to_contain_text('廚房感應器（廚房）')
        expect(summary).to_contain_text('熱水壺（廚房）')
        expect(summary).to_contain_text('從 狀態未知 變成 無法使用')
        expect(summary).not_to_contain_text('binary_sensor.kitchen_motion')
        assert inventory == ['inventory']
        _layouts(page, 'proposal')
        failed['value'] = True
        page.reload()
        expect(summary).to_contain_text('binary_sensor.kitchen_motion')
        expect(page.get_by_text('廚房感應器（廚房）', exact=True)).to_have_count(0)
        _layouts(page, 'proposal-fallback')
        _finish(server, thread, browser, errors)


def test_automatic_poll_recovers_without_hiding_action_error():
    server, thread = _server()
    state = {'offline': False, 'polls': 0}

    def route(route):
        path = route.request.url.split('/api/')[1]
        if path == 'tasks':
            state['polls'] += 1
            if state['offline']:
                route.fulfill(status=503, json={'detail': '暫時連不上管理服務，請稍後再試。'})
                return
        if path == 'pause':
            route.fulfill(status=503, json={'detail': '恢復失敗，請稍後再試。'})
            return
        value = {'deployment': {'mode': 'host', 'authenticated': True},
                 'session': {'csrf': 'fixture-csrf', 'principal': 'fixture-owner'},
                 'status': {'paused': True, 'ha': {'status': 'connected'}},
                 'tasks': {'tasks': []}}.get(path, {})
        route.fulfill(status=200, json=value)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.clock.install()
            page.route('**/api/**', route)
            page.goto(f'http://127.0.0.1:{server.server_port}/#history')
            resume = page.get_by_role('button', name='AI 已暫停 · 恢復', exact=True)
            expect(resume).to_be_visible()
            state['offline'] = True
            page.clock.run_for(10_100)
            error = page.locator('.global-error')
            expect(error).to_contain_text('暫時連不上管理服務')
            state['offline'] = False
            page.clock.run_for(10_100)
            expect(error).to_have_count(0)
            resume.click()
            expect(error).to_contain_text('恢復失敗')
            before = state['polls']
            page.clock.run_for(10_100)
            expect(error).to_contain_text('恢復失敗')
            assert state['polls'] > before
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_lost_connection_recovers_after_failed_reconnect_and_first_load_but_missing_task_stays():
    server, thread = _server()
    state = {'offline': True}
    missing = 'f' * 32

    def route(route):
        path = route.request.url.split('/api/')[1]
        if state['offline'] and path in {'session', 'tasks'}:
            route.fulfill(status=503, json={'detail': '暫時連不上管理服務，請稍後再試。'})
            return
        if path == f'tasks/{missing}':
            route.fulfill(status=404, json={'detail': '這份資料已過期或不存在。'})
            return
        value = {'deployment': {'mode': 'host', 'authenticated': True},
                 'session': {'csrf': 'fixture-csrf', 'principal': 'fixture-owner'},
                 'status': {'paused': True, 'ha': {'status': 'connected'}},
                 'tasks': {'tasks': []}}.get(path, {})
        route.fulfill(status=200, json=value)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.clock.install()
            page.route('**/api/**', route)
            page.goto(f'http://127.0.0.1:{server.server_port}/#history')
            error = page.locator('.global-error')
            resume = page.get_by_role('button', name='AI 已暫停 · 恢復', exact=True)
            # The first load fails: the page opens by itself once the service answers.
            expect(page.get_by_text('暫時無法開啟', exact=True)).to_be_visible()
            expect(error).to_contain_text('暫時連不上管理服務')
            state['offline'] = False
            page.clock.run_for(10_100)
            expect(resume).to_be_visible()
            expect(error).to_have_count(0)
            # A reconnect that fails while the service is down does not keep the error after it is back.
            state['offline'] = True
            page.clock.run_for(10_100)
            expect(error).to_contain_text('暫時連不上管理服務')
            error.get_by_role('button', name='重新連線', exact=True).click()
            expect(error.get_by_role('button', name='重新連線', exact=True)).to_be_enabled()
            expect(error).to_contain_text('暫時連不上管理服務')
            state['offline'] = False
            page.clock.run_for(10_100)
            expect(error).to_have_count(0)
            # A task that no longer exists keeps its message; it does not blink with each poll.
            page.goto(f'http://127.0.0.1:{server.server_port}/#home/{missing}')
            expect(error).to_contain_text('這份資料已過期或不存在。')
            for _ in range(3):
                page.clock.run_for(10_100)
                expect(error).to_contain_text('這份資料已過期或不存在。')
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
