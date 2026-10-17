"""Compiled product network inventory; all network/household observations are fixtures."""

import copy
import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import expect
from test_discovery_setup_ui import (
    OBSERVED,
    SETUP_ID,
    setup_browser,  # noqa: F401 — shared compiled-product browser transport
    setup_record,
    setup_view,
)
from test_site import browser as browser
from test_site import origin as origin

SCOPE = 'fixture-home-network'
NAME = '書房服務設備（合成資料）'
SOURCE_IDS = ('lan_neighbors', 'lan_mdns', 'lan_ssdp', 'router_import')


def inventory_snapshot(populated=False, revision=0):
    sources = [dict(id=identifier, adapter_version=1, state='ok' if populated and index < 3 else 'not_configured',
                    method=('neighbor_table', 'mdns', 'ssdp', 'json_import')[index],
                    target='合成網路 demo0' if index < 3 else '尚未指定來源',
                    observed_at=OBSERVED if populated and index < 3 else None,
                    item_count=(1 if index < 2 else 0) if populated and index < 3 else None,
                    truncated=False, reason_code=None, content_digest='a' * 64 if populated else None,
                    limitations=['synthetic_protocol_observation'])
               for index, identifier in enumerate(SOURCE_IDS)]
    rows = [dict(id='fixture-service', source_id='lan_mdns', scope_id=SCOPE,
                 address='192.168.50.30', mac='02:00:00:00:00:30', name=NAME,
                 services=['_shelly._tcp.local.'], observed_at=OBSERVED, source_updated_at=None,
                 reachability='responded', metadata={'model': 'synthetic-device'}),
            dict(id='fixture-unknown', source_id='lan_neighbors', scope_id=SCOPE,
                 address='192.168.50.31', mac=None, name='未辨識設備（合成資料）',
                 services=[], observed_at=OBSERVED, source_updated_at=None,
                 reachability='unknown', metadata={})] if populated else []
    recommendations = [dict(id='fixture-recommendation', observation_id='fixture-service',
                            integration_domain='fixture_bridge', title='服務接入建議（合成資料）',
                            confidence='service_match', reason='服務公告有相符線索',
                            next_step='先核對型號，再在家庭設定對應整合。',
                            documentation_url='https://www.home-assistant.io/integrations/fixture_bridge/',
                            native_path='/config/integrations')] if populated else []
    return dict(contract_version=1, revision=revision, observed_at=OBSERVED,
                expires_at='2099-10-17T12:00:00Z' if populated else None,
                sources=sources, observations=rows, recommendations=recommendations,
                limitations=['scope_only', 'nat_and_vlan_limit'], content_digest='b' * 64)


@pytest.fixture
def inventory_browser(setup_browser):  # noqa: F811
    page, state = setup_browser
    state.update(network=inventory_snapshot(), network_errors={}, network_held=[],
                 network_hold=set(), network_replays={}, network_commits=[], network_lost=False,
                 network_scopes={'contract_version': 1, 'scopes': [
                     {'id': SCOPE, 'label': '合成家庭網路', 'interface': 'demo0',
                      'address': '192.168.50.10', 'network': '192.168.50.0/24'}],
                     'limitations': ['synthetic_protocol_observation']})

    def handle(route):
        request = route.request
        path = request.url.split('/api/', 1)[1]
        body = request.post_data_json if request.method == 'POST' else None
        key = request.headers.get('idempotency-key')
        state['requests'].append({'method': request.method, 'path': path, 'body': body, 'key': key})
        status, result = state['network_errors'].get((request.method, path), (200, None))
        if result is None:
            if request.method == 'GET':
                result = state['network_scopes'] if path.endswith('/scopes') else state['network']
            else:
                assert request.headers.get('x-agentic-csrf') == 'fixture-csrf'
                assert key
                replay = state['network_replays'].get((path, key))
                if replay:
                    assert replay[0] == body
                    result = replay[1]
                else:
                    revision = state['network']['revision'] + 1
                    if path.endswith('/scan'):
                        assert body == {'scope_id': SCOPE}
                        state['network'] = inventory_snapshot(True, revision)
                    elif path.endswith('/import'):
                        snapshot = inventory_snapshot(False, revision)
                        source = snapshot['sources'][-1]
                        source.update(state='ok', method=body['format'] + '_import', target=body['label'],
                                      observed_at=OBSERVED, item_count=1, content_digest='c' * 64)
                        row = copy.deepcopy(inventory_snapshot(True)['observations'][1])
                        row.update(id='fixture-imported', source_id='router_import', scope_id='import-scope',
                                   name='匯入的設備（合成資料）')
                        snapshot['observations'] = [row]
                        state['network'] = snapshot
                    elif path.endswith('/clear'):
                        assert body == {'expected_revision': state['network']['revision']}
                        state['network'] = inventory_snapshot(False, revision)
                    else:
                        raise AssertionError(path)
                    state['network_commits'].append((path, copy.deepcopy(body)))
                    result = {'contract_version': 1, 'revision': revision}
                    state['network_replays'][(path, key)] = (copy.deepcopy(body), result)
        result = copy.deepcopy(result)
        if (request.method, path) in state['network_hold']:
            state['network_held'].append((route, status, result))
            return
        if request.method == 'POST' and state['network_lost']:
            state['network_lost'] = False
            route.abort('failed')
            return
        route.fulfill(status=status, content_type='application/json', body=json.dumps(result))

    page.route('**/api/discovery/inventory**', handle)
    yield page, state
    for route, status, result in state['network_held']:
        route.fulfill(status=status, content_type='application/json', body=json.dumps(result))


