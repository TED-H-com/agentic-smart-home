"""Compiled product setup tracking with synthetic protocol replies, never live HA."""

import copy
import json
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright
from test_discovery_ui import observation

SETUP_ID = 'a' * 32
CANDIDATE = '待連接的燈光橋接器（介面測試資料）'
ENTRY_TITLE = '名稱相同的燈光整合'
OBSERVED = '2026-10-16T12:00:00Z'


def setup_record(identifier=SETUP_ID, entry=None, revision=1):
    return {
        'contract_version': 1, 'id': identifier, 'revision': revision,
        'candidate': observation()['candidates'][0], 'candidate_observed_at': OBSERVED,
        'created_at': OBSERVED, 'updated_at': OBSERVED, 'expires_at': '2099-10-23T12:00:00Z',
        'selected_entry_id': entry, 'selected_at': OBSERVED if entry else None,
    }


def setup_options():
    return [{'integration': {'id': identifier, 'domain': 'fixture_bridge', 'title': ENTRY_TITLE,
                             'state': 'loaded', 'source_id': 'ha_integrations'}, 'known_before': known}
            for identifier, known in [('entry-existing', True), ('entry-new', False), ('entry-unknown', None)]]


def setup_view(record):
    options = setup_options()
    selected = next((item['integration'] for item in options if item['integration']['id'] == record['selected_entry_id']), None)
    devices = []
    if selected:
        device = observation()['devices'][1]
        device.update(integration_ids=[selected['id']], via_device_id=None, parent_device_id=None)
        devices = [device]
    return {
        'contract_version': 1, 'setup': copy.deepcopy(record), 'observed_at': OBSERVED,
        'candidate_state': 'present', 'integration_state': 'present' if selected else 'unselected',
        'integration': selected, 'options': options, 'options_truncated': False,
        'devices': devices, 'devices_truncated': False,
        'device_count': 1 if selected else None, 'entity_count': 1 if selected else None,
        'available_entity_count': 1 if selected else None,
        'sources': observation()['sources'], 'physical_verification': 'not_performed',
        'native_path': '/config/integrations',
    }


