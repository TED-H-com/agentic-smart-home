"""Compiled conversation UI using synthetic devices; no live household acceptance."""

import hashlib
import json
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright


@pytest.fixture
def conversation_browser():
    root = Path(__file__).parents[1]
    server = ThreadingHTTPServer(
        ('127.0.0.1', 0), partial(SimpleHTTPRequestHandler, directory=os.environ.get(
            'AGENTIC_UI_STATIC_DIR', str(root / 'controller/static'))),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state = {
        'requests': [], 'messages': [], 'errors': [], 'unhandled': [],
        'ha': 'connected', 'owner_verified': True, 'policy_confirmed': False,
        'runner': {'protocol': 1, 'providers': [{'provider': 'codex', 'status': 'connected'}]},
        'gateway_selected': True, 'mode': 'host', 'response': {},
    }

    def route(intercept):
        request = intercept.request
        path = request.url.split('/api/', 1)[1].split('?', 1)[0]
        body = request.post_data_json if request.method == 'POST' else None
        state['requests'].append((request.method, path, body, request.headers.get('idempotency-key')))
        status = 200
        if path == 'deployment':
            result = {'mode': state['mode'], 'authenticated': True, 'bridge_state': 'ready'}
        elif path == 'session':
            result = {'principal': 'fixture-owner', 'csrf': 'fixture-csrf', 'mode': state['mode'],
                      'storage_scope': '1' * 64}
        elif path == 'status':
            result = {'ha': {'status': state['ha']}, 'runner': state['runner'],
                      'gateway': {'selected': state['gateway_selected']},
                      'onboarding': {'owner_verified': state['owner_verified'],
                                     'policy_confirmed': state['policy_confirmed']},
                      'policy_readable': 0, 'version': 'fixture', 'paused': False}
        elif path == 'tasks':
            result = {'tasks': []}
        elif path == 'gateway':
            result = {'selected': True, 'web': {'status': 'connected'}, 'channels': [],
                      'preferences': {'primary': 'web', 'runtime': 'host', 'provider': 'codex',
                                      'coordinator': 'hermes'}}
        elif path == 'gateway/messages' and body is None:
            result = {'messages': state['messages']}
        elif path == 'gateway/messages':
            assert request.headers.get('x-agentic-csrf') == 'fixture-csrf'
            assert request.headers.get('idempotency-key')
            if state.get('fail_send'):
                intercept.fulfill(status=503, content_type='application/json',
                                  body=json.dumps({'detail': '服務暫時無法回應。'}))
                return
            identity = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
            parent = next((message for message in state['messages'] if message['id'] == body['reply_to']), None)
            result = {**body, 'id': identity, 'thread_id': parent['thread_id'] if parent else identity,
                      'source': 'web', 'state': 'succeeded', 'answer': '合成的管家回覆。',
                      'created_at': f'2026-10-19T12:00:{len(state["messages"]):02d}Z', **state['response']}
            state['messages'].insert(0, result)
        elif path == 'bootstrap':
            result = {'contract': 1, 'id': None, 'state': 'not_started', 'steps': []}
        elif path == 'discovery/setups' and body is None:
            result = {'contract_version': 1, 'limit': 20, 'retention_days': 7, 'setups': []}
        elif path == 'discovery/setups':
            assert request.headers.get('x-agentic-csrf') == 'fixture-csrf'
            assert request.headers.get('idempotency-key')
            result, status = {'detail': '探索結果已改變，請重新探索。'}, 409
        else:
            state['unhandled'].append((request.method, path))
            result, status = {'detail': '測試未提供此介面。'}, 404
        intercept.fulfill(status=status, content_type='application/json', body=json.dumps(result))

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 900})
            page.on('pageerror', lambda error: state['errors'].append(str(error)))
            page.route('**/api/**', route)
            state['url'] = f'http://127.0.0.1:{server.server_port}/'
            yield page, state
            assert not state['errors'], state['errors']
            assert not state['unhandled'], state['unhandled']
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_completed_host_continues_device_setup_in_conversation(conversation_browser):
    page, state = conversation_browser
    page.goto(state['url'] + '#home')
    guide = page.get_by_role('region', name='開始使用')
    expect(guide).to_contain_text('讓管家協助接好設備。')
    guide.get_by_role('button', name='繼續設定', exact=True).click()
    expect(page).to_have_url(state['url'] + '#gateway')
    expect(page.get_by_label('對話訊息', exact=True)).to_be_visible()
    assert not any(path == 'bootstrap' for _, path, _, _ in state['requests'])
    assert not any(method != 'GET' for method, _, _, _ in state['requests'])