def open_inventory(page, state, mode='host', navigate=True):
    state['mode'] = mode
    if navigate:
        page.goto('about:blank')
        page.goto(state['base'] + ('#gateway' if mode == 'bridge' else '#inventory'))
    container = page.get_by_role('region', name='探索設備')
    if mode == 'bridge':
        page.get_by_role('button', name='分享探索結果', exact=True).click()
        container = page.get_by_role('dialog', name='分享探索結果')
    container.locator('summary').filter(has_text='探索更多設備').click()
    return container.locator('.network-inventory')


def network_requests(state, method=None):
    return [request for request in state['requests'] if request['path'].startswith('discovery/inventory')
            and (method is None or request['method'] == method)]


def scan(panel):
    panel.get_by_label('探索的主機網路', exact=True).select_option(SCOPE)
    panel.get_by_role('button', name='開始探索', exact=True).click()
    expect(panel.get_by_text(NAME, exact=True)).to_be_visible()


def test_inventory_lazy_requires_actual_scope_and_preserves_source_observations(inventory_browser):
    page, state = inventory_browser
    page.goto(state['base'] + '#inventory')
    expect(page.get_by_role('heading', name='探索設備')).to_be_visible()
    assert network_requests(state) == []
    panel = open_inventory(page, state, navigate=False)
    expect(panel.get_by_role('button', name='開始探索')).to_be_disabled()
    expect(panel.get_by_label('探索的主機網路')).to_have_value('')
    assert network_requests(state, 'POST') == []
    scan(panel)
    assert network_requests(state, 'POST')[0]['body'] == {'scope_id': SCOPE}
    expect(panel.get_by_role('heading', name='觀測結果 · 2 筆')).to_be_visible()
    expect(panel.get_by_text('同一設備可能有多筆觀測，尚未合併身分。')).to_be_visible()
    expect(panel.get_by_text('尚未辨識接入方式；可分享給管家核對品牌或官方整合。')).to_be_visible()
    expect(panel.get_by_text('服務線索相符，仍待確認 · 服務公告有相符線索')).to_be_visible()
    panel.locator('summary').filter(has_text='涵蓋範圍與來源').click()
    expect(panel.get_by_text('只涵蓋所選主機網路與匯入清單。跨網段、訪客隔離、NAT 與離線設備可能看不到。')).to_be_visible()
    expect(panel.get_by_text('建議有效至', exact=False)).to_be_visible()
    panel.get_by_role('button', name='重新讀取盤點', exact=True).click()
    expect(panel.get_by_text(NAME, exact=True)).to_be_visible()
    assert len(network_requests(state, 'POST')) == 1
    page.reload()
    expect(page.locator('.network-inventory>summary')).to_be_visible()
    assert len(network_requests(state, 'GET')) == 4
    panel = open_inventory(page, state, navigate=False)
    expect(panel.get_by_text(NAME, exact=True)).to_be_visible()