@pytest.fixture
def setup_browser():
    root = Path(__file__).parents[1]
    server = ThreadingHTTPServer(
        ('127.0.0.1', 0), partial(SimpleHTTPRequestHandler, directory=str(root / 'controller/static')),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state = {
        'mode': 'host', 'scope': '1' * 64, 'snapshot': observation(), 'records': {}, 'record_scopes': {},
        'views': {}, 'requests': [], 'errors': [], 'unhandled': [], 'held': [], 'hold_paths': set(),
        'overrides': {}, 'replays': {}, 'mutation_mode': 'success', 'commits': [],
    }

    def add_record(record=None):
        record = copy.deepcopy(record or setup_record())
        state['records'][record['id']] = record
        state['record_scopes'][record['id']] = state['scope']
        return record

    state['add_record'] = add_record

    def handle(path, method, body, key):
        if method == 'GET' and path in state['overrides']:
            return state['overrides'][path]
        if path == 'discovery':
            return 200, state['snapshot']
        if path == 'deployment':
            return 200, {'mode': state['mode'], 'authenticated': True, 'bridge_state': 'ready'}
        if path == 'session':
            return 200, {'principal': 'fixture-owner', 'csrf': 'fixture-csrf', 'mode': state['mode'],
                         'storage_scope': state['scope']}
        if path == 'host/login' and method == 'POST':
            assert body == {'key': 'fixture-login-key'}
            return 200, {'connected': True}
        if path == 'status':
            return 200, {'ha': {'status': 'connected'}, 'runner': {'protocol': 1, 'providers': []},
                         'gateway': {'selected': True}, 'onboarding': {'policy_confirmed': True},
                         'policy_readable': 0, 'version': 'fixture', 'paused': False}
        if path == 'tasks':
            return 200, {'tasks': []}
        if path == 'inventory':
            return 200, {'entities': [], 'policy': {'entities': []}}
        if path == 'gateway':
            return 200, {'selected': True, 'channels': [], 'web': {'status': 'connected'},
                         'preferences': {'primary': 'web', 'runtime': 'host', 'provider': 'codex',
                                         'coordinator': 'hermes'}}
        if path == 'gateway/messages' and method == 'GET':
            return 200, {'messages': []}
        if path == 'discovery/setups' and method == 'GET':
            return 200, {'contract_version': 1, 'limit': 20, 'retention_days': 7,
                         'setups': [copy.deepcopy(record) for identifier, record in state['records'].items()
                                    if state['record_scopes'][identifier] == state['scope']]}
        if path.startswith('discovery/setups/') and method == 'GET':
            identifier = path.rsplit('/', 1)[-1]
            record = state['records'].get(identifier)
            if not record or state['record_scopes'][identifier] != state['scope']:
                return 404, {'detail': 'discovery_setup_not_found'}
            return 200, state['views'].get(identifier, setup_view(record))
        if path.startswith('discovery/setups') and method == 'POST':
            assert key, 'tracking writes use the existing idempotency header'
            replay_key = (state['scope'], path, key)
            if replay_key in state['replays']:
                previous_body, result = state['replays'][replay_key]
                assert body == previous_body, 'a retry key must retain the same exact body'
                return 200, result
            if state['mutation_mode'] == 'conflict':
                return 409, {'detail': 'discovery_setup_revision_changed'}
            if state['mutation_mode'] == 'forbidden':
                return 403, {'detail': '家庭授權已失效，請重新確認身分。'}
            if path == 'discovery/setups':
                assert body == {'candidate_id': 'fixture-candidate', 'integration_domain': 'fixture_bridge'}
                record = add_record()
                result = {'contract_version': 1, 'id': record['id'], 'revision': 1, 'forgotten': False}
            else:
                identifier, action = path.split('/')[2:]
                record = state['records'][identifier]
                if body['expected_revision'] != record['revision']:
                    return 409, {'detail': 'discovery_setup_revision_changed'}
                record['revision'] += 1
                result = {'contract_version': 1, 'id': identifier, 'revision': record['revision'],
                          'forgotten': action == 'forget'}
                if action == 'select':
                    assert body['entry_id'] in {item['integration']['id'] for item in setup_options()}
                    record.update(selected_entry_id=body['entry_id'], selected_at=OBSERVED)
                elif action == 'forget':
                    del state['records'][identifier]
                else:
                    raise AssertionError('unexpected tracking mutation: ' + action)
            state['commits'].append((path, copy.deepcopy(body)))
            state['replays'][replay_key] = (copy.deepcopy(body), copy.deepcopy(result))
            return 200, result
        state['unhandled'].append((method, path))
        return 404, {'detail': '測試未提供此介面。'}

    def route(intercept):
        request = intercept.request
        path = request.url.split('/api/', 1)[1].split('?', 1)[0]
        method = request.method
        body = request.post_data_json if method == 'POST' else None
        key = request.headers.get('idempotency-key')
        state['requests'].append({'method': method, 'path': path, 'body': body, 'key': key})
        if method == 'POST':
            assert request.headers.get('x-agentic-csrf') == 'fixture-csrf'
        status, result = handle(path, method, body, key)
        result = copy.deepcopy(result)
        if (method, path) in state['hold_paths']:
            state['held'].append((intercept, status, result))
            return
        if method == 'POST' and state['mutation_mode'] == 'lost_after_commit':
            state['mutation_mode'] = 'success'
            intercept.abort('failed')
            return
        intercept.fulfill(status=status, content_type='application/json', body=json.dumps(result))

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 900})
            page.on('pageerror', lambda error: state['errors'].append(str(error)))
            page.route('**/api/**', route)
            state['base'] = f'http://127.0.0.1:{server.server_port}/'
            yield page, state
            assert not state['errors'], state['errors']
            assert not state['unhandled'], state['unhandled']
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def tracking_requests(state, method=None):
    return [request for request in state['requests'] if request['path'].startswith('discovery/setups')
            and (method is None or request['method'] == method)]


def release_held(state):
    held, state['held'] = state['held'], []
    for intercept, status, result in held:
        intercept.fulfill(status=status, content_type='application/json', body=json.dumps(result))


def open_tracking(page, state, mode='host', navigate=True):
    state['mode'] = mode
    if navigate:
        # A same-URL hash navigation preserves the previous expanded details.
        # Start an independent document for each requested viewport/session.
        page.goto('about:blank')
        page.goto(state['base'] + ('#gateway' if mode == 'bridge' else '#inventory'))
    container = page.get_by_role('region', name='探索設備')
    if mode == 'bridge':
        page.get_by_role('button', name='分享探索結果', exact=True).click()
        container = page.get_by_role('dialog', name='分享探索結果', exact=True)
    summary = container.locator('summary').filter(has_text='接入進度')
    expect(summary).to_be_visible()
    summary.click()
    return container