def test_unfinished_host_still_continues_onboarding(conversation_browser):
    page, state = conversation_browser
    state['owner_verified'] = False
    page.goto(state['url'] + '#home')
    guide = page.get_by_role('region', name='開始使用')
    expect(guide).to_contain_text('設定尚未完成')
    guide.get_by_role('button', name='繼續設定', exact=True).click()
    expect(page).to_have_url(state['url'] + '#onboarding')
    expect(page.get_by_role('button', name='開始設定', exact=True)).to_be_visible()


def test_connected_host_outage_is_not_unfinished_device_setup(conversation_browser):
    page, state = conversation_browser
    state['ha'] = 'unavailable'
    page.goto(state['url'] + '#home')
    expect(page.locator('.composer')).to_contain_text('家庭尚未連線。')
    expect(page.locator('.setup-loading')).to_have_count(0)
    expect(page.get_by_role('region', name='開始使用')).to_have_count(0)


def discovery_result(state='ready'):
    return {
        'contract_version': 1, 'state': state, 'native_path': '/config/integrations',
        'observed_at': '2026-10-19T12:00:00Z' if state == 'ready' else None,
        'content_digest': 'a' * 64 if state == 'ready' else None,
        'candidate_count': 1 if state == 'ready' else None, 'truncated': False,
        'limitations': ['not_a_complete_lan_inventory'],
        'candidates': [{
            'id': 'fixture-printer', 'integration_domain': 'ipp', 'title': '書房印表機（合成資料）',
            'integration_name': '網路印表機', 'next_step': '先確認印表機已開機，再由管家協助連接。',
            'discovery_method': 'zeroconf', 'source_id': 'ha_discovery',
            'native_path': '/config/integrations',
        }] if state == 'ready' else [],
        'sources': [{
            'id': 'ha_discovery', 'state': 'ok', 'truncated': False, 'item_count': 1,
            'observed_at': '2026-10-19T12:00:00Z', 'reason_code': None,
        }] if state == 'ready' else [],
    }


def sent_messages(state):
    return [body for method, path, body, _ in state['requests']
            if method == 'POST' and path == 'gateway/messages']


