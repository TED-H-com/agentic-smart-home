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
             "interface": None, "guest_mac": None, "vm_state": "shut off", "can_configure": True,
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
    expect(page.get_by_label('接到家庭路由器的網路線')).to_be_visible()
    return state, posts, errors


def test_explicit_wired_choice_job_readback_and_same_key_after_lost_response(ui_page):  # noqa: F811
    page, origin = ui_page
    state, posts, errors = mount(page, origin)
    assert posts == []
    save = page.get_by_role('button', name='保存家庭接線')
    expect(save).to_be_disabled()
    page.get_by_label('接到家庭路由器的網路線').select_option(CHOICE)
    state['lose'] = True
    save.click()
    expect(page.get_by_role('alert')).to_be_visible()
    save.click()
    expect(page.get_by_role('button', name='正在保存…')).to_be_disabled()
    assert len(posts) == 2 and posts[0] == posts[1]
    assert posts[0][0] == {'action': 'configure_lan', 'interface_id': CHOICE}
    assert posts[0][1]
    expect(page.get_by_role('region', name='家庭區網接入')).to_have_attribute('aria-busy', 'false')
    state['job']['state'] = 'succeeded'
    state['value'].update(mode='wired', state='configured', selected_interface_id=CHOICE, interface='enp2s0',
                          home={'state': 'needs_home_setup'})
    page.clock.run_for(5500)
    expect(page.get_by_text('已設定接線', exact=True)).to_be_visible()
    expect(page.get_by_text('接線設定已保存；完成家庭帳號與授權後，會檢查實際連線。')).to_be_visible()
    expect(save).to_be_disabled()
    assert not errors


def test_running_and_unreadable_network_never_offer_mutation(ui_page):  # noqa: F811
    page, origin = ui_page
    state, posts, errors = mount(page, origin)
    state['value'].update(vm_state='running', can_configure=False)
    page.clock.run_for(5500)
    expect(page.get_by_label('接到家庭路由器的網路線')).to_be_disabled()
    expect(page.get_by_text('更改接線前，請在主機維護將家庭正常關機。期間家庭自動化會暫停。')).to_be_visible()
    state['fail'] = True
    page.clock.run_for(5500)
    expect(page.get_by_role('alert')).to_contain_text('暫時無法讀取接線狀態')
    state['value'].update(vm_state='shut off', can_configure=True)
    state['fail'] = False
    page.clock.run_for(5500)
    expect(page.get_by_role('alert')).to_have_count(0)
    expect(page.get_by_label('接到家庭路由器的網路線')).to_be_enabled()
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
    expect(page.get_by_text('已讀回家庭的區網位址、探索介面與本機網址。')).to_be_visible()
    expect(page.get_by_text('接著到「裝置與服務」新增設備，並實測控制；這項檢查不代表所有設備已接通。')).to_be_visible()
    page.locator('summary').filter(has_text='連線資料').click()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    output = Path('evidence/ui/ha-lan')
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f'wired-fixture-{width}.png'), full_page=True)
    before = copy.deepcopy(state['value'])
    state['value']['home']['state'] = 'adapter_selection_required'
    page.clock.run_for(5500)
    expect(page.get_by_role('link', name='開啟家庭網路設定 ↗')).to_have_attribute('href', 'http://127.0.0.1:8123/config/network')
    assert before['home']['state'] == 'ready' and not posts and not errors