def open_saved(container, title=CANDIDATE):
    container.get_by_role('button', name=f'查看接入 {title}', exact=True).click()
    result = container.get_by_role('region', name='接入讀回', exact=True)
    expect(result).to_be_visible()
    return result


def choose_entry(container, identifier='entry-new'):
    container.locator(f'input[type=radio][value="{identifier}"]').check()


def assert_only_tracking_writes(state):
    writes = [request for request in state['requests'] if request['method'] != 'GET']
    assert all(request['path'] == 'discovery/setups' or request['path'].startswith('discovery/setups/')
               for request in writes)


def test_tracking_is_lazy_explicit_and_survives_page_refresh(setup_browser):
    page, state = setup_browser
    page.goto(state['base'] + '#inventory')
    container = page.get_by_role('region', name='探索設備')
    expect(container.get_by_text(CANDIDATE, exact=True)).to_be_visible()
    assert tracking_requests(state) == []
    container.locator('summary').filter(has_text='接入進度').click()
    button = container.get_by_role('button', name=f'追蹤接入 {CANDIDATE}', exact=True)
    expect(button).to_be_visible()
    assert tracking_requests(state, 'POST') == []
    button.click()
    expect(container.get_by_role('region', name='接入讀回')).to_be_visible()
    writes = tracking_requests(state, 'POST')
    assert len(writes) == 1
    assert writes[0]['path'] == 'discovery/setups'
    assert writes[0]['body'] == {'candidate_id': 'fixture-candidate', 'integration_domain': 'fixture_bridge'}
    assert len(state['records']) == 1 and state['records'][SETUP_ID]['selected_entry_id'] is None
    expect(container.locator('input[type=radio]:checked')).to_have_count(0)
    before = len(tracking_requests(state))
    page.reload()
    expect(container.locator('summary').filter(has_text='接入進度')).to_be_visible()
    assert len(tracking_requests(state)) == before
    container.locator('summary').filter(has_text='接入進度').click()
    open_saved(container)
    assert len(tracking_requests(state, 'POST')) == 1
    assert len(state['commits']) == 1
    assert_only_tracking_writes(state)


@pytest.mark.parametrize('mode', ['host', 'bridge'])
def test_tracking_requires_exact_entry_selection_and_fixed_native_handoff(setup_browser, mode):
    page, state = setup_browser
    state['add_record']()
    container = open_tracking(page, state, mode)
    result = open_saved(container)
    expect(container.locator('input[type=radio]:checked')).to_have_count(0)
    expect(container.get_by_role('button', name='保存整合選擇', exact=True)).to_be_disabled()
    expect(container.get_by_role('radio')).to_have_count(3)
    for identifier in ('entry-existing', 'entry-new', 'entry-unknown'):
        expect(container.locator(f'input[type=radio][value="{identifier}"]')).to_be_visible()
        expect(container.get_by_text(identifier, exact=True)).to_be_visible()
    native = result.get_by_role('link', name='開啟家庭設定', exact=False)
    expect(native).to_have_attribute('href', 'http://127.0.0.1:8123/config/integrations'
                                    if mode == 'host' else '/config/integrations')
    expect(native).to_have_attribute('target', '_blank' if mode == 'host' else '_top')
    choose_entry(container)
    assert tracking_requests(state, 'POST') == []
    container.get_by_role('button', name='保存整合選擇', exact=True).click()
    expect(container.get_by_text('entry-new', exact=True).first).to_be_visible()
    expect(container.get_by_role('button', name='重新讀取接入', exact=True)).to_be_enabled()
    writes = tracking_requests(state, 'POST')
    assert len(writes) == 1 and writes[0]['path'] == f'discovery/setups/{SETUP_ID}/select'
    assert writes[0]['body'] == {'expected_revision': 1, 'entry_id': 'entry-new'}
    assert state['records'][SETUP_ID]['selected_entry_id'] == 'entry-new'
    assert state['records'][SETUP_ID]['revision'] == 2
    expect(result).to_contain_text('實體')
    assert '接入成功' not in result.inner_text()
    assert_only_tracking_writes(state)


