"""Browser contracts using synthetic discovery data; no LAN or household acceptance."""
import copy
import json
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright


def observation():
    sources = []
    for name in ['ha_discovery', 'ha_devices', 'ha_entities', 'ha_states', 'ha_integrations',
                 'ha_config', 'router_inventory', 'lan_probe', 'account_inventory']:
        supported = name.startswith('ha_')
        sources.append({'id': name, 'state': 'ok' if supported else 'not_implemented',
                        'location': 'home_assistant' if supported else 'not_implemented',
                        'method': 'websocket' if supported else 'not_implemented', 'target': name,
                        'adapter_version': 1, 'upstream_version': 'fixture' if supported else None,
                        'observed_at': '2026-10-16T12:00:00Z' if supported else None,
                        'item_count': 1 if supported else None, 'truncated': False,
                        'reason_code': None, 'content_digest': 'a' * 64 if supported else None})
    return {'contract_version': 1, 'observed_at': '2026-10-16T12:00:00Z',
            'content_digest': 'b' * 64, 'native_path': '/config/integrations',
            'sources': sources, 'limitations': ['not_a_complete_lan_inventory'],
            'candidates': [{'id': 'fixture-candidate', 'integration_domain': 'fixture_bridge',
                            'title': '待連接的燈光橋接器（介面測試資料）', 'discovery_method': 'zeroconf',
                            'source_id': 'ha_discovery', 'native_path': '/config/integrations'}],
            'devices': [{'id': 'fixture-hub', 'name': '燈光橋接器（介面測試資料）',
                         'manufacturer': '測試廠商', 'model': 'Fixture Hub', 'via_device_id': None,
                         'parent_device_id': None, 'integration_ids': ['fixture-integration'],
                         'disabled': False, 'entity_count': 0, 'available_entity_count': 0,
                         'source_id': 'ha_devices'},
                        {'id': 'fixture-lamp', 'name': '書桌燈（介面測試資料）',
                         'manufacturer': '測試廠商', 'model': 'Fixture Lamp', 'via_device_id': 'fixture-hub',
                         'parent_device_id': None, 'integration_ids': ['fixture-integration'],
                         'disabled': False, 'entity_count': 1, 'available_entity_count': 1,
                         'source_id': 'ha_devices'}],
            'integrations': [{'id': 'fixture-integration', 'title': '燈光整合', 'domain': 'fixture_bridge',
                              'state': 'loaded', 'source_id': 'ha_integrations'}]}


