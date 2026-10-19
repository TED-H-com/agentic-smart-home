"""Source-specific candidate guidance in compiled UI; household/device data are synthetic."""
import copy
import json
import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect
from test_conversation_discovery_ui import conversation_browser as conversation_browser
from test_conversation_discovery_ui import printer_result, sent_messages, setup_posts
from test_discovery_ui import observation
from test_network_inventory_ui import inventory_snapshot

TITLE = '書房設備（合成資料）'
IPP_STEP = '這是保存的印表機服務線索；請先核對家庭是否已發現它。'
SYNOLOGY_STEP = '請先核對是否為 Synology 設備，再由家庭原生設定完成帳號授權。'
MATTER_STEP = 'Matter 服務線索不能代替配對；請依家庭原生流程準備配對碼。'


def mixed_candidates():
    result = printer_result()
    result.update(share_scope='environment', candidate_count=4)
    result['candidates'][0]['title'] = TITLE
    first = result['candidates'][0]
    for identifier, domain, label, title, step in (
        ('inventory-ipp', 'ipp', '網路印表機', TITLE, IPP_STEP),
        ('inventory-synology', 'synology_dsm', 'Synology 網路儲存設備', '儲存服務（合成線索）', SYNOLOGY_STEP),
        ('inventory-matter', 'matter', 'Matter 設備', '配對服務（合成線索）', MATTER_STEP),
    ):
        result['candidates'].append({**first, 'id': identifier, 'source_id': 'network_inventory',
            'discovery_method': 'network_inventory', 'integration_domain': domain,
            'integration_name': label, 'title': title, 'next_step': step})
    return result


def open_saved_discovery(page, state, value):
    identifier = '1' * 64
    state['response'] = {'discovery': value}
    state['messages'] = [{'id': identifier, 'thread_id': identifier, 'source': 'web',
        'message': '先前明確分享的合成探索結果', 'answer': '這是保存的探索結果。',
        'runtime': 'host', 'provider': 'codex', 'coordinator': 'hermes', 'state': 'succeeded',
        'created_at': '2026-10-19T12:00:00Z', 'discovery': value}]
    page.add_init_script("sessionStorage.setItem('agentic-auth-conversations-" + '1' * 64 + "', JSON.stringify({'hermes-host-codex':'" + identifier + "'}))")
    page.goto(state['url'] + '#gateway')
    panel = page.get_by_role('region', name='設備探索結果', exact=True)
    expect(panel.get_by_role('heading', name='設備接入線索', exact=True)).to_be_visible()
    return panel