def test_missing_candidate_never_autoselects_same_name_or_loaded_integration(setup_browser):
    page, state = setup_browser
    record = state['add_record']()
    state['snapshot']['candidates'] = []
    value = setup_view(record)
    value['candidate_state'] = 'not_seen'
    state['views'][SETUP_ID] = value
    container = open_tracking(page, state)
    result = open_saved(container)
    expect(container.get_by_role('radio')).to_have_count(3)
    expect(container.locator('input[type=radio]:checked')).to_have_count(0)
    expect(container.get_by_role('button', name='保存整合選擇', exact=True)).to_be_disabled()
    expect(result).to_contain_text('不能據此判定設定完成')
    assert all(text not in result.inner_text() for text in ('接入成功', '已通過實體測試'))
    assert tracking_requests(state, 'POST') == []


def test_partial_readback_keeps_unknown_counts_and_loaded_separate_from_physical_verification(setup_browser):
    page, state = setup_browser
    record = state['add_record'](setup_record(entry='entry-existing', revision=2))
    value = setup_view(record)
    value.update(candidate_state='unknown', device_count=None, entity_count=None, available_entity_count=None,
                 options_truncated=True, devices_truncated=True)
    for source in value['sources']:
        if source['id'] in {'ha_devices', 'ha_entities'}:
            source.update(state='partial', truncated=True)
        elif source['id'] == 'ha_states':
            source.update(state='unavailable', observed_at=None)
    state['views'][SETUP_ID] = value
    container = open_tracking(page, state)
    result = open_saved(container)
    expect(result).to_contain_text('已載入')
    expect(result).to_contain_text('實體')
    assert '0 個' not in result.inner_text() and '0 台' not in result.inner_text()
    assert '接入成功' not in result.inner_text()
    assert tracking_requests(state, 'POST') == []


def test_device_display_truncation_does_not_erase_complete_source_counts(setup_browser):
    page, state = setup_browser
    record = state['add_record'](setup_record(entry='entry-existing', revision=2))
    value = setup_view(record)
    value.update(devices_truncated=True, device_count=137, entity_count=278, available_entity_count=201)
    state['views'][SETUP_ID] = value
    container = open_tracking(page, state)
    result = open_saved(container)
    expect(result).to_contain_text('137 筆設備紀錄')
    expect(result).to_contain_text('201 / 278 個功能顯示可用狀態')
    expect(result).to_contain_text('尚未進行實體操作驗證')
    assert tracking_requests(state, 'POST') == []


def test_removed_selected_entry_does_not_switch_to_another_same_named_entry(setup_browser):
    page, state = setup_browser
    record = state['add_record'](setup_record(entry='entry-existing', revision=2))
    value = setup_view(record)
    value.update(integration_state='not_seen', integration=None, devices=[], device_count=None,
                 entity_count=None, available_entity_count=None)
    value['options'] = value['options'][1:]
    state['views'][SETUP_ID] = value
    container = open_tracking(page, state)
    result = open_saved(container)
    expect(container.get_by_role('radio')).to_have_count(2)
    expect(container.locator('input[type=radio]:checked')).to_have_count(0)
    assert state['records'][SETUP_ID]['selected_entry_id'] == 'entry-existing'
    assert '接入成功' not in result.inner_text()
    assert tracking_requests(state, 'POST') == []


def test_revision_conflict_does_not_retry_or_replace_newer_selection_automatically(setup_browser):
    page, state = setup_browser
    record = state['add_record']()
    container = open_tracking(page, state)
    open_saved(container)
    choose_entry(container)
    record.update(selected_entry_id='entry-existing', selected_at=OBSERVED, revision=2)
    container.get_by_role('button', name='保存整合選擇', exact=True).click()
    expect(container.get_by_role('alert')).to_contain_text('追蹤已在其他頁面變更')
    page.wait_for_timeout(100)
    assert len(tracking_requests(state, 'POST')) == 1
    assert state['commits'] == []
    assert record['selected_entry_id'] == 'entry-existing'
    container.get_by_role('button', name='重新讀取接入', exact=True).click()
    expect(container.get_by_role('button', name='重新讀取接入', exact=True)).to_be_enabled()
    choose_entry(container)
    container.get_by_role('button', name='保存整合選擇', exact=True).click()
    expect(container.get_by_role('button', name='重新讀取接入', exact=True)).to_be_enabled()
    assert tracking_requests(state, 'POST')[-1]['body'] == {'expected_revision': 2, 'entry_id': 'entry-new'}
    assert len(state['commits']) == 1


