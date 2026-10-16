"""Real compiled UI with protocol fixtures; not real HA or household acceptance."""

import copy
import hashlib
import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright
from test_discovery_ui import observation
from ui_helpers import click_navigation

HISTORICAL_TIME = '2021-02-03T04:05:06Z'
CANDIDATE = '待設定橋接器（合成資料）'
DEVICE = '書桌燈（合成資料）'
INTEGRATION = '燈光整合（合成資料）'


def context_observation():
    value = observation()
    value['observed_at'] = HISTORICAL_TIME
    value['content_digest'] = 'b' * 64
    value['candidates'][0].update(title=CANDIDATE)
    value['devices'][0].update(id='unselected-parent-private-id', name='未選取的橋接器')
    value['devices'][1].update(
        name=DEVICE, via_device_id='unselected-parent-private-id',
        parent_device_id='unselected-parent-other-id',
        integration_ids=['unselected-integration-private-id'],
    )
    value['integrations'][0].update(id='unselected-integration-private-id', title=INTEGRATION)
    # New API fields must not silently expand the text shared with the assistant.
    value['devices'][1]['private_address'] = 'private-device-address.invalid'
    value['private_account'] = 'private-account-token'
    for source in value['sources']:
        if source['state'] == 'ok':
            source.update(observed_at=HISTORICAL_TIME, item_count=94721,
                          target='private-source-target', content_digest='a' * 64)
    return value