@pytest.mark.parametrize('format,filename,content', [
    ('json', 'devices.json', '[{"ip":"192.168.50.31","hostname":"test"}]'),
    ('csv', 'devices.csv', 'ip,mac,hostname\n192.168.50.31,,test\n'),
    ('dnsmasq', 'dnsmasq.leases', '0 02:00:00:00:00:31 192.168.50.31 test *\n'),
])
def test_inventory_import_file_is_explicit_bounded_and_read_back(inventory_browser, format, filename, content):
    page, state = inventory_browser
    panel = open_inventory(page, state)
    panel.get_by_role('button', name='匯入清單', exact=True).click()
    panel.get_by_label('選擇清單檔案', exact=True).set_input_files(
        {'name': filename, 'mimeType': 'text/plain', 'buffer': content.encode()})
    expect(panel.get_by_label('檔案格式', exact=True)).to_have_value(format)
    assert network_requests(state, 'POST') == []
    panel.get_by_label('清單名稱', exact=True).fill('合成路由器匯出')
    panel.get_by_role('button', name='匯入設備清單', exact=True).click()
    expect(panel.get_by_text('匯入的設備（合成資料）', exact=True)).to_be_visible()
    writes = network_requests(state, 'POST')
    assert len(writes) == 1
    assert writes[0]['body'] == {'format': format, 'label': '合成路由器匯出', 'content': content}
    assert writes[0]['key']
    expect(panel.get_by_role('button', name='匯入設備清單', exact=True)).to_be_disabled()
    assert not [request for request in state['requests'] if request['method'] == 'POST'
                and not request['path'].startswith('discovery/inventory')]


def test_inventory_rejects_oversize_file_and_multibyte_paste_before_post(inventory_browser):
    page, state = inventory_browser
    panel = open_inventory(page, state)
    panel.get_by_role('button', name='匯入清單', exact=True).click()
    panel.get_by_label('選擇清單檔案', exact=True).set_input_files(
        {'name': 'large.json', 'mimeType': 'application/json', 'buffer': b'x' * 65537})
    expect(panel.get_by_text('檔案超過 64 KiB，請縮小清單後再匯入。')).to_be_visible()
    panel.locator('summary').filter(has_text='貼上清單或查看格式').click()
    panel.get_by_label('清單內容', exact=True).fill('設' * 22000)
    expect(panel.get_by_role('button', name='匯入設備清單', exact=True)).to_be_disabled()
    assert network_requests(state, 'POST') == []


@pytest.mark.parametrize('between', ['refresh', 'collapse'])
def test_inventory_lost_write_retries_exact_key_and_body(inventory_browser, between):
    page, state = inventory_browser
    panel = open_inventory(page, state)
    state['network_lost'] = True
    panel.get_by_label('探索的主機網路').select_option(SCOPE)
    panel.get_by_role('button', name='開始探索', exact=True).click()
    expect(panel.get_by_role('button', name='重試本次盤點')).to_be_visible()
    if between == 'collapse':
        panel.locator(':scope>summary').click()
        panel.locator(':scope>summary').click()
    else:
        panel.get_by_role('button', name='重新讀取盤點', exact=True).click()
    expect(panel.get_by_role('button', name='重試本次盤點')).to_be_visible()
    panel.get_by_role('button', name='重試本次盤點').click()
    expect(panel.get_by_text(NAME, exact=True)).to_be_visible()
    writes = network_requests(state, 'POST')
    assert len(writes) == 2 and writes[0]['key'] == writes[1]['key']
    assert writes[0]['body'] == writes[1]['body']
    assert len(state['network_commits']) == 1


def test_inventory_clear_is_revision_bound_and_only_clears_local_snapshot(inventory_browser):
    page, state = inventory_browser
    state['network'] = inventory_snapshot(True, 4)
    panel = open_inventory(page, state)
    panel.locator('summary').filter(has_text='涵蓋範圍與來源').click()
    panel.get_by_role('button', name='清除保存的盤點').click()
    expect(panel.get_by_text('只清除保存的網路觀測；家庭設備、整合及接入追蹤會保留。')).to_be_visible()
    assert network_requests(state, 'POST') == []
    panel.get_by_role('button', name='確認清除盤點').click()
    expect(panel.get_by_role('heading', name='觀測結果 · 0 筆')).to_be_visible()
    assert network_requests(state, 'POST')[0]['body'] == {'expected_revision': 4}
    assert len(state['snapshot']['devices']) == 2


@pytest.mark.parametrize('broken', ['foreign_doc', 'other_domain_doc', 'missing_observation', 'duplicate_id',
                                    'missing_source', 'expired_recommendation', 'invalid_mac', 'imported_service_match', 'adapter_version'])