@pytest.mark.parametrize('between', ['none', 'refresh', 'collapse'])
def test_lost_create_response_retries_exact_key_and_body_without_duplicate_tracking(setup_browser, between):
    page, state = setup_browser
    container = open_tracking(page, state)
    state['mutation_mode'] = 'lost_after_commit'
    container.get_by_role('button', name=f'追蹤接入 {CANDIDATE}', exact=True).click()
    retry = container.get_by_role('button', name='重試本次操作', exact=True)
    expect(retry).to_be_visible()
    assert len(tracking_requests(state, 'POST')) == 1
    assert len(state['records']) == 1 and len(state['commits']) == 1
    if between == 'refresh':
        container.get_by_role('button', name='重新讀取接入', exact=True).click()
        expect(container.get_by_role('button', name='重新讀取接入', exact=True)).to_be_enabled()
    elif between == 'collapse':
        summary = container.locator('summary').filter(has_text='接入進度')
        summary.click()
        expect(retry).not_to_be_visible()
        summary.click()
        expect(container.get_by_role('button', name='重新讀取接入', exact=True)).to_be_enabled()
    expect(retry).to_be_visible()
    assert len(tracking_requests(state, 'POST')) == 1
    retry.click()
    expect(container.get_by_role('region', name='接入讀回')).to_be_visible()
    writes = tracking_requests(state, 'POST')
    assert len(writes) == 2 and writes[0] == writes[1]
    assert len(state['records']) == 1 and len(state['commits']) == 1
    assert_only_tracking_writes(state)


@pytest.mark.parametrize('mismatch', ['older_revision', 'different_entry', 'different_candidate'])
def test_acknowledged_write_rejects_mismatched_readback_without_resubmitting(setup_browser, mismatch):
    page, state = setup_browser
    record = setup_record()
    if mismatch != 'different_candidate':
        state['add_record'](record)
    container = open_tracking(page, state)
    if mismatch != 'different_candidate':
        open_saved(container)
        choose_entry(container)
    if mismatch == 'different_entry':
        record.update(revision=2, selected_entry_id='entry-existing', selected_at=OBSERVED)
    elif mismatch == 'different_candidate':
        record['candidate']['id'] = 'unrelated-candidate'
    state['views'][SETUP_ID] = setup_view(record)
    if mismatch == 'different_candidate':
        container.get_by_role('button', name=f'追蹤接入 {CANDIDATE}', exact=True).click()
    else:
        container.get_by_role('button', name='保存整合選擇', exact=True).click()
    expect(container.get_by_role('alert')).to_contain_text('進度已保存，但暫時無法讀回')
    expect(container.get_by_role('region', name='接入讀回')).to_have_count(0)
    expect(container.get_by_role('button', name='重試本次操作', exact=True)).to_have_count(0)
    assert len(tracking_requests(state, 'POST')) == 1
    assert len(state['commits']) == 1
    del state['views'][SETUP_ID]
    container.get_by_role('button', name='重新讀取接入', exact=True).click()
    expect(container.get_by_role('region', name='接入讀回')).to_be_visible()
    assert len(tracking_requests(state, 'POST')) == 1
    if mismatch != 'different_candidate':
        expect(container.locator('.discovery-setup-selected-id')).to_have_text('entry-new')
    assert_only_tracking_writes(state)


def test_forget_requires_explicit_confirmation_and_only_removes_tracking(setup_browser):
    page, state = setup_browser
    state['add_record'](setup_record(entry='entry-existing', revision=2))
    snapshot = copy.deepcopy(state['snapshot'])
    container = open_tracking(page, state)
    open_saved(container)
    container.get_by_role('button', name='停止追蹤', exact=True).click()
    confirm = container.get_by_role('button', name='確認停止追蹤', exact=True)
    expect(confirm).to_be_visible()
    assert tracking_requests(state, 'POST') == []
    confirm.click()
    expect(container.get_by_text('還沒有保存的接入進度。', exact=True)).to_be_visible()
    expect(container.get_by_role('button', name=f'查看接入 {CANDIDATE}', exact=True)).to_have_count(0)
    assert state['records'] == {}
    assert state['snapshot'] == snapshot
    assert [(request['path'], request['body']) for request in tracking_requests(state, 'POST')] == [
        (f'discovery/setups/{SETUP_ID}/forget', {'expected_revision': 2}),
    ]
    assert_only_tracking_writes(state)