@pytest.fixture
def context_browser():
    root = Path(__file__).parents[1]
    server = ThreadingHTTPServer(
        ('127.0.0.1', 0), partial(SimpleHTTPRequestHandler, directory=str(root / 'controller/static')),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state = {
        'mode': 'host', 'storage_scope': '1' * 64, 'snapshot': context_observation(),
        'discovery_status': 200, 'hold': False, 'held': [], 'requests': [], 'messages': [],
        'errors': [], 'unhandled': [], 'coordinator': 'hermes',
    }

    def route(intercept):
        request = intercept.request
        path = request.url.split('/api/', 1)[1].split('?', 1)[0]
        body = request.post_data_json if request.method == 'POST' else None
        state['requests'].append((request.method, path, body))
        status = 200
        if path == 'discovery':
            if state['hold']:
                state['held'].append(intercept)
                return
            result, status = state['snapshot'], state['discovery_status']
        elif path == 'deployment':
            result = {'mode': state['mode'], 'authenticated': True, 'bridge_state': 'ready'}
        elif path == 'session':
            result = {'principal': 'fixture-owner', 'csrf': 'fixture-csrf', 'mode': state['mode'],
                      'storage_scope': state['storage_scope'], 'resume': state.get('resume')}
        elif path == 'status':
            result = {'ha': {'status': 'connected'}, 'runner': {'protocol': 1, 'providers': []},
                      'gateway': {'selected': True}, 'onboarding': {'policy_confirmed': True},
                      'policy_readable': 1, 'version': 'fixture', 'paused': False}
        elif path == 'tasks':
            result = {'tasks': []}
        elif path == 'inventory':
            result = {'entities': [], 'policy': {'entities': []}}
        elif path == 'gateway':
            result = {'selected': True, 'web': {'status': 'connected'}, 'channels': [],
                      'preferences': {'primary': 'web', 'runtime': 'host', 'provider': 'codex',
                                      'coordinator': state['coordinator']}}
        elif path == 'gateway/messages' and body is None:
            result = {'messages': state['messages']}
        elif path == 'gateway/messages':
            assert request.headers.get('x-agentic-csrf') == 'fixture-csrf'
            assert request.headers.get('idempotency-key')
            identity = hashlib.sha256(body['message'].encode()).hexdigest()
            result = {**body, 'id': identity, 'thread_id': identity, 'source': 'web',
                      'state': 'succeeded', 'answer': '已收到這次選取的資料（合成回覆）。',
                      'created_at': '2026-10-16T12:00:00Z'}
            state['messages'].insert(0, result)
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
            state['url'] = f'http://127.0.0.1:{server.server_port}/#gateway'
            yield page, state
            assert not state['errors'], state['errors']
            assert not state['unhandled'], state['unhandled']
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def open_context(page, state):
    page.goto(state['url'])
    page.get_by_label('對話訊息', exact=True).wait_for()
    page.get_by_role('button', name='分享探索結果', exact=True).click()
    dialog = page.get_by_role('dialog', name='分享探索結果', exact=True)
    expect(dialog).to_be_visible()
    return dialog


def select_item(dialog, category, name):
    dialog.get_by_role('combobox', name='檢視項目', exact=True).select_option(label=category)
    dialog.get_by_role('checkbox', name=f'選取 {name}', exact=True).check()


def writes(state):
    return [(path, body) for method, path, body in state['requests'] if method != 'GET']


def test_context_exact_preview_preserves_draft_and_waits_for_normal_send(context_browser):
    page, state = context_browser
    page.goto(state['url'])
    draft = '請幫我判斷這些設備下一步怎麼接。\n先保留我的限制：暫時不開啟控制。'
    composer = page.get_by_label('對話訊息', exact=True)
    composer.fill(draft)
    page.get_by_role('button', name='分享探索結果', exact=True).click()
    dialog = page.get_by_role('dialog', name='分享探索結果', exact=True)
    expect(dialog.locator('input[type=checkbox]:checked')).to_have_count(0)
    expect(dialog.get_by_role('button', name='帶入對話', exact=True)).to_be_disabled()
    select_item(dialog, '待設定設備', CANDIDATE)
    select_item(dialog, '已登錄設備', DEVICE)
    select_item(dialog, '已設定整合', INTEGRATION)
    preview = dialog.get_by_role('textbox', name='對話預覽', exact=True)
    expect(preview).to_have_attribute('readonly', '')
    reviewed = preview.input_value()
    assert draft in reviewed and all(name in reviewed for name in (CANDIDATE, DEVICE, INTEGRATION))
    assert HISTORICAL_TIME in reviewed and len(reviewed) <= 5000
    assert writes(state) == []
    dialog.get_by_role('button', name='帶入對話', exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(composer).to_have_value(reviewed)
    expect(composer).to_be_focused()
    assert writes(state) == []
    edited = reviewed + '\n補充：請先說明，再讓我決定。'
    composer.fill(edited)
    page.get_by_role('button', name='送出訊息', exact=True).click()
    expect(page.get_by_text('已收到這次選取的資料（合成回覆）。', exact=True)).to_be_visible()
    assert writes(state) == [('gateway/messages', {
        'message': edited, 'runtime': 'host', 'provider': 'codex', 'reply_to': None, 'coordinator': 'hermes',
    })]


def test_context_selected_device_has_scoped_metadata_without_hidden_household_details(context_browser):
    page, state = context_browser
    dialog = open_context(page, state)
    select_item(dialog, '已登錄設備', DEVICE)
    preview = dialog.get_by_role('textbox', name='對話預覽', exact=True).input_value()
    assert DEVICE in preview and HISTORICAL_TIME in preview
    assert all(label in preview for label in ('已登錄設備', '設備功能清單', '功能目前狀態'))
    assert all(label not in preview for label in ('家庭自動探索', '已設定整合', '路由器設備清單'))
    for private in (CANDIDATE, INTEGRATION, '未選取的橋接器', 'unselected-parent-private-id',
                    'unselected-parent-other-id', 'unselected-integration-private-id',
                    'private-device-address.invalid', 'private-account-token', 'private-source-target',
                    '94721', 'a' * 64, 'b' * 64):
        assert private not in preview
    assert writes(state) == []


def test_context_selection_is_bounded_and_refresh_drops_previous_review(context_browser):
    page, state = context_browser
    template = state['snapshot']['candidates'][0]
    state['snapshot']['candidates'] = [
        {**template, 'id': f'fixture-candidate-{index}', 'title': f'待設定項目 {index}'} for index in range(10)
    ]
    dialog = open_context(page, state)
    for index in range(8):
        dialog.get_by_role('checkbox', name=f'選取 待設定項目 {index}', exact=True).check()
    expect(dialog.locator('input[type=checkbox]:checked')).to_have_count(8)
    expect(dialog.get_by_role('checkbox', name='選取 待設定項目 8', exact=True)).to_be_disabled()
    dialog.get_by_role('combobox', name='檢視項目', exact=True).select_option(label='已設定整合')
    expect(dialog.get_by_role('checkbox', name=f'選取 {INTEGRATION}', exact=True)).to_be_disabled()
    dialog.get_by_role('combobox', name='檢視項目', exact=True).select_option(label='待設定設備')
    dialog.get_by_role('checkbox', name='選取 待設定項目 0', exact=True).uncheck()
    dialog.get_by_role('checkbox', name='選取 待設定項目 8', exact=True).check()
    before = sum(path == 'discovery' for _, path, _ in state['requests'])
    state['hold'] = True
    dialog.get_by_role('button', name='重新讀取', exact=True).click()
    expect(dialog.get_by_role('button', name='帶入對話', exact=True)).to_be_disabled()
    expect(dialog.locator('input[type=checkbox]:checked')).to_have_count(0)
    expect(dialog.get_by_role('textbox', name='對話預覽', exact=True)).to_have_count(0)
    page.wait_for_function('document.querySelector("dialog")?.textContent.includes("讀取")')
    assert sum(path == 'discovery' for _, path, _ in state['requests']) == before + 1
    fresh = copy.deepcopy(state['snapshot'])
    fresh['candidates'][0]['title'] = '重新讀取的項目'
    state['hold'] = False
    state['held'].pop().fulfill(status=200, content_type='application/json', body=json.dumps(fresh))
    expect(dialog.get_by_role('checkbox', name='選取 重新讀取的項目', exact=True)).to_be_visible()
    expect(dialog.locator('input[type=checkbox]:checked')).to_have_count(0)
    expect(dialog.get_by_role('button', name='帶入對話', exact=True)).to_be_disabled()
    assert writes(state) == []


def test_context_combined_limit_never_truncates_or_overwrites_existing_draft(context_browser):
    page, state = context_browser
    page.goto(state['url'])
    composer = page.get_by_label('對話訊息', exact=True)
    draft = '原' * 4950
    composer.fill(draft)
    page.get_by_role('button', name='分享探索結果', exact=True).click()
    dialog = page.get_by_role('dialog', name='分享探索結果', exact=True)
    select_item(dialog, '待設定設備', CANDIDATE)
    expect(dialog.get_by_role('button', name='帶入對話', exact=True)).to_be_disabled()
    expect(dialog.get_by_text('5,000', exact=False)).to_be_visible()
    context_length = len(dialog.get_by_role('textbox', name='對話預覽', exact=True).input_value()) - len(draft) - 2
    dialog.get_by_role('button', name='關閉', exact=True).click()
    expect(composer).to_have_value(draft)
    fitting_draft = '原' * (5000 - context_length - 2)
    composer.fill(fitting_draft)
    page.get_by_role('button', name='分享探索結果', exact=True).click()
    select_item(dialog, '待設定設備', CANDIDATE)
    reviewed = dialog.get_by_role('textbox', name='對話預覽', exact=True).input_value()
    assert len(reviewed) == 5000 and reviewed.startswith(fitting_draft + '\n\n')
    expect(dialog.get_by_role('button', name='帶入對話', exact=True)).to_be_enabled()
    dialog.get_by_role('button', name='帶入對話', exact=True).click()
    expect(composer).to_have_value(reviewed)
    assert writes(state) == []


@pytest.mark.parametrize('mode', ['host', 'bridge'])
def test_context_native_settings_link_and_keyboard_responsive_dialog(context_browser, tmp_path, mode):
    page, state = context_browser
    state['mode'] = mode
    page.goto(state['url'])
    trigger = page.get_by_role('button', name='分享探索結果', exact=True)
    composer = page.get_by_label('對話訊息', exact=True)
    for width in (390, 768, 1440):
        height = 900 if width == 1440 else 844
        page.set_viewport_size({'width': width, 'height': height})
        page.evaluate('window.scrollTo(0,0)')
        expect(trigger).to_be_visible()
        box = composer.bounding_box()
        assert box and 0 <= box['y'] and box['y'] + box['height'] <= height
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        trigger.focus()
        trigger.press('Enter')
        dialog = page.get_by_role('dialog', name='分享探索結果', exact=True)
        expect(dialog).to_be_visible()
        native = dialog.get_by_role('link', name='開啟家庭設定', exact=False)
        expect(native).to_have_attribute('href', 'http://127.0.0.1:8123/config/integrations'
                                        if mode == 'host' else '/config/integrations')
        expect(native).to_have_attribute('target', '_blank' if mode == 'host' else '_top')
        assert dialog.evaluate('(el) => el.contains(document.activeElement)')
        for _ in range(15):
            page.keyboard.press('Tab')
            assert dialog.evaluate('(el) => el.contains(document.activeElement)')
        page.keyboard.press('Shift+Tab')
        assert dialog.evaluate('(el) => el.contains(document.activeElement)')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(tmp_path / f'context-{mode}-{width}.png'), full_page=True)
        page.keyboard.press('Escape')
        expect(dialog).not_to_be_visible()
        expect(trigger).to_be_focused()
    assert writes(state) == []


def test_context_failure_and_partial_sources_do_not_claim_no_devices_or_control_readiness(context_browser):
    page, state = context_browser
    snapshot = state['snapshot']
    state['snapshot'] = {'detail': '家庭暫時無法連接。'}
    state['discovery_status'] = 503
    dialog = open_context(page, state)
    expect(dialog.get_by_role('alert')).to_contain_text('家庭暫時無法連接。')
    expect(dialog.get_by_role('button', name='帶入對話', exact=True)).to_be_disabled()
    expect(dialog.get_by_text('目前沒有', exact=False)).to_have_count(0)
    state['snapshot'], state['discovery_status'] = snapshot, 200
    snapshot['candidates'] = []
    snapshot['sources'][0].update(state='partial', truncated=True)
    next(source for source in snapshot['sources'] if source['id'] == 'ha_states').update(state='unavailable')
    snapshot['devices'][1]['available_entity_count'] = None
    dialog.get_by_role('button', name='重新讀取', exact=True).click()
    expect(dialog.get_by_text('此類資料尚未完整取得，清單可能不完整。', exact=True)).to_be_visible()
    select_item(dialog, '已登錄設備', DEVICE)
    reviewed = dialog.get_by_role('textbox', name='對話預覽', exact=True).input_value()
    assert '暫時無法讀取' in reviewed
    assert '尚無資料' in reviewed or '尚未取得' in reviewed
    assert '實際控制' in reviewed
    assert writes(state) == []


@pytest.mark.parametrize('mutation', ['unknown_contract', 'untrusted_native_path'])
def test_context_invalid_contract_retains_fixed_native_link_without_selection(context_browser, mutation):
    page, state = context_browser
    if mutation == 'unknown_contract':
        state['snapshot']['contract_version'] = 99
    else:
        state['snapshot']['native_path'] = 'https://untrusted.invalid/private-login'
    dialog = open_context(page, state)
    expect(dialog.get_by_role('alert')).to_contain_text('探索資料格式已變更')
    expect(dialog.get_by_role('checkbox')).to_have_count(0)
    expect(dialog.get_by_role('button', name='帶入對話', exact=True)).to_be_disabled()
    expect(dialog.get_by_role('link', name='開啟家庭設定', exact=False)).to_have_attribute(
        'href', 'http://127.0.0.1:8123/config/integrations',
    )
    assert writes(state) == []


def test_context_is_absent_from_direct_mode_and_readonly_history(context_browser):
    page, state = context_browser
    state['messages'] = [{
        'id': 'a' * 64, 'thread_id': 'a' * 64, 'source': 'line', 'runtime': 'host',
        'provider': 'codex', 'coordinator': 'hermes', 'state': 'succeeded',
        'message': '另一個聊天室的過去需求', 'answer': '過去的回覆', 'created_at': HISTORICAL_TIME,
    }]
    page.goto(state['url'])
    trigger = page.get_by_role('button', name='分享探索結果', exact=True)
    expect(trigger).to_be_visible()
    page.get_by_role('button', name='對話紀錄', exact=False).click()
    expect(trigger).to_have_count(0)
    page.get_by_role('region', name='對話紀錄').get_by_role('button').filter(
        has_text='另一個聊天室的過去需求',
    ).click()
    expect(page.get_by_text('請回原來的聊天室繼續。', exact=True)).to_be_visible()
    expect(trigger).to_have_count(0)
    page.get_by_role('button', name='開始新對話', exact=True).click()
    expect(trigger).to_be_visible()
    page.locator('summary').filter(has_text='進階').click()
    page.get_by_label('對話方式', exact=True).select_option('direct')
    expect(trigger).to_have_count(0)
    page.get_by_label('對話方式', exact=True).select_option('hermes')
    expect(trigger).to_be_visible()
    assert not any(path == 'discovery' for _, path, _ in state['requests'])
    assert writes(state) == []


@pytest.mark.parametrize('boundary', ['close', 'thread', 'surface', 'session'])
def test_context_discards_late_snapshot_across_conversation_surface_and_identity(context_browser, boundary):
    page, state = context_browser
    if boundary == 'session':
        state['mode'] = 'bridge'
    if boundary == 'thread':
        identity = 'a' * 64
        state['messages'] = [{
            'id': identity, 'thread_id': identity, 'source': 'web', 'runtime': 'host',
            'provider': 'codex', 'coordinator': 'hermes', 'state': 'succeeded',
            'message': '前一段對話', 'answer': '前一段回覆', 'created_at': HISTORICAL_TIME,
        }]
        state['resume'] = {'message_id': identity, 'thread_id': identity, 'case_id': None}
    state['hold'] = True
    old_snapshot = copy.deepcopy(state['snapshot'])
    dialog = open_context(page, state)
    expect(dialog.get_by_role('button', name='帶入對話', exact=True)).to_be_disabled()
    page.wait_for_timeout(50)
    assert len(state['held']) == 1
    old_route = state['held'].pop()
    if boundary == 'close':
        page.keyboard.press('Escape')
    elif boundary == 'thread':
        page.keyboard.press('Escape')
        page.get_by_role('button', name='新對話', exact=True).click()
        expect(page.get_by_label('對話訊息', exact=True)).to_have_attribute('placeholder', '輸入訊息…')
    elif boundary == 'surface':
        # Browser history can change the outer surface while a modal has focus.
        page.evaluate("location.hash = 'inventory'")
        expect(page.get_by_role('region', name='探索設備')).to_be_visible()
        state['hold'] = False
        for pending in state['held']:
            pending.fulfill(status=200, content_type='application/json', body=json.dumps(old_snapshot))
        state['held'].clear()
        click_navigation(page, '智能管家')
    else:
        page.evaluate("window.dispatchEvent(new Event('agentic-bridge-expired'))")
        expect(page.get_by_role('region', name='管家連線')).to_be_visible()
        state['storage_scope'] = '2' * 64
        page.get_by_role('button', name='重新連線', exact=True).click()
    expect(dialog).not_to_be_visible()
    state['hold'] = False
    state['snapshot']['candidates'][0]['title'] = '這次重新讀取的候選項目'
    page.get_by_role('button', name='分享探索結果', exact=True).click()
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role('checkbox', name='選取 這次重新讀取的候選項目', exact=True)).to_be_visible()
    old_route.fulfill(status=200, content_type='application/json', body=json.dumps(old_snapshot))
    page.wait_for_timeout(100)
    expect(dialog.get_by_role('checkbox', name='選取 這次重新讀取的候選項目', exact=True)).to_be_visible()
    expect(dialog.get_by_role('checkbox', name=f'選取 {CANDIDATE}', exact=True)).to_have_count(0)
    expect(dialog.locator('input[type=checkbox]:checked')).to_have_count(0)
    expect(dialog.get_by_role('button', name='帶入對話', exact=True)).to_be_disabled()
    assert writes(state) == []