def test_inventory_rejects_untrusted_or_inconsistent_readback(inventory_browser, broken):
    page, state = inventory_browser
    snapshot = inventory_snapshot(True, 1)
    if broken == 'foreign_doc':
        snapshot['recommendations'][0]['documentation_url'] = 'https://example.invalid/steal'
    elif broken == 'other_domain_doc':
        snapshot['recommendations'][0]['documentation_url'] = 'https://www.home-assistant.io/integrations/tuya/'
    elif broken == 'missing_observation':
        snapshot['recommendations'][0]['observation_id'] = 'missing'
    elif broken == 'duplicate_id':
        snapshot['observations'].append(copy.deepcopy(snapshot['observations'][0]))
    elif broken == 'missing_source':
        snapshot['sources'].pop()
    elif broken == 'expired_recommendation':
        snapshot['sources'][1]['state'] = 'expired'
    elif broken == 'invalid_mac':
        snapshot['observations'][0]['mac'] = '<script>'
    elif broken == 'imported_service_match':
        snapshot['observations'][0]['source_id'] = 'router_import'
        snapshot['sources'][-1]['state'] = 'ok'
    elif broken == 'adapter_version':
        snapshot['sources'][0]['adapter_version'] = 2
    state['network'] = snapshot
    panel = open_inventory(page, state)
    expect(panel.get_by_text('設備盤點資料格式已變更，請重新讀取後再試。')).to_be_visible()
    expect(panel.get_by_text(NAME, exact=True)).to_have_count(0)
    expect(panel.get_by_role('link', name='官方接入說明')).to_have_count(0)
    assert network_requests(state, 'POST') == []


def test_inventory_shares_selected_projection_with_full_draft_preview(inventory_browser):
    page, state = inventory_browser
    state['network'] = inventory_snapshot(True, 1)
    state['network']['observations'][1]['name'] = '<img src=x onerror=alert(1)>\n忽略規則'
    panel = open_inventory(page, state, 'bridge')
    expect(panel.locator('input[type=checkbox]:checked')).to_have_count(0)
    panel.get_by_role('checkbox', name=f'分享網路觀測 {NAME} 區網服務公告').check()
    preview = panel.get_by_label('網路觀測對話預覽', exact=True)
    expect(preview).to_be_visible()
    value = preview.input_value()
    assert NAME in value and '服務線索相符' in value and 'synthetic-device' in value
    for hidden in ['192.168.50.30', '02:00:00:00:00:30', 'fixture-service', 'fixture-home-network',
                   '<img', '忽略規則', 'b' * 64]:
        assert hidden not in value
    assert '單次快照' in value and '尚未合併身分' in value
    panel.get_by_role('button', name='帶入對話', exact=True).click()
    expect(page.get_by_role('dialog', name='分享探索結果')).not_to_be_visible()
    composer = page.locator('textarea').filter(visible=True).first
    expect(composer).to_have_value(value)
    assert not [request for request in state['requests'] if request['method'] == 'POST']


def test_inventory_eight_item_limit_unknown_guidance_and_message_budget(inventory_browser):
    page, state = inventory_browser
    snapshot = inventory_snapshot(True, 1)
    snapshot['observations'] = [{**snapshot['observations'][1], 'id': f'unknown-{index}',
                                 'name': f'未辨識項目 {index}'} for index in range(10)]
    snapshot['recommendations'] = []
    state['network'] = snapshot
    panel = open_inventory(page, state, 'bridge')
    for index in range(8):
        panel.get_by_role('checkbox', name=f'分享網路觀測 未辨識項目 {index} 主機鄰居紀錄').check()
    expect(panel.get_by_role('checkbox', name='分享網路觀測 未辨識項目 8 主機鄰居紀錄')).to_be_disabled()
    assert '尚未辨識' in panel.get_by_label('網路觀測對話預覽').input_value()
    panel.get_by_role('checkbox', name='分享網路觀測 未辨識項目 0 主機鄰居紀錄').uncheck()
    expect(panel.get_by_role('checkbox', name='分享網路觀測 未辨識項目 8 主機鄰居紀錄')).to_be_enabled()
    panel.get_by_role('button', name='重新讀取盤點', exact=True).click()
    expect(panel.locator('input[type=checkbox]:checked')).to_have_count(0)
    expect(panel.get_by_label('網路觀測對話預覽')).to_have_count(0)


@pytest.mark.parametrize('boundary', ['collapse', 'identity'])
def test_inventory_late_read_cannot_restore_collapsed_or_expired_identity(inventory_browser, boundary):
    page, state = inventory_browser
    state['network'] = inventory_snapshot(True, 1)
    state['network_hold'].add(('GET', 'discovery/inventory'))
    panel = open_inventory(page, state, 'bridge')
    expect(panel.get_by_text('正在讀取盤點結果…')).to_be_visible()
    if boundary == 'collapse':
        panel.locator(':scope>summary').click()
    else:
        page.evaluate("window.dispatchEvent(new Event('agentic-bridge-expired'))")
    held, state['network_held'] = state['network_held'], []
    for route, status, result in held:
        route.fulfill(status=status, content_type='application/json', body=json.dumps(result))
    expect(page.get_by_text(NAME, exact=True)).to_have_count(0)
    assert network_requests(state, 'POST') == []