@pytest.mark.parametrize('broken', [
    'version', 'native_path', 'physical_verification', 'record_identity', 'count',
    'candidate_state_type', 'integration_state_type',
])
def test_tracking_rejects_unknown_or_mismatched_readback_contract(setup_browser, broken):
    page, state = setup_browser
    record = state['add_record']()
    value = setup_view(record)
    if broken == 'version':
        value['contract_version'] = 2
    elif broken == 'native_path':
        value['native_path'] = 'https://untrusted.invalid/login'
    elif broken == 'physical_verification':
        value['physical_verification'] = 'passed'
    elif broken == 'record_identity':
        value['setup']['id'] = 'b' * 32
    elif broken == 'candidate_state_type':
        value['candidate_state'] = ['present']
    elif broken == 'integration_state_type':
        value['integration_state'] = ['unselected']
    else:
        value['device_count'] = -1
    state['views'][SETUP_ID] = value
    container = open_tracking(page, state)
    container.get_by_role('button', name=f'查看接入 {CANDIDATE}', exact=True).click()
    expect(container.get_by_role('alert')).to_contain_text('格式')
    expect(container.get_by_role('region', name='接入讀回')).to_have_count(0)
    expect(container.get_by_role('link', name='開啟家庭設定', exact=False).first).to_have_attribute(
        'href', 'http://127.0.0.1:8123/config/integrations',
    )
    assert tracking_requests(state, 'POST') == []


def test_tracking_refresh_failure_is_visible_and_can_be_read_again_without_mutation(setup_browser):
    page, state = setup_browser
    state['add_record'](setup_record(entry='entry-existing', revision=2))
    container = open_tracking(page, state)
    open_saved(container)
    path = f'discovery/setups/{SETUP_ID}'
    state['overrides'][path] = (503, {'detail': 'discovery_setup_source_unavailable'})
    container.get_by_role('button', name='重新讀取接入', exact=True).click()
    expect(container.get_by_role('alert')).to_contain_text('家庭探索資料暫時無法確認')
    expect(container.get_by_text('以下是上次讀回資料；目前狀態尚未重新確認。', exact=True)).to_be_visible()
    assert tracking_requests(state, 'POST') == []
    del state['overrides'][path]
    container.get_by_role('button', name='重新讀取接入', exact=True).click()
    expect(container.get_by_role('region', name='接入讀回')).to_be_visible()
    expect(container.get_by_role('alert')).to_have_count(0)
    assert tracking_requests(state, 'POST') == []


@pytest.mark.parametrize('boundary', ['surface', 'identity', 'host_session'])
def test_late_tracking_response_cannot_restore_an_old_surface_or_household(setup_browser, boundary):
    page, state = setup_browser
    record = setup_record()
    record['candidate']['title'] = '舊家庭的私人追蹤名稱'
    state['add_record'](record)
    mode = 'bridge' if boundary == 'identity' else 'host'
    container = open_tracking(page, state, mode)
    path = f'discovery/setups/{SETUP_ID}'
    state['hold_paths'].add(('GET', path))
    container.get_by_role('button', name='查看接入 舊家庭的私人追蹤名稱', exact=True).click()
    expect(container.get_by_role('button', name='重新讀取接入', exact=True)).to_be_disabled()
    assert len(state['held']) == 1
    reads_before_leave = len(tracking_requests(state, 'GET'))
    if boundary == 'surface':
        page.evaluate("location.hash = 'gateway'")
        expect(page.get_by_label('對話訊息', exact=True)).to_be_visible()
        release_held(state)
        page.evaluate("location.hash = 'inventory'")
        expect(page.get_by_role('region', name='探索設備')).to_be_visible()
        expect(page.get_by_role('region', name='接入讀回')).to_have_count(0)
        assert len(tracking_requests(state, 'GET')) == reads_before_leave
    else:
        event = 'agentic-bridge-expired' if boundary == 'identity' else 'agentic-session-expired'
        page.evaluate('(event)=>window.dispatchEvent(new Event(event))', event)
        if boundary == 'identity':
            expect(page.get_by_role('region', name='管家連線')).to_be_visible()
        else:
            expect(page.get_by_role('heading', name='登入主機', exact=True)).to_be_visible()
        state['scope'] = '2' * 64
        state['hold_paths'].clear()
        if boundary == 'identity':
            page.get_by_role('button', name='重新連線', exact=True).click()
        else:
            page.get_by_label('主機登入金鑰', exact=True).fill('fixture-login-key')
            page.get_by_role('button', name='開啟管理中心', exact=True).click()
        container = open_tracking(page, state, mode, navigate=False)
        expect(container.get_by_role('button', name=f'追蹤接入 {CANDIDATE}', exact=True)).to_be_visible()
        release_held(state)
        page.wait_for_timeout(100)
        expect(container.get_by_text('舊家庭的私人追蹤名稱', exact=True)).to_have_count(0)
        expect(container.get_by_role('region', name='接入讀回')).to_have_count(0)
    assert tracking_requests(state, 'POST') == []


