"""Compiled UI with fictional wiring observations, not physical LAN acceptance."""
import copy
import json
from pathlib import Path

import pytest
from playwright.sync_api import expect
from test_worker_ui import common, ui_page  # noqa: F401

CHOICE = "a" * 64


def mount(page, origin):
    value = {"contract_version": 1, "mode": "nat", "state": "disabled", "selected_interface_id": None,
             "interface": None, "guest_mac": None, "vm_state": "shut off", "can_configure": True, "can_connect": True,
             "interfaces": [{"id": CHOICE, "label": "家用網路 enp2s0", "interface": "enp2s0",
                             "address": "192.168.50.10", "network": "192.168.50.0/24"}], "limitations": [],
             "home": {"state": "not_configured"}}
    state = {"value": value, "fail": False, "lose": False, "job": None}
    posts, errors = [], []

    def route(route):
        request = route.request
        path = request.url.split('/api/')[1]
        if path == 'network/ha-lan':
            if state['fail']:
                route.fulfill(status=503, content_type='application/json', body='{}')
                return
            result = state['value']
        elif path == 'host/actions':
            body = request.post_data_json
            posts.append((body, request.headers.get('idempotency-key')))
            state['job'] = {'id': posts[-1][1], 'state': 'running', 'request': body, 'at': 1792195200}
            if state['lose']:
                state['lose'] = False
                route.abort()
                return
            result = state['job']
        elif path == 'host/status':
            result = {**common(path), 'jobs': [state['job']] if state['job'] else []}
        else:
            result = common(path)
            if result is None:
                raise AssertionError(path)
        route.fulfill(status=200, content_type='application/json', body=json.dumps(result))

    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/api/**', route)
    page.clock.install()
    page.goto(origin + '/#host')
    page.locator('summary').filter(has_text='家庭區網接入').click()
    expect(page.get_by_role('button', name='連上家庭網路')).to_be_visible()
    return state, posts, errors


def test_explicit_wired_choice_job_readback_and_same_key_after_lost_response(ui_page):  # noqa: F811
    page, origin = ui_page
    state, posts, errors = mount(page, origin)
    assert posts == []
    save = page.get_by_role('button', name='連上家庭網路')
    expect(save).to_be_enabled()
    expect(page.get_by_label('接到家庭路由器的網路線')).to_have_count(0)
    state['lose'] = True
    save.click()
    expect(page.get_by_role('alert')).to_be_visible()
    save.click()
    expect(page.get_by_role('button', name='正在連接…')).to_be_disabled()
    assert len(posts) == 2 and posts[0] == posts[1]
    assert posts[0][0] == {'action': 'configure_lan'}
    assert posts[0][1]
    expect(page.get_by_role('region', name='家庭區網接入')).to_have_attribute('aria-busy', 'false')
    state['job']['state'] = 'succeeded'
    state['value'].update(mode='wired', state='configured', selected_interface_id=CHOICE, interface='enp2s0',
                          home={'state': 'needs_home_setup'}, core_configuration_pending=True)
    page.clock.run_for(5500)
    expect(page.get_by_text('已連接', exact=True)).to_be_visible()
    expect(page.get_by_text('家庭已取得區網位址；完成家庭授權後會自動核對探索功能。')).to_be_visible()
    expect(save).to_be_disabled()
    assert not errors


def test_running_guest_uses_one_action_and_unreadable_network_blocks_mutation(ui_page):  # noqa: F811
    page, origin = ui_page
    state, posts, errors = mount(page, origin)
    state['value'].update(vm_state='running', can_configure=False)
    page.clock.run_for(5500)
    expect(page.get_by_role('button', name='連上家庭網路')).to_be_enabled()
    expect(page.get_by_text('連接期間家庭自動化會短暫暫停；未能連上時會自動恢復基本網路。')).to_be_visible()
    state['fail'] = True
    page.clock.run_for(5500)
    expect(page.get_by_role('alert')).to_contain_text('暫時無法讀取家庭網路')
    state['value'].update(vm_state='shut off', can_configure=True)
    state['fail'] = False
    page.clock.run_for(5500)
    expect(page.get_by_role('alert')).to_have_count(0)
    expect(page.get_by_role('button', name='連上家庭網路')).to_be_enabled()
    assert not posts and not errors


@pytest.mark.parametrize('width', [390, 768, 1440])
def test_wired_readback_is_distinct_from_device_acceptance(ui_page, width):  # noqa: F811
    page, origin = ui_page
    page.set_viewport_size({'width': width, 'height': 900 if width == 1440 else 844})
    state, posts, errors = mount(page, origin)
    state['value'].update(mode='wired', state='configured', interface='enp2s0', selected_interface_id=CHOICE,
                          vm_state='running', can_configure=False, home={
                              'state': 'ready', 'interface': 'enp7s0', 'ipv4': ['192.168.50.20'],
                              'ipv6': ['fd12:3456:789a:1:1234:5678:9abc:def0'],
                              'internal_url': 'http://192.168.50.20:8123',
                              'observed_at': '2026-10-17T00:00:00Z'})
    page.clock.run_for(5500)
    expect(page.get_by_text('HA 已連上家庭網路：192.168.50.20')).to_be_visible()
    expect(page.get_by_text('可到「裝置與服務」新增設備，再實測控制。')).to_be_visible()
    page.locator('summary').filter(has_text='連線資料').click()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    output = Path('evidence/ui/ha-lan')
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f'wired-fixture-{width}.png'), full_page=True)
    before = copy.deepcopy(state['value'])
    state['value']['home']['state'] = 'adapter_selection_required'
    page.clock.run_for(5500)
    expect(page.get_by_text('正在完成家庭設備探索設定。')).to_be_visible()
    expect(page.get_by_role('link', name='開啟家庭網路設定 ↗')).to_have_count(0)
    assert before['home']['state'] == 'ready' and not posts and not errors


def test_multiple_nics_require_choice_and_disable_uses_explicit_null(ui_page):  # noqa: F811
    page, origin = ui_page
    state, posts, errors = mount(page, origin)
    state['value']['interfaces'].append({'id': 'b' * 64, 'label': 'USB', 'interface': 'enx2', 'address': None, 'network': None})
    page.clock.run_for(5500)
    save = page.get_by_role('button', name='連上家庭網路')
    expect(save).to_be_disabled()
    page.get_by_label('接到家庭路由器的網路線').select_option(CHOICE)
    save.click()
    assert posts[0][0] == {'action': 'configure_lan', 'interface_id': CHOICE}
    state['job']['state'] = 'succeeded'
    state['value'].update(mode='wired', state='configured', selected_interface_id=CHOICE, interface='enp2s0')
    page.clock.run_for(5500)
    page.get_by_role('button', name='使用基本網路').click()
    assert posts[-1][0] == {'action': 'configure_lan', 'interface_id': None}
    assert not errors


def test_failed_connection_explains_automatic_nat_fallback(ui_page):  # noqa: F811
    page, origin = ui_page
    state, posts, errors = mount(page, origin)
    page.get_by_role('button', name='連上家庭網路').click()
    state['job'].update(state='failed', message='ha_lan_connection_failed_nat_restored')
    page.clock.run_for(5500)
    expect(page.get_by_role('alert')).to_contain_text('已恢復基本網路')
    expect(page.get_by_role('button', name='連上家庭網路')).to_be_enabled()
    assert not errors


def test_legacy_incomplete_core_settings_offer_one_owner_reconnect(ui_page):  # noqa: F811
    page, origin = ui_page
    state, posts, errors = mount(page, origin)
    state['value'].update(mode='wired', state='configured', selected_interface_id=CHOICE, interface='enp2s0',
                          core_configuration_pending=False, home={'state': 'adapter_selection_required'})
    page.clock.run_for(5500)
    expect(page.get_by_role('button', name='連上家庭網路')).to_be_enabled()
    page.get_by_role('button', name='連上家庭網路').click()
    assert posts[0][0] == {'action': 'configure_lan'} and not errors


@pytest.mark.parametrize('remaining', ['chosen', 'other'])
def test_explicit_nic_selection_survives_inventory_changes(ui_page, remaining):  # noqa: F811
    page, origin = ui_page
    state, posts, errors = mount(page, origin)
    other = {'id': 'b' * 64, 'label': 'USB', 'interface': 'enx2', 'address': None, 'network': None}
    state['value']['interfaces'].append(other)
    page.clock.run_for(5500)
    selector = page.get_by_label('接到家庭路由器的網路線')
    selector.select_option(CHOICE)
    state['value']['interfaces'] = [state['value']['interfaces'][0] if remaining == 'chosen' else other]
    page.clock.run_for(5500)
    save = page.get_by_role('button', name='連上家庭網路')
    if remaining == 'other':
        expect(page.get_by_text('原先選擇的網路卡目前無法使用，請重新選擇。')).to_be_visible()
        expect(selector).to_be_visible()
        expect(save).to_be_disabled()
        assert not posts
        selector.select_option(other['id'])
    expect(save).to_be_enabled()
    save.click()
    assert posts[0][0] == {'action': 'configure_lan', 'interface_id': CHOICE if remaining == 'chosen' else other['id']}
    assert not errors