@pytest.fixture
def discovery_browser():
    root = Path(__file__).parents[1]
    server = ThreadingHTTPServer(('127.0.0.1', 0),
                                 partial(SimpleHTTPRequestHandler, directory=str(root / 'controller/static')))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state = {'snapshot': observation(), 'status': 200, 'hold': False, 'held': [],
             'requests': [], 'errors': [], 'mode': 'host'}

    def route(intercept):
        request = intercept.request
        path = request.url.split('/api/')[1]
        state['requests'].append((request.method, path, request.post_data_json if request.method == 'POST' else None))
        status = 200
        if path == 'discovery':
            if state['hold']:
                state['held'].append(intercept)
                return
            status, result = state['status'], state['snapshot']
        elif path == 'deployment':
            result = {'mode': state['mode'], 'authenticated': True}
        elif path == 'session':
            result = {'principal': 'fixture-owner', 'csrf': 'fixture-csrf'}
        elif path == 'status':
            result = {'ha': {'status': 'connected'}, 'runner': {'protocol': 1, 'providers': [
                {'provider': 'codex', 'status': 'connected'}]}, 'gateway': {'selected': True},
                'onboarding': {'policy_confirmed': True}, 'policy_readable': 1}
        elif path == 'tasks':
            result = {'tasks': []}
        elif path == 'inventory':
            result = {'entities': [{'entity_id': 'light.fixture_desk', 'name': '書桌燈（介面測試資料）',
                                   'domain': 'light', 'area': '工作室', 'state': 'off',
                                   'availability': 'available', 'observed_at': '2026-10-16T12:00:00Z',
                                   'actions': ['turn_on', 'turn_off']}], 'policy': {'entities': []}}
        else:
            result = {}
        intercept.fulfill(status=status, content_type='application/json', body=json.dumps(result))

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 900})
            page.on('pageerror', lambda error: state['errors'].append(str(error)))
            page.route('**/api/**', route)
            state['url'] = f'http://127.0.0.1:{server.server_port}/#inventory'
            yield page, state
            assert not state['errors'], state['errors']
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_discovery_handoff_coverage_responsive_and_permission_draft(discovery_browser, tmp_path):
    page, state = discovery_browser
    page.goto(state['url'])
    panel = page.get_by_role('region', name='探索設備')
    expect(panel.get_by_text('待連接的燈光橋接器（介面測試資料）', exact=True)).to_be_visible()
    native = panel.get_by_role('link', name='開啟家庭設定')
    expect(native).to_have_attribute('href', 'http://127.0.0.1:8123/config/integrations')
    expect(native).to_have_attribute('target', '_blank')
    assert not any(method == 'POST' for method, _, _ in state['requests'])
    evidence = Path(os.environ.get('DISCOVERY_UI_EVIDENCE', str(tmp_path / 'screenshots')))
    evidence.mkdir(parents=True, exist_ok=True)
    for width in (1440, 768, 390):
        height = 900 if width == 1440 else 844
        page.set_viewport_size({'width': width, 'height': height})
        page.evaluate('window.scrollTo(0,0)')
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        button = native.bounding_box()
        assert button and button['height'] >= 44 and 0 <= button['y'] < height - button['height']
        page.screenshot(path=str(evidence / f'after-{width}.png'), full_page=True)
    panel.locator('summary').filter(has_text='盤點範圍與來源').click()
    for source in ['路由器設備清單', '內網主動探測']:
        expect(panel.locator('.discovery-sources li').filter(has_text=source)).to_have_count(0)
    expect(panel.locator('.discovery-sources li').filter(has_text='獨立品牌帳號盤點')).to_contain_text('尚未接入')
    expect(panel.locator('.network-inventory > summary')).to_have_text('探索更多設備')
    panel.locator('summary').filter(has_text='已登錄設備').click()
    expect(panel.get_by_text('經由 燈光橋接器（介面測試資料） 連接')).to_be_visible()
    expect(panel.get_by_text('整合：燈光整合（已載入）')).to_have_count(2)
    page.screenshot(path=str(evidence / 'after-details-390.png'), full_page=True)
    share = page.get_by_role('checkbox', name='分享 light.fixture_desk')
    share.check()
    page.get_by_role('combobox', name='light.fixture_desk 操作權限').select_option('lighting')
    page.get_by_label('light.fixture_desk 用途').fill('書桌照明')
    before_inventory = sum(path == 'inventory' for _, path, _ in state['requests'])
    state['snapshot']['candidates'] = []
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(panel.get_by_text('目前沒有待設定的探索結果；可到家庭設定新增整合。')).to_be_visible()
    expect(share).to_be_checked()
    expect(page.get_by_label('light.fixture_desk 用途')).to_have_value('書桌照明')
    expect(page.get_by_role('button', name='重新盤點', exact=True)).to_be_disabled()
    assert sum(path == 'inventory' for _, path, _ in state['requests']) == before_inventory
    assert not any(method == 'POST' for method, _, _ in state['requests'])
    page.get_by_role('button', name='儲存修改', exact=True).click()
    expect(page.get_by_role('status').filter(has_text='已儲存。受影響的提案需要重新產生。')).to_be_visible()
    assert [data for method, path, data in state['requests'] if method == 'POST' and path == 'policy'] == [
        {'entities': [{'entity_id': 'light.fixture_desk', 'read': True, 'write': 'lighting', 'purpose': '書桌照明'}]}]


def test_discovery_partial_sources_do_not_claim_empty_or_control_ready(discovery_browser):
    page, state = discovery_browser
    state['snapshot']['candidates'] = []
    state['snapshot']['sources'][0]['state'] = 'unsupported'
    state['snapshot']['sources'][3]['state'] = 'unavailable'
    state['snapshot']['devices'][1]['available_entity_count'] = None
    state['snapshot']['integrations'][0]['state'] = 'error'
    page.goto(state['url'])
    panel = page.get_by_role('region', name='探索設備')
    expect(panel.get_by_text('部分來源尚未完整讀取，請查看盤點範圍。')).to_be_visible()
    expect(panel.get_by_text('探索清單尚未完整取得，請開啟家庭設定確認。')).to_be_visible()
    expect(panel.get_by_text('目前沒有待設定', exact=False)).to_have_count(0)
    panel.locator('summary').filter(has_text='已登錄設備').click()
    expect(panel.get_by_text('1 個功能 · 可用狀態尚未取得')).to_be_visible()
    expect(panel.get_by_text('整合：燈光整合（設定異常）')).to_have_count(2)
    expect(page.get_by_role('checkbox', name='分享 light.fixture_desk')).not_to_be_checked()


def test_discovery_loading_failure_and_retry_are_distinct(discovery_browser):
    page, state = discovery_browser
    state['hold'] = True
    page.goto(state['url'])
    panel = page.get_by_role('region', name='探索設備')
    expect(panel.get_by_text('正在讀取家庭探索結果…')).to_be_visible()
    expect(panel.get_by_text('目前沒有待設定', exact=False)).to_have_count(0)
    expect(page.get_by_role('checkbox', name='分享 light.fixture_desk')).to_be_visible()
    state['hold'] = False
    for pending in state['held']:
        pending.fulfill(status=503, content_type='application/json', body=json.dumps({'detail': '家庭暫時無法連接。'}))
    expect(panel.get_by_role('alert')).to_contain_text('家庭暫時無法連接。')
    panel.get_by_role('button', name='重試探索').click()
    expect(panel.get_by_text('待連接的燈光橋接器（介面測試資料）', exact=True)).to_be_visible()
    state['status'] = 503
    state['snapshot'] = {'detail': '家庭暫時無法連接。'}
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(panel.get_by_role('alert')).to_be_visible()
    expect(panel.get_by_text('待連接的燈光橋接器（介面測試資料）', exact=True)).to_have_count(0)
    expect(panel.get_by_text('目前沒有待設定', exact=False)).to_have_count(0)