@pytest.mark.parametrize('mode', ['host', 'bridge'])
def test_tracking_responsive_controls_and_keyboard_keep_the_owner_in_context(setup_browser, tmp_path, mode):
    page, state = setup_browser
    state['add_record']()
    evidence = Path(os.environ.get('DISCOVERY_SETUP_UI_EVIDENCE', str(tmp_path / 'screenshots')))
    evidence.mkdir(parents=True, exist_ok=True)
    for width in (390, 768, 1440):
        page.set_viewport_size({'width': width, 'height': 900 if width == 1440 else 844})
        container = open_tracking(page, state, mode)
        open_saved(container)
        radio = container.locator('input[type=radio][value="entry-new"]')
        radio.focus()
        radio.press('Space')
        expect(radio).to_be_checked()
        button = container.get_by_role('button', name='保存整合選擇', exact=True)
        button.scroll_into_view_if_needed()
        bounds = button.bounding_box()
        assert bounds and bounds['height'] >= 44
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(evidence / f'tracking-{mode}-{width}.png'), full_page=True)
        if mode == 'bridge':
            dialog = page.get_by_role('dialog', name='分享探索結果', exact=True)
            for _ in range(25):
                page.keyboard.press('Tab')
                assert dialog.evaluate('(element)=>element.contains(document.activeElement)')
            page.keyboard.press('Escape')
            expect(dialog).not_to_be_visible()
            expect(page.get_by_role('button', name='分享探索結果', exact=True)).to_be_focused()
            expect(page.get_by_label('對話訊息', exact=True)).to_be_in_viewport()
        assert tracking_requests(state, 'POST') == []


@pytest.mark.parametrize('step', ['link', 'unknown_step'])
def test_tracking_parses_synology_link_record_next_to_other_candidates(setup_browser, step):
    page, state = setup_browser
    candidate = {**state['snapshot']['candidates'][0], 'id': 'synthetic-nas',
                 'title': '合成 NAS', 'integration_domain': 'synology_dsm',
                 'discovery_method': 'ssdp', 'confirmation_step': 'link'}
    state['snapshot']['candidates'].append(candidate)
    record = setup_record()
    record['candidate'] = {**candidate, 'confirmation_step': step}
    state['add_record'](record)
    value = setup_view(record)
    for option in value['options']:
        option['integration']['domain'] = 'synology_dsm'
    state['views'][SETUP_ID] = value
    container = open_tracking(page, state)
    expect(container.get_by_role('button', name=f'追蹤接入 {CANDIDATE}', exact=True)).to_be_visible()
    expect(container.get_by_role('button', name='追蹤接入 合成 NAS', exact=True)).to_be_visible()
    if step == 'link':
        result = open_saved(container, '合成 NAS')
        expect(result.get_by_text('合成 NAS', exact=True)).to_be_visible()
        expect(container.get_by_role('alert')).to_have_count(0)
    else:
        expect(container.get_by_role('alert')).to_contain_text('格式')
        expect(container.get_by_role('button', name='查看接入 合成 NAS', exact=True)).to_have_count(0)
    assert tracking_requests(state, 'POST') == []