def test_same_name_ha_and_saved_network_ipp_have_distinct_actions(conversation_browser, tmp_path):
    page, state = conversation_browser
    value = mixed_candidates()
    # Even a network clue advertising the same method must not acquire HA setup authority.
    value['candidates'][1]['discovery_method'] = 'zeroconf'
    panel = open_saved_discovery(page, state, value)
    rows = panel.locator('.conversation-discovery-candidates > li')
    expect(rows).to_have_count(4)
    expect(rows.nth(0).get_by_text(TITLE, exact=True)).to_be_visible()
    expect(rows.nth(1).get_by_text(TITLE, exact=True)).to_be_visible()
    expect(rows.nth(0).get_by_text('家庭發現 · 待確認接入', exact=True)).to_be_visible()
    expect(rows.nth(1).get_by_text('保存的網路線索 · 尚未核對接入', exact=True)).to_be_visible()
    expect(panel.get_by_role('button', name='協助設定', exact=True)).to_have_count(1)
    expect(panel.get_by_role('button', name='請管家核對', exact=True)).to_have_count(3)
    for step in (IPP_STEP, SYNOLOGY_STEP, MATTER_STEP):
        expect(panel.get_by_text(step, exact=True)).to_be_visible()
    expect(panel.get_by_text('不同來源可能來自同一設備，尚未合併身分。', exact=True)).to_be_visible()
    assert sent_messages(state) == [] and setup_posts(state) == []
    assert all(method == 'GET' for method, _, _, _ in state['requests'])
    evidence = Path(os.environ.get('NETWORK_CANDIDATE_UI_EVIDENCE', str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    for width in (1440, 768, 390):
        page.set_viewport_size({'width': width, 'height': 900 if width == 1440 else 844})
        button = rows.nth(1).get_by_role('button', name='請管家核對', exact=True)
        button.scroll_into_view_if_needed()
        assert button.bounding_box()['height'] >= 44
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        rows.nth(1 if width == 390 else 0).evaluate("element => element.scrollIntoView({block:'start'})")
        page.screenshot(path=str(evidence / f'mixed-network-clues-{width}.png'), full_page=True)
        if width == 390:
            rows.nth(0).evaluate("element => element.scrollIntoView({block:'start'})")
            page.screenshot(path=str(evidence / 'mixed-network-clues-ha-390.png'), full_page=True)
    rows.nth(0).get_by_role('button', name='協助設定', exact=True).click()
    expect(rows.nth(0).get_by_role('button', name='確認接入', exact=True)).to_be_enabled()
    expect(rows.nth(1).get_by_role('button', name='確認接入', exact=True)).to_have_count(0)
    assert [path for path, _, _ in setup_posts(state)] == ['device-setups']
    assert setup_posts(state)[0][1]['candidate_id'] == value['candidates'][0]['id']
    assert sent_messages(state) == []


@pytest.mark.parametrize('source', ['network_inventory', 'ha_discovery'])
def test_selected_source_and_integration_are_preserved_in_explicit_message(conversation_browser, source):
    page, state = conversation_browser
    value = mixed_candidates()
    index = 1 if source == 'network_inventory' else 2
    selected = value['candidates'][index]
    if source == 'ha_discovery':
        selected.update(source_id=source, discovery_method='zeroconf', title=TITLE)
    panel = open_saved_discovery(page, state, value)
    page.get_by_label('對話訊息', exact=True).fill('保留原本的草稿')
    state['response'] = {}
    action = '請管家核對' if source == 'network_inventory' else '協助設定'
    panel.locator('.conversation-discovery-candidates > li').nth(index).get_by_role('button', name=action, exact=True).click()
    expect(page.get_by_text('合成的管家回覆。', exact=True)).to_be_visible()
    expected = (f'請先核對保存的網路線索「{TITLE}」（網路印表機），說明接入方式與下一步；這筆線索尚未確認為家庭待接入設備。'
        if source == 'network_inventory' else f'請帶我設定家庭發現的「{TITLE}」（Synology 網路儲存設備），先說明需要我做的下一步。')
    assert [message['message'] for message in sent_messages(state)] == [expected]
    assert sent_messages(state)[0]['discover_environment'] is True
    assert setup_posts(state) == []
    expect(page.get_by_label('對話訊息', exact=True)).to_have_value('保留原本的草稿')


def test_network_candidate_names_are_plain_text(conversation_browser):
    page, state = conversation_browser
    value = mixed_candidates()
    title = '<img src=x onerror="window.syntheticDeviceNameExecuted=true">'
    value['candidates'][1]['title'] = title
    panel = open_saved_discovery(page, state, value)
    expect(panel.get_by_text(title, exact=True)).to_be_visible()
    expect(panel.locator('img')).to_have_count(0)
    assert page.evaluate('window.syntheticDeviceNameExecuted === undefined')
    assert sent_messages(state) == [] and setup_posts(state) == []


def test_inventory_uses_plain_integration_names_and_keeps_backend_next_steps(conversation_browser):
    page, state = conversation_browser
    inventory = inventory_snapshot(True, 1)
    original_row, original_rec = inventory['observations'][0], inventory['recommendations'][0]
    inventory['observations'], inventory['recommendations'] = [], []
    for domain, step in (('ipp', IPP_STEP), ('synology_dsm', SYNOLOGY_STEP), ('matter', MATTER_STEP)):
        identifier = 'observation-' + domain
        inventory['observations'].append({**copy.deepcopy(original_row), 'id': identifier, 'name': '保存的服務 ' + domain})
        inventory['recommendations'].append({**copy.deepcopy(original_rec), 'id': 'recommendation-' + domain,
            'observation_id': identifier, 'integration_domain': domain, 'title': domain, 'next_step': step,
            'documentation_url': f'https://www.home-assistant.io/integrations/{domain}/'})

    def inventory_route(route):
        path = route.request.url.split('/api/', 1)[1]
        state['requests'].append((route.request.method, path, None, None))
        assert route.request.method == 'GET'
        result = {'contract_version': 1, 'scopes': [], 'limitations': []} if path.endswith('/scopes') else inventory
        route.fulfill(content_type='application/json', body=json.dumps(result))

    page.route('**/api/discovery', lambda route: route.fulfill(content_type='application/json', body=json.dumps(observation())))
    page.route('**/api/discovery/inventory**', inventory_route)
    page.goto(state['url'] + '#gateway')
    page.get_by_role('button', name='分享探索結果', exact=True).click()
    dialog = page.get_by_role('dialog', name='分享探索結果', exact=True)
    dialog.locator('summary').filter(has_text='探索更多設備').click()
    panel = dialog.locator('.network-inventory')
    for label in ('網路印表機', 'Synology 網路儲存設備', 'Matter 設備'):
        expect(panel.get_by_role('heading', name=label, exact=True)).to_be_visible()
        expect(panel.get_by_text(label + ' · 保存的網路線索', exact=True)).to_be_visible()
    for step in (IPP_STEP, SYNOLOGY_STEP, MATTER_STEP):
        expect(panel.get_by_text(step, exact=True)).to_be_visible()
    assert 'synology_dsm' not in '\n'.join(panel.locator('.network-inventory-guidance').all_inner_texts())
    panel.get_by_role('checkbox').first.check()
    preview = panel.get_by_label('網路觀測對話預覽', exact=True)
    expect(preview).to_have_value(re.compile('整合種類："網路印表機"'))
    assert '整合種類："ipp"' not in preview.input_value()
    assert sent_messages(state) == [] and setup_posts(state) == []
    assert all(method == 'GET' for method, _, _, _ in state['requests'])