def test_inventory_tracking_keeps_exact_network_source_and_explicit_ha_choice(inventory_browser):
    page, state = inventory_browser
    state['network'] = inventory_snapshot(True, 1)
    record = setup_record()
    record['candidate'].update(id='fixture-recommendation', source_id='network_inventory',
                               title='服務接入建議（合成資料）', discovery_method='network_inventory')

    def create(route):
        body = route.request.post_data_json
        assert body == {'candidate_id': 'fixture-recommendation', 'integration_domain': 'fixture_bridge',
                        'source_id': 'network_inventory'}
        state['requests'].append({'method': 'POST', 'path': 'discovery/setups', 'body': body})
        state['add_record'](record)
        view = setup_view(record)
        view['sources'].append({'id': 'network_inventory', 'state': 'ok', 'truncated': False,
                               'item_count': 2, 'observed_at': OBSERVED, 'reason_code': None})
        state['views'][SETUP_ID] = view
        route.fulfill(status=200, content_type='application/json', body=json.dumps(
            {'contract_version': 1, 'id': SETUP_ID, 'revision': 1, 'forgotten': False}))

    page.route('**/api/discovery/setups', lambda route: create(route) if route.request.method == 'POST' else route.fallback())
    panel = open_inventory(page, state)
    panel.locator('summary').filter(has_text='接入進度').click()
    panel.get_by_role('button', name='追蹤接入 服務接入建議（合成資料）', exact=True).click()
    view = panel.get_by_role('region', name='接入讀回')
    expect(view).to_be_visible()
    expect(view.get_by_text('原建議仍出現在網路盤點中。', exact=False)).to_be_visible()
    expect(view.locator('input[type=radio]:checked')).to_have_count(0)
    expect(view.get_by_role('button', name='保存整合選擇')).to_be_disabled()
    expect(view.get_by_text('尚未進行實體操作驗證。')).to_be_visible()