@pytest.mark.parametrize('mutation', ['empty', 'version', 'redirect', 'source_state', 'missing_source',
                                     'candidate_link', 'duplicate', 'null_device', 'unknown_integration'])
def test_discovery_unknown_contract_fails_visibly(discovery_browser, mutation):
    page, state = discovery_browser
    value = state['snapshot']
    if mutation == 'empty':
        state['snapshot'] = {}
    elif mutation == 'version':
        value['contract_version'] = 2
    elif mutation == 'redirect':
        value['native_path'] = 'https://untrusted.invalid/'
    elif mutation == 'source_state':
        value['sources'][0]['state'] = 'new_state'
    elif mutation == 'missing_source':
        value['sources'].pop(0)
    elif mutation == 'candidate_link':
        value['candidates'][0]['native_path'] = '//untrusted.invalid'
    elif mutation == 'duplicate':
        value['devices'].append(copy.deepcopy(value['devices'][0]))
    elif mutation == 'null_device':
        value['devices'].append(None)
    elif mutation == 'unknown_integration':
        value['integrations'][0]['state'] = 'new_state'
    page.goto(state['url'])
    panel = page.get_by_role('region', name='探索設備')
    expect(panel.get_by_role('alert')).to_contain_text('探索資料格式已變更')
    expect(panel.get_by_text('目前沒有待設定', exact=False)).to_have_count(0)
    expect(panel.get_by_role('link', name='開啟家庭設定')).to_have_attribute(
        'href', 'http://127.0.0.1:8123/config/integrations')
    expect(page.get_by_role('checkbox', name='分享 light.fixture_desk')).to_be_visible()


def test_discovery_bounded_lists_and_ingress_handoff(discovery_browser):
    page, state = discovery_browser
    state['mode'] = 'ingress'
    value = state['snapshot']
    value['future_field'] = {'compatible': True}
    value['candidates'] = [{**value['candidates'][0], 'id': f'candidate-{i}', 'title': f'候選 {i}'} for i in range(10)]
    value['devices'] = [{**value['devices'][1], 'id': f'device-{i}', 'name': f'已登錄 {i}'} for i in range(42)]
    page.goto(state['url'])
    panel = page.get_by_role('region', name='探索設備')
    expect(panel.locator('.discovery-candidates li')).to_have_count(3)
    expect(panel.get_by_text('另有 7 項，請開啟家庭設定查看。')).to_be_visible()
    expect(panel.get_by_role('link', name='開啟家庭設定')).to_have_attribute('href', '/config/integrations')
    expect(panel.get_by_role('link', name='開啟家庭設定')).to_have_attribute('target', '_top')
    panel.locator('summary').filter(has_text='已登錄設備').click()
    expect(panel.locator('.discovery-devices li')).to_have_count(20)
    panel.get_by_role('button', name='再顯示 20 台').click()
    expect(panel.locator('.discovery-devices li')).to_have_count(40)
    panel.get_by_role('button', name='再顯示 20 台').click()
    expect(panel.locator('.discovery-devices li')).to_have_count(42)
    expect(panel.get_by_role('button', name='再顯示 20 台')).to_have_count(0)
    assert not any(method == 'POST' for method, _, _ in state['requests'])


@pytest.mark.parametrize('step', ['link', 'unknown_step'])
def test_synology_link_candidate_uses_shared_discovery_steps(discovery_browser, step):
    page, state = discovery_browser
    candidate = {**state['snapshot']['candidates'][0], 'id': 'synthetic-nas',
                 'title': '合成 NAS', 'integration_domain': 'synology_dsm',
                 'discovery_method': 'ssdp', 'confirmation_step': step}
    state['snapshot']['candidates'].append(candidate)
    page.goto(state['url'])
    panel = page.get_by_role('region', name='探索設備')
    if step == 'link':
        expect(panel.get_by_text('合成 NAS', exact=True)).to_be_visible()
        expect(panel.get_by_text('待連接的燈光橋接器（介面測試資料）', exact=True)).to_be_visible()
        expect(panel.get_by_role('alert')).to_have_count(0)
    else:
        expect(panel.get_by_role('alert')).to_contain_text('格式已變更')
        expect(panel.locator('.discovery-candidates')).to_have_count(0)
    assert not any(method == 'POST' for method, _, _ in state['requests'])