def test_explore_is_explicit_preserves_draft_and_conversation(conversation_browser, tmp_path):
    page, state = conversation_browser
    state['response'] = {'discovery': discovery_result()}
    page.goto(state['url'] + '#gateway')
    composer = page.get_by_label('對話訊息', exact=True)
    composer.fill('保留草稿：暫時不要開啟控制。')
    expect(page.get_by_text('將找到的設備名稱與接入提示交給管家協助。')).to_be_visible()
    assert sent_messages(state) == []
    page.get_by_role('button', name='探索家庭設備', exact=True).click()
    results = page.get_by_role('region', name='設備探索結果', exact=True)
    expect(results.get_by_text('書房印表機（合成資料）', exact=True)).to_be_visible()
    expect(composer).to_have_value('保留草稿：暫時不要開啟控制。')
    expect(composer).to_be_focused()
    first = state['messages'][0]
    assert sent_messages(state)[0]['discover_devices'] is True
    assert sent_messages(state)[0]['reply_to'] is None
    assert not any(path == 'discovery' for _, path, _, _ in state['requests'])

    evidence = Path(os.environ.get('CONVERSATION_DISCOVERY_UI_EVIDENCE', str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    for width in (1440, 768, 390):
        height = 900 if width == 1440 else 844
        page.set_viewport_size({'width': width, 'height': height})
        page.evaluate('window.scrollTo(0,0)')
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        send_box = page.get_by_role('button', name='送出訊息', exact=True).bounding_box()
        assert send_box and send_box['height'] >= 44 and 0 <= send_box['y'] <= height - send_box['height']
        assert composer.evaluate('element => parseFloat(getComputedStyle(element).fontSize)') >= 16
        button = results.get_by_role('button', name='協助設定', exact=True)
        button.scroll_into_view_if_needed()
        box = button.bounding_box()
        assert box and box['height'] >= 44
        page.screenshot(path=str(evidence / f'conversation-discovery-{width}.png'), full_page=True)

    results.get_by_role('button', name='協助設定', exact=True).click()
    expect(results).to_have_count(2)
    second = state['messages'][0]
    assert sent_messages(state)[1]['reply_to'] == first['id']
    assert '書房印表機（合成資料）' in sent_messages(state)[1]['message']
    expect(composer).to_have_value('保留草稿：暫時不要開啟控制。')
    expect(results.first.get_by_role('button', name='協助設定', exact=True)).to_be_disabled()
    expect(results.first.locator('summary', has_text='接入進度')).to_have_count(0)
    expect(results.last.locator('summary', has_text='接入進度')).to_be_visible()

    # Consent is specific to the action; ordinary follow-up does not silently share again.
    state['response'] = {}
    page.get_by_role('button', name='送出訊息', exact=True).click()
    expect(composer).to_have_value('')
    assert sent_messages(state)[2]['reply_to'] == second['id']
    assert 'discover_devices' not in sent_messages(state)[2]
    assert all(path == 'gateway/messages' for method, path, _, _ in state['requests'] if method == 'POST')


def test_natural_request_waits_for_explicit_discovery_consent(conversation_browser):
    page, state = conversation_browser
    state['response'] = {'discovery': discovery_result('consent_required')}
    page.goto(state['url'] + '#gateway')
    composer = page.get_by_label('對話訊息', exact=True)
    composer.fill('找看看家裡有什麼還沒接好。')
    page.get_by_role('button', name='送出訊息', exact=True).click()
    consent = page.get_by_role('region', name='設備探索授權', exact=True)
    expect(consent).to_be_visible()
    assert 'discover_devices' not in sent_messages(state)[0]
    assert len(sent_messages(state)) == 1
    state['response'] = {'discovery': discovery_result()}
    consent.get_by_role('button', name='探索家庭設備', exact=True).click()
    expect(page.get_by_role('region', name='設備探索結果')).to_be_visible()
    assert sent_messages(state)[1]['discover_devices'] is True
    assert sent_messages(state)[1]['message'] == sent_messages(state)[0]['message']


@pytest.mark.parametrize('coverage', ['partial', 'complete', 'unavailable', 'invalid'])
def test_discovery_empty_partial_failure_remain_distinct(conversation_browser, coverage):
    page, state = conversation_browser
    value = discovery_result('unavailable' if coverage == 'unavailable' else 'ready')
    value['candidates'] = []
    if coverage == 'partial':
        value['sources'][0]['state'] = 'unavailable'
        value['candidate_count'] = None
    elif coverage == 'complete':
        value['candidate_count'] = 0
    elif coverage == 'invalid':
        value['native_path'] = 'https://untrusted.invalid/'
    state['response'] = {'discovery': value}
    page.goto(state['url'] + '#gateway')
    page.get_by_role('button', name='探索家庭設備', exact=True).click()
    results = page.get_by_role('region', name='設備探索結果')
    expected = {
        'partial': '探索資料尚未完整取得，目前無法確認待接入的設備。',
        'complete': '這次未找到待接入的設備；仍可能有需要登入或配對的設備。',
        'unavailable': '家庭暫時沒有回應，尚無法確認有哪些設備。',
        'invalid': '暫時無法讀取探索結果，請再試一次。',
    }[coverage]
    expect(results.get_by_text(expected, exact=True)).to_be_visible()
    expect(results.get_by_role('link')).to_have_count(0)
    if coverage != 'complete':
        expect(results.get_by_text('這次未找到待接入', exact=False)).to_have_count(0)


def test_discovery_labels_are_text_and_bridge_keeps_current_conversation(conversation_browser):
    page, state = conversation_browser
    state['mode'] = 'bridge'
    value = discovery_result()
    value['candidates'][0]['title'] = '<img src=x onerror=alert(1)> 合成設備'
    state['response'] = {'discovery': value}
    page.goto(state['url'] + '#gateway')
    page.get_by_role('button', name='探索家庭設備', exact=True).click()
    results = page.get_by_role('region', name='設備探索結果')
    expect(results.get_by_text('<img src=x onerror=alert(1)> 合成設備', exact=True)).to_be_visible()
    expect(results.locator('img,iframe,script')).to_have_count(0)
    assert sent_messages(state)[0]['coordinator'] == 'hermes'
    assert not any(path == 'bootstrap' for _, path, _, _ in state['requests'])


def test_discovery_retry_preserves_key_but_does_not_reuse_unshared_request(conversation_browser):
    page, state = conversation_browser
    state['fail_send'] = True
    state['response'] = {'discovery': discovery_result()}
    page.goto(state['url'] + '#gateway')
    composer = page.get_by_label('對話訊息', exact=True)
    composer.fill('請找出家庭裡尚未接好的設備，列出找到的名稱，帶我完成下一步。')
    page.get_by_role('button', name='送出訊息', exact=True).click()
    expect(page.get_by_role('alert')).to_contain_text('服務暫時無法回應。')
    action = page.get_by_role('button', name='探索家庭設備', exact=True)
    action.click()
    expect(page.get_by_role('alert')).to_contain_text('服務暫時無法回應。')
    state['fail_send'] = False
    action.click()
    expect(page.get_by_role('region', name='設備探索結果')).to_be_visible()
    posts = [(body, key) for method, path, body, key in state['requests']
             if method == 'POST' and path == 'gateway/messages']
    assert len(posts) == 3
    assert posts[0][0]['message'] == posts[1][0]['message']
    assert 'discover_devices' not in posts[0][0] and posts[1][0]['discover_devices'] is True
    assert posts[0][1] != posts[1][1]
    assert posts[1][1] == posts[2][1]


def test_conversation_setup_tracking_requires_exact_owner_selection(conversation_browser):
    page, state = conversation_browser
    state['response'] = {'discovery': discovery_result()}
    page.goto(state['url'] + '#gateway')
    page.get_by_role('button', name='探索家庭設備', exact=True).click()
    results = page.get_by_role('region', name='設備探索結果')
    expect(results.get_by_role('button', name='協助設定', exact=True)).to_be_visible()
    assert not any(path == 'discovery/setups' for _, path, _, _ in state['requests'])
    results.locator('summary').filter(has_text='接入進度').click()
    track = results.get_by_role('button', name='追蹤接入 書房印表機（合成資料）', exact=True)
    expect(track).to_be_enabled()
    assert not any(method == 'POST' and path == 'discovery/setups'
                   for method, path, _, _ in state['requests'])
    track.click()
    expect(results.get_by_role('alert')).to_contain_text('探索結果已改變，請重新探索。')
    assert [body for method, path, body, _ in state['requests']
            if method == 'POST' and path == 'discovery/setups'] == [
        {'candidate_id': 'fixture-printer', 'integration_domain': 'ipp'},
    ]
    expect(results.get_by_text('尚未進行實體操作驗證。')).to_have_count(0)