@pytest.mark.parametrize('mode', ['host', 'bridge'])
def test_inventory_responsive_acquisition_results_import_and_keyboard(inventory_browser, tmp_path, mode):
    page, state = inventory_browser
    evidence = Path(os.environ.get('NETWORK_INVENTORY_UI_EVIDENCE', str(tmp_path / 'screenshots')))
    evidence.mkdir(parents=True, exist_ok=True)
    for width, height in [(1440, 900), (768, 1024), (390, 844)]:
        page.set_viewport_size({'width': width, 'height': height})
        state['network'] = inventory_snapshot(True, 1)
        panel = open_inventory(page, state, mode)
        expect(panel.get_by_text(NAME, exact=True)).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        panel.get_by_role('button', name='開始探索').scroll_into_view_if_needed()
        page.screenshot(path=str(evidence / f'network-{mode}-{width}.png'), full_page=True)
        for control in panel.locator('button,input,select,summary').all():
            if not control.is_visible():
                continue
            box = control.bounding_box()
            if control.get_attribute('type') == 'checkbox':
                box = control.locator('..').bounding_box()
            assert box and box['height'] >= 44, (control.inner_text(), box)
        if width == 390:
            assert panel.get_by_label('探索的主機網路').evaluate('node=>parseFloat(getComputedStyle(node).fontSize)') >= 16
        panel.get_by_role('button', name='匯入清單', exact=True).click()
        panel.locator('summary').filter(has_text='貼上清單或查看格式').click()
        expect(panel.get_by_label('清單內容')).to_be_visible()
        page.screenshot(path=str(evidence / f'import-{mode}-{width}.png'), full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        if mode == 'bridge':
            page.keyboard.press('Escape')
            expect(page.get_by_role('dialog', name='分享探索結果')).not_to_be_visible()


@pytest.mark.parametrize('next_action', ['new_file', 'paste', 'oversize'])
def test_inventory_file_read_generation_preserves_latest_owner_input(inventory_browser, next_action):
    page, state = inventory_browser
    panel = open_inventory(page, state)
    panel.get_by_role('button', name='匯入清單', exact=True).click()
    panel.locator('summary').filter(has_text='貼上清單或查看格式').click()
    page.evaluate("""() => {
      const original=File.prototype.text;
      File.prototype.text=function(){if(this.name==='old.json')return new Promise(resolve=>window.releaseOldFile=()=>resolve('[{"name":"OLD"}]'));return original.call(this)};
    }""")
    picker = panel.get_by_label('選擇清單檔案', exact=True)
    picker.set_input_files({'name': 'old.json', 'mimeType': 'application/json', 'buffer': b'old'})
    expect(panel.get_by_text('正在讀取清單檔案…')).to_be_visible()
    if next_action == 'new_file':
        picker.set_input_files({'name': 'new.csv', 'mimeType': 'text/csv', 'buffer': b'ip,hostname\n192.168.50.21,NEW'})
        expect(panel.get_by_label('清單內容')).to_have_value('ip,hostname\n192.168.50.21,NEW')
    elif next_action == 'paste':
        panel.get_by_label('清單內容').fill('[{"name":"PASTED"}]')
    else:
        picker.set_input_files({'name': 'large.csv', 'mimeType': 'text/csv', 'buffer': b'x' * 65537})
    page.evaluate('window.releaseOldFile()')
    expected = {'new_file': 'ip,hostname\n192.168.50.21,NEW', 'paste': '[{"name":"PASTED"}]', 'oversize': ''}[next_action]
    expect(panel.get_by_label('清單內容')).to_have_value(expected)
    if next_action == 'new_file':
        expect(panel.get_by_label('檔案格式')).to_have_value('csv')
    if next_action == 'oversize':
        expect(panel.get_by_role('button', name='匯入設備清單')).to_be_disabled()
    assert network_requests(state, 'POST') == []


def test_inventory_shared_import_retains_source_age_and_stays_plain_text(inventory_browser):
    page, state = inventory_browser
    snapshot = inventory_snapshot(True, 1)
    old = snapshot['observations'][1]
    old.update(source_id='router_import', name='<img src=x onerror=alert(1)>\n來源文字',
               source_updated_at='2025-01-02T03:04:05Z',
               metadata={'manufacturer': 'Synthetic Maker', 'model': 'Model 4',
                         'server': 'private-server', 'usn': 'private-usn'})
    snapshot['sources'][-1].update(state='ok', observed_at=OBSERVED, item_count=1)
    state['network'] = snapshot
    panel = open_inventory(page, state, 'bridge')
    checkbox = panel.get_by_role('checkbox').nth(1)
    checkbox.check()
    preview = panel.get_by_label('網路觀測對話預覽').input_value()
    assert '2025-01-02T03:04:05Z' in preview and '來源原始更新時間' in preview
    assert '\\n來源文字' in preview
    assert 'Synthetic Maker' in preview and 'Model 4' in preview
    assert 'private-server' not in preview and 'private-usn' not in preview
    expect(panel.locator('img')).to_have_count(0)
    expect(panel.get_by_text('來源原始更新於', exact=False)).to_be_visible()


def test_inventory_expiry_clears_selection_and_stops_showing_actionable_guidance(inventory_browser):
    from datetime import UTC, datetime, timedelta

    page, state = inventory_browser
    snapshot = inventory_snapshot(True, 1)
    snapshot['expires_at'] = (datetime.now(UTC) + timedelta(seconds=3)).isoformat()
    state['network'] = snapshot
    panel = open_inventory(page, state, 'bridge')
    panel.get_by_role('checkbox').first.check()
    expect(panel.get_by_label('網路觀測對話預覽')).to_be_visible()
    expect(panel.get_by_text('盤點建議已到期，請重新探索或匯入清單。')).to_be_visible(timeout=7000)
    expect(panel.get_by_label('網路觀測對話預覽')).to_have_count(0)
    expect(panel.locator('input[type=checkbox]:checked')).to_have_count(0)
    expect(panel.get_by_role('link', name='官方接入說明')).to_have_count(0)
    assert network_requests(state, 'POST') == []


def test_inventory_return_focus_refreshes_saved_observations_without_scanning(inventory_browser):
    page, state = inventory_browser
    state['network'] = inventory_snapshot(True, 1)
    panel = open_inventory(page, state, 'bridge')
    panel.get_by_role('checkbox').first.check()
    expect(panel.get_by_label('網路觀測對話預覽')).to_be_visible()
    state['network'] = inventory_snapshot(False, 2)
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(panel.get_by_role('heading', name='觀測結果 · 0 筆')).to_be_visible()
    expect(panel.get_by_label('網路觀測對話預覽')).to_have_count(0)
    assert network_requests(state, 'POST') == []


def test_inventory_bridge_outage_hides_data_then_reads_saved_revision_without_retry(inventory_browser):
    page, state = inventory_browser
    state['network'] = inventory_snapshot(True, 1)
    panel = open_inventory(page, state, 'bridge')
    panel.get_by_role('checkbox').first.check()
    page.evaluate("window.dispatchEvent(new Event('agentic-bridge-unavailable'))")
    expect(page.get_by_role('dialog', name='分享探索結果')).not_to_be_visible()
    expect(page.get_by_text(NAME, exact=True)).to_have_count(0)
    panel = open_inventory(page, state, 'bridge')
    expect(panel.get_by_text(NAME, exact=True)).to_be_visible()
    expect(panel.locator('input[type=checkbox]:checked')).to_have_count(0)
    assert state['network']['revision'] == 1
    assert network_requests(state, 'POST') == []


def test_inventory_large_existing_draft_requires_shorter_selected_preview(inventory_browser):
    page, state = inventory_browser
    state['network'] = inventory_snapshot(True, 1)
    state['mode'] = 'bridge'
    page.goto(state['base'] + '#gateway')
    composer = page.locator('textarea').filter(visible=True).first
    composer.fill('既有草稿' * 1175)
    panel = open_inventory(page, state, 'bridge', navigate=False)
    panel.get_by_role('checkbox').first.check()
    preview = panel.get_by_label('網路觀測對話預覽').input_value()
    assert preview.startswith('既有草稿' * 1175)
    assert len(preview) > 5000
    expect(panel.get_by_role('button', name='帶入對話', exact=True)).to_be_disabled()
    assert network_requests(state, 'POST') == []


@pytest.mark.parametrize('width', [390, 1440])
def test_inventory_demo_scan_native_setup_readback_and_draft_share(browser, origin, width):
    from test_native_demo import native_nav, read_api
    from test_site import clean, product_frame, watch

    page = browser.new_page(viewport={'width': width, 'height': 844 if width == 390 else 900})
    errors, external = watch(page, origin)
    page.goto(f'{origin}/demo/#/agentic-home')
    ui = product_frame(page, 'bridge')
    ui.get_by_role('button', name='新對話', exact=True).click()
    ui.locator('#gateway-message').fill('請幫我核對設備。')
    before_policy = read_api(ui, 'inventory')['policy']
    before_tasks = read_api(ui, 'tasks')
    before_messages = read_api(ui, 'gateway/messages')
    ui.get_by_role('button', name='分享探索結果', exact=True).click()
    panel = ui.locator('.network-inventory')
    panel.locator(':scope>summary').click()
    panel.get_by_label('探索的主機網路').select_option('demo-home-lan')
    panel.get_by_role('button', name='開始探索', exact=True).click()
    expect(panel.get_by_role('heading', name='觀測結果 · 4 筆')).to_be_visible()
    snapshot = read_api(ui, 'discovery/inventory')
    assert len(snapshot['observations']) == 4
    assert len(snapshot['recommendations']) == 3
    assert {item['confidence'] for item in snapshot['recommendations']} == {'service_match', 'hint'}
    panel.locator('summary').filter(has_text='接入進度').click()
    panel.get_by_role('button', name='追蹤接入 Shelly 接入建議', exact=True).click()
    result = panel.get_by_role('region', name='接入讀回')
    expect(result).to_be_visible()
    assert read_api(ui, 'discovery/setups')['setups'][0]['candidate']['source_id'] == 'network_inventory'
    result.get_by_role('link', name='開啟家庭設定 ↗', exact=True).click()
    expect(page).to_have_url(f'{origin}/demo/#/config/integrations')
    page.get_by_role('button', name='新增Shelly 接入建議', exact=True).click()
    native_dialog = page.get_by_role('dialog', name='設定示範設備')
    expect(native_dialog).to_contain_text('虛構的手動設定流程')
    native_dialog.get_by_role('button', name='完成示範設定', exact=True).click()
    native_nav(page, '智能管家')
    panel.get_by_role('button', name='重新讀取接入', exact=True).click()
    expect(result.get_by_role('radio')).to_have_count(1)
    expect(result.get_by_role('radio')).not_to_be_checked()
    result.get_by_role('radio').check()
    result.get_by_role('button', name='保存整合選擇').click()
    expect(result.get_by_text('1 筆設備紀錄', exact=True)).to_be_visible()
    expect(result.get_by_text('1 / 1 個功能顯示可用狀態', exact=True)).to_be_visible()
    expect(result.get_by_text('尚未進行實體操作驗證。')).to_be_visible()
    panel.get_by_role('button', name='返回盤點', exact=True).click()
    panel.get_by_role('checkbox', name='分享網路觀測 書房 Shelly 插座 區網服務公告', exact=True).check()
    preview = panel.get_by_label('網路觀測對話預覽').input_value()
    assert preview.startswith('請幫我核對設備。\n\n【網路設備探索快照】')
    assert 'Google Cast' not in preview and '192.168.50' not in preview
    panel.get_by_role('button', name='帶入對話', exact=True).click()
    expect(ui.locator('#gateway-message')).to_have_value(preview)
    assert read_api(ui, 'gateway/messages') == before_messages
    ui.get_by_role('button', name='送出訊息', exact=True).click()
    expect(ui.locator('.assistant-answer').filter(has_text='這是固定示範回覆')).to_be_visible()
    assert read_api(ui, 'tasks') == before_tasks
    assert read_api(ui, 'inventory')['policy'] == before_policy
    clean(page, errors, external)
    page.close()


def test_inventory_demo_router_import_preserves_age_hints_and_clear_scope(browser, origin):
    from test_native_demo import read_api
    from test_site import clean, product_frame, watch

    page = browser.new_page(viewport={'width': 768, 'height': 1024})
    errors, external = watch(page, origin)
    page.goto(f'{origin}/demo/#inventory')
    ui = product_frame(page)
    before = read_api(ui, 'inventory')
    panel = ui.locator('.network-inventory')
    panel.locator(':scope>summary').click()
    panel.get_by_role('button', name='匯入清單', exact=True).click()
    content = json.dumps({'devices': [
        {'name': 'Tuya 智慧插座', 'manufacturer': 'Tuya'},
        {'name': 'Shelly 舊清單', 'ip': '192.168.50.33', 'last_seen': '2025-01-01T00:00:00Z'},
    ]})
    panel.get_by_label('選擇清單檔案').set_input_files(
        {'name': 'router.json', 'mimeType': 'application/json', 'buffer': content.encode()})
    panel.get_by_role('button', name='匯入設備清單', exact=True).click()
    expect(panel.get_by_text('Tuya 智慧插座', exact=True)).to_be_visible()
    expect(panel.get_by_text('Tuya 帳號接入建議', exact=True)).to_be_visible()
    expect(panel.get_by_text('來源原始更新於', exact=False)).to_be_visible()
    snapshot = read_api(ui, 'discovery/inventory')
    assert len(snapshot['recommendations']) == 1
    assert snapshot['recommendations'][0]['confidence'] == 'hint'
    assert snapshot['recommendations'][0]['integration_domain'] == 'tuya'
    panel.locator('summary').filter(has_text='涵蓋範圍與來源').click()
    panel.get_by_role('button', name='清除保存的盤點').click()
    panel.get_by_role('button', name='確認清除盤點').click()
    expect(panel.get_by_role('heading', name='觀測結果 · 0 筆')).to_be_visible()
    assert read_api(ui, 'inventory') == before
    clean(page, errors, external)
    page.close()


@pytest.mark.parametrize('boundary', ['read', 'mutation', 'parallel_scopes'])
def test_inventory_host_forbidden_clears_prior_private_fields_without_logging_out_rescue(inventory_browser, boundary):
    page, state = inventory_browser
    state['network'] = inventory_snapshot(True, 1)
    if boundary == 'parallel_scopes':
        state['network_errors'][('GET', 'discovery/inventory/scopes')] = (
            403, {'detail': 'discovery_authority_changed'})
    panel = open_inventory(page, state)
    if boundary != 'parallel_scopes':
        expect(panel.get_by_text(NAME, exact=True)).to_be_visible()
        panel.get_by_role('button', name='匯入清單', exact=True).click()
        panel.get_by_label('清單名稱', exact=True).fill('不應保留的私人名稱')
        panel.locator('summary').filter(has_text='貼上清單或查看格式').click()
        panel.get_by_label('清單內容').fill('[{"name":"PRIVATE CONTENT"}]')
        method, path = ('GET', 'discovery/inventory') if boundary == 'read' else ('POST', 'discovery/inventory/import')
        state['network_errors'][(method, path)] = (403, {'detail': 'discovery_authority_changed'})
        panel.get_by_role('button', name='重新讀取盤點' if boundary == 'read' else '匯入設備清單', exact=True).click()
    expect(panel.get_by_text('家庭授權已變更，請重新確認身分。')).to_be_visible()
    expect(panel.get_by_text(NAME, exact=True)).to_have_count(0)
    expect(panel.get_by_role('button', name='重試本次盤點')).to_have_count(0)
    panel.get_by_role('button', name='主機探索', exact=True).click()
    expect(panel.get_by_label('探索的主機網路')).to_be_disabled()
    expect(panel.get_by_label('探索的主機網路').locator('option')).to_have_count(1)
    panel.get_by_role('button', name='匯入清單', exact=True).click()
    details = panel.locator('.network-inventory-import details').first
    if not details.evaluate('node=>node.open'):
        details.locator(':scope>summary').click()
    expect(panel.get_by_label('清單內容')).to_have_value('')
    expect(panel.get_by_label('清單名稱')).to_have_value('路由器設備清單')
    expect(page.get_by_role('heading', name='設備', exact=True)).to_be_visible()
    assert not [request for request in state['requests'] if request['path'] == 'host/login']
