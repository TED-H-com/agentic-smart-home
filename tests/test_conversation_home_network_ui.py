"""Compiled browser protocol fixtures; no real account, NIC or household effects."""
import copy
import json
import os
import time
from pathlib import Path

import pytest
from playwright.sync_api import expect
from test_conversation_discovery_ui import conversation_browser, sent_messages  # noqa: F401
from test_worker_ui import common

CHOICE = 'a' * 64
SCOPE = '1' * 64
STORAGE = 'agentic-home-network-job-' + SCOPE


@pytest.fixture
def chat_home_network(conversation_browser):  # noqa: F811
    page, state = conversation_browser
    state.update(lan={
        'contract_version': 1, 'mode': 'nat', 'state': 'disabled', 'selected_interface_id': None,
        'interface': None, 'guest_mac': None, 'vm_state': 'running', 'can_configure': False,
        'can_connect': True, 'interfaces': [{'id': CHOICE, 'label': '家用有線網路',
            'interface': 'enp2s0', 'address': '192.168.50.10', 'network': '192.168.50.0/24'}],
        'limitations': [], 'home': {'state': 'not_configured'}},
        lan_jobs={}, lan_posts=[], lan_gets=[], lan_mode='normal', lan_held=[], lan_rejections=[], hold_job=False)

    def route(intercept):
        req = intercept.request
        path = req.url.split('/api/', 1)[1]
        if path == 'network/ha-lan':
            state['lan_gets'].append(path)
            result = copy.deepcopy(state['lan'])
        elif path == 'host/status':
            result = {**common(path), 'jobs': list(state['lan_jobs'].values())[-50:]}
        elif path == 'host/actions':
            body, key = req.post_data_json, req.headers.get('idempotency-key')
            assert req.headers.get('x-agentic-csrf') == 'fixture-csrf' and key
            state['stored_before_post'] = page.evaluate('(key)=>sessionStorage.getItem(key)', STORAGE)
            state['lan_posts'].append((body, key))
            if state['lan_mode'].startswith('reject_'):
                state['lan_rejections'].append((intercept, int(state['lan_mode'].split('_')[1])))
                return
            result = {'id': key, 'request': {'id': key, **body}, 'state': 'running', 'at': time.time()}
            if state['lan_mode'] != 'lost_missing':
                state['lan_jobs'][key] = result
            if state['lan_mode'].startswith('lost'):
                intercept.abort('failed')
                return
        else:
            identifier = path.removeprefix('host/jobs/')
            state['lan_gets'].append(path)
            result = copy.deepcopy(state['lan_jobs'].get(identifier))
            if state['hold_job']:
                state['lan_held'].append((intercept, result))
                return
            if result is None:
                intercept.fulfill(status=404, content_type='application/json', body='{"detail":"host_job_not_found"}')
                return
        intercept.fulfill(status=200, content_type='application/json', body=json.dumps(result))

    for path in ('network/ha-lan', 'host/status', 'host/actions', 'host/jobs/*'):
        page.route('**/api/' + path, route)
    page.clock.install()
    yield page, state
    for route, result in state['lan_held']:
        route.fulfill(status=200, content_type='application/json', body=json.dumps(result))
    for route, status in state['lan_rejections']:
        route.fulfill(status=status, content_type='application/json', body='{"detail":"fixture_rejected"}')


def open_lan(page, state):
    page.goto(state['url'] + '#gateway')
    page.get_by_role('button', name='接通家庭網路', exact=True).click()
    panel = page.get_by_role('region', name='接通家庭網路', exact=True)
    expect(panel.get_by_role('button', name='連上家庭網路', exact=True)).to_be_enabled()
    return panel


def wait_submission(page, state):
    for _ in range(40):
        if state['lan_posts']:
            return
        page.wait_for_timeout(50)
    raise AssertionError('Expected one network submission')


def finish(page, state, job_state='succeeded'):
    for job in state['lan_jobs'].values():
        job['state'] = job_state
    state['lan'].update(mode='wired', state='configured', selected_interface_id=CHOICE,
        interface='enp2s0', guest_mac='02:00:00:00:00:01',
        home={'state': 'ready', 'ipv4': ['192.168.50.20'], 'observed_at': page.evaluate('new Date().toISOString()')})


@pytest.mark.parametrize('lost', [False, True])
def test_connect_in_chat_reads_exact_job_and_requires_fresh_explicit_sharing(chat_home_network, lost, tmp_path):
    page, state = chat_home_network
    state['lan_mode'] = 'lost_committed' if lost else 'normal'
    panel = open_lan(page, state)
    page.get_by_label('對話訊息', exact=True).fill('保留草稿：請接好家裡的設備。')
    assert not state['lan_posts'] and not sent_messages(state)
    panel.get_by_role('button', name='連上家庭網路', exact=True).click()
    wait_submission(page, state)
    expect(panel.get_by_role('button', name='正在連接…', exact=True)).to_be_disabled()
    key = state['lan_posts'][0][1]
    assert state['lan_posts'] == [({'action': 'configure_lan'}, key)]
    assert state['stored_before_post'] == key
    assert 'host/jobs/' + key in state['lan_gets']
    assert page.evaluate('(key)=>sessionStorage.getItem(key)', STORAGE) == key
    finish(page, state)
    page.clock.run_for(5500)
    proceed = panel.get_by_role('button', name='繼續探索家庭設備', exact=True)
    expect(proceed).to_be_enabled()
    assert not sent_messages(state)
    evidence = Path(os.environ.get('AGENTIC_HOME_NETWORK_EVIDENCE_DIR', str(tmp_path))) / ('lost-reply' if lost else 'normal')
    evidence.mkdir(parents=True, exist_ok=True)
    for width in (1440, 768, 390):
        page.set_viewport_size({'width': width, 'height': 900 if width == 1440 else 844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(evidence / f'chat-network-ready-{width}.png'), full_page=True)
    before = len(state['lan_gets'])
    proceed.click()
    expect(page.get_by_text('合成的管家回覆。', exact=True)).to_be_visible()
    assert len(state['lan_gets']) > before and len(sent_messages(state)) == 1
    assert sent_messages(state)[0]['discover_environment'] is True
    expect(page.get_by_label('對話訊息', exact=True)).to_have_value('保留草稿：請接好家裡的設備。')
    assert len(state['lan_posts']) == 1


def test_existing_maintenance_job_survives_reload_without_resubmitting(chat_home_network):
    """Regression: old HomeLanSettings forgets accepted jobs on every remount."""
    page, state = chat_home_network
    page.goto(state['url'] + '#host')
    page.locator('summary').filter(has_text='家庭區網接入').click()
    page.get_by_role('button', name='連上家庭網路', exact=True).click()
    wait_submission(page, state)
    expect(page.get_by_role('button', name='正在連接…', exact=True)).to_be_disabled()
    identifier = state['lan_posts'][0][1]
    page.reload()
    page.locator('summary').filter(has_text='家庭區網接入').click()
    expect(page.get_by_role('button', name='正在連接…', exact=True)).to_be_disabled()
    assert 'host/jobs/' + identifier in state['lan_gets']
    assert len(state['lan_posts']) == 1
    state['lan_jobs'][identifier].update(state='failed', message='建立還原點時遇到問題，尚未變更網路；請稍後重試。')
    page.clock.run_for(5500)
    expect(page.get_by_role('region', name='家庭區網接入')).to_contain_text('建立還原點時遇到問題')


def test_missing_job_after_lost_response_and_reload_stays_read_only(chat_home_network):
    page, state = chat_home_network
    state['lan_mode'] = 'lost_missing'
    panel = open_lan(page, state)
    panel.get_by_role('button', name='連上家庭網路', exact=True).click()
    wait_submission(page, state)
    expect(panel.get_by_text('正在核對原本的接入工作；尚未確認前不會重新送出。', exact=False)).to_be_visible()
    identifier = state['lan_posts'][0][1]
    page.reload()
    page.get_by_role('button', name='接通家庭網路', exact=True).click()
    expect(panel.get_by_role('button', name='連上家庭網路', exact=True)).to_be_disabled()
    panel.get_by_role('button', name='重新確認接入進度').click()
    assert len(state['lan_posts']) == 1 and not sent_messages(state)
    assert page.evaluate('(key)=>sessionStorage.getItem(key)', STORAGE) == identifier
    state['lan_jobs'][identifier] = {'id': identifier, 'request': {'id': identifier, 'action': 'configure_lan'},
                                    'state': 'running', 'at': time.time()}
    page.clock.run_for(5500)
    expect(panel.get_by_role('button', name='正在連接…', exact=True)).to_be_disabled()


@pytest.mark.parametrize('state_name', ['waiting_for_address', 'adapter_selection_required', 'callback_address_required', 'unavailable'])
def test_address_or_finished_job_does_not_offer_discovery(chat_home_network, state_name):
    page, state = chat_home_network
    panel = open_lan(page, state)
    panel.get_by_role('button', name='連上家庭網路', exact=True).click()
    wait_submission(page, state)
    finish(page, state)
    state['lan'].update(ha_lan_ip='192.168.50.20', home={'state': state_name})
    page.clock.run_for(5500)
    expect(panel.get_by_role('button', name='繼續探索家庭設備')).to_be_disabled()
    assert not sent_messages(state)


@pytest.mark.parametrize('invalid', ['expired', 'recovery', 'needs_reconcile', 'mismatch'])
def test_unknown_or_stale_results_cannot_continue(chat_home_network, invalid):
    page, state = chat_home_network
    panel = open_lan(page, state)
    panel.get_by_role('button', name='連上家庭網路', exact=True).click()
    wait_submission(page, state)
    finish(page, state)
    if invalid == 'expired':
        state['lan']['home']['observed_at'] = '2020-01-01T00:00:00Z'
    elif invalid == 'recovery':
        state['lan']['recovery_pending'] = True
    elif invalid == 'needs_reconcile':
        next(iter(state['lan_jobs'].values()))['state'] = 'needs_reconcile'
    else:
        next(iter(state['lan_jobs'].values()))['request']['action'] = 'restore'
    page.clock.run_for(5500)
    expect(panel.get_by_role('button', name='繼續探索家庭設備')).to_be_disabled()
    assert not sent_messages(state) and len(state['lan_posts']) == 1


def test_leave_new_thread_and_history_preserve_job_without_automatic_sharing(chat_home_network):
    page, state = chat_home_network
    panel = open_lan(page, state)
    page.get_by_label('對話訊息', exact=True).fill('草稿保留。')
    panel.get_by_role('button', name='連上家庭網路', exact=True).click()
    wait_submission(page, state)
    page.evaluate("location.hash='#home'")
    expect(page.get_by_role('heading', name='你想讓家做什麼？')).to_be_visible()
    finish(page, state)
    page.evaluate("location.hash='#gateway'")
    page.get_by_role('button', name='接通家庭網路', exact=True).click()
    expect(panel.get_by_role('button', name='繼續探索家庭設備')).to_be_enabled()
    expect(page.get_by_label('對話訊息', exact=True)).to_have_value('草稿保留。')
    page.get_by_role('button', name='對話紀錄', exact=True).click()
    expect(panel).to_have_count(0)
    assert not sent_messages(state) and len(state['lan_posts']) == 1
    page.get_by_role('button', name='對話紀錄', exact=True).click()
    page.get_by_role('button', name='送出訊息', exact=True).click()
    expect(page.get_by_text('合成的管家回覆。', exact=True)).to_be_visible()
    page.get_by_role('button', name='新對話', exact=True).click()
    expect(panel).to_have_count(0)
    page.get_by_role('button', name='接通家庭網路', exact=True).click()
    expect(panel.get_by_role('button', name='繼續探索家庭設備')).to_be_enabled()
    assert len(sent_messages(state)) == 1 and len(state['lan_posts']) == 1


@pytest.mark.parametrize('event', ['agentic-session-expired', 'hide', 'visibility'])
def test_late_readback_cannot_share_after_revocation_or_leaving(chat_home_network, event):
    page, state = chat_home_network
    panel = open_lan(page, state)
    panel.get_by_role('button', name='連上家庭網路', exact=True).click()
    wait_submission(page, state)
    finish(page, state)
    page.clock.run_for(5500)
    expect(panel.get_by_role('button', name='繼續探索家庭設備')).to_be_enabled()
    state['hold_job'] = True
    panel.get_by_role('button', name='繼續探索家庭設備').click()
    for _ in range(20):
        if state['lan_held']:
            break
        page.wait_for_timeout(50)
    assert state['lan_held']
    if event == 'hide':
        page.get_by_role('button', name='對話紀錄', exact=True).click()
    elif event == 'visibility':
        page.evaluate("()=>{Object.defineProperty(document,'visibilityState',{configurable:true,value:'hidden'});document.dispatchEvent(new Event('visibilitychange'))}")
    else:
        page.evaluate("window.dispatchEvent(new Event('agentic-session-expired'))")
    for route, result in state['lan_held']:
        route.fulfill(status=200, content_type='application/json', body=json.dumps(result))
    state['lan_held'].clear()
    page.wait_for_timeout(50)
    assert not sent_messages(state) and len(state['lan_posts']) == 1


def test_bridge_offers_host_instruction_without_host_requests(chat_home_network):
    page, state = chat_home_network
    state['mode'] = 'bridge'
    page.goto(state['url'] + '#gateway')
    page.get_by_role('button', name='接通家庭網路', exact=True).click()
    panel = page.get_by_role('region', name='接通家庭網路', exact=True)
    expect(panel).to_contain_text('請在主機桌面開啟 Agentic Home')
    expect(panel.get_by_role('button', name='連上家庭網路', exact=True)).to_have_count(0)
    assert not state['lan_gets'] and not state['lan_posts']


def test_cannot_submit_when_browser_cannot_persist_identity(chat_home_network):
    page, state = chat_home_network
    panel = open_lan(page, state)
    page.evaluate("()=>{Storage.prototype.setItem=function(){throw new Error('storage blocked')}}")
    panel.get_by_role('button', name='連上家庭網路', exact=True).click()
    expect(panel).to_contain_text('無法保存接入進度')
    assert not state['lan_posts'] and not sent_messages(state)



def test_another_surface_identity_cannot_be_overwritten_by_stale_panel(chat_home_network):
    page, state = chat_home_network
    panel = open_lan(page, state)
    identifier = 'b' * 32
    # The same-window storage write sends no storage event to this panel.
    page.evaluate('([key,id])=>sessionStorage.setItem(key,id)', [STORAGE, identifier])
    state['lan_jobs'][identifier] = {'id': identifier, 'request': {'id': identifier, 'action': 'configure_lan'},
                                    'state': 'running', 'at': time.time()}
    panel.get_by_role('button', name='連上家庭網路', exact=True).click()
    expect(panel.get_by_role('button', name='正在連接…', exact=True)).to_be_disabled()
    assert not state['lan_posts'] and not sent_messages(state)
    assert page.evaluate('(key)=>sessionStorage.getItem(key)', STORAGE) == identifier


def test_ready_network_can_be_shared_when_network_mutations_are_unavailable(chat_home_network):
    page, state = chat_home_network
    finish(page, state)
    state['lan']['can_connect'] = False
    page.goto(state['url'] + '#gateway')
    page.get_by_role('button', name='接通家庭網路', exact=True).click()
    panel = page.get_by_role('region', name='接通家庭網路', exact=True)
    expect(panel.get_by_role('button', name='連上家庭網路', exact=True)).to_be_disabled()
    expect(panel.get_by_role('button', name='繼續探索家庭設備')).to_be_enabled()
    panel.get_by_role('button', name='繼續探索家庭設備').click()
    expect(page.get_by_text('合成的管家回覆。', exact=True)).to_be_visible()
    assert not state['lan_posts'] and len(sent_messages(state)) == 1


def test_continue_rechecks_network_before_sharing(chat_home_network):
    page, state = chat_home_network
    finish(page, state)
    page.goto(state['url'] + '#gateway')
    page.get_by_role('button', name='接通家庭網路', exact=True).click()
    panel = page.get_by_role('region', name='接通家庭網路', exact=True)
    expect(panel.get_by_role('button', name='繼續探索家庭設備')).to_be_enabled()
    state['lan']['home'] = {'state': 'unavailable'}
    panel.get_by_role('button', name='繼續探索家庭設備').click()
    expect(panel.get_by_role('button', name='繼續探索家庭設備')).to_be_disabled()
    assert not state['lan_posts'] and not sent_messages(state)


def test_other_identity_does_not_restore_previous_scope_job(chat_home_network):
    page, state = chat_home_network
    panel = open_lan(page, state)
    panel.get_by_role('button', name='連上家庭網路', exact=True).click()
    wait_submission(page, state)
    identifier = state['lan_posts'][0][1]
    page.route('**/api/session', lambda route: route.fulfill(status=200, content_type='application/json',
        body=json.dumps({'principal': 'other-owner', 'csrf': 'fixture-csrf', 'mode': 'host', 'storage_scope': '2' * 64})))
    state['lan_gets'].clear()
    page.reload()
    page.get_by_role('button', name='接通家庭網路', exact=True).click()
    expect(panel.get_by_role('button', name='連上家庭網路', exact=True)).to_be_enabled()
    assert 'host/jobs/' + identifier not in state['lan_gets']
    assert len(state['lan_posts']) == 1 and not sent_messages(state)


def test_non_host_deployment_never_offers_host_network_actions(chat_home_network):
    page, state = chat_home_network
    state['mode'] = 'ingress'
    page.goto(state['url'] + '#gateway')
    expect(page.get_by_role('heading', name='你想讓家做什麼？')).to_be_visible()
    expect(page.get_by_role('button', name='接通家庭網路', exact=True)).to_have_count(0)
    assert not state['lan_gets'] and not state['lan_posts']



@pytest.mark.parametrize('status', [400, 409, 422])
@pytest.mark.parametrize('close_card', [False, True])
def test_definite_rejection_after_poll_resolves_only_its_pending_identity(chat_home_network, status, close_card):
    page, state = chat_home_network
    state['lan_mode'] = f'reject_{status}'
    panel = open_lan(page, state)
    panel.get_by_role('button', name='連上家庭網路', exact=True).click()
    wait_submission(page, state)
    identifier = state['lan_posts'][0][1]
    page.clock.run_for(5500)
    for _ in range(40):
        if 'host/jobs/' + identifier in state['lan_gets']:
            break
        page.wait_for_timeout(50)
    assert 'host/jobs/' + identifier in state['lan_gets']
    if close_card:
        panel.get_by_role('button', name='收起家庭網路', exact=True).click()
    route, response_status = state['lan_rejections'].pop()
    route.fulfill(status=response_status, content_type='application/json', body='{"detail":"fixture_rejected"}')
    page.wait_for_function('(key)=>sessionStorage.getItem(key)===null', arg=STORAGE)
    if close_card:
        page.get_by_role('button', name='接通家庭網路', exact=True).click()
    expect(panel.get_by_role('button', name='連上家庭網路', exact=True)).to_be_enabled()
    assert len(state['lan_posts']) == 1 and not sent_messages(state)


def test_old_rejection_does_not_clear_another_surface_pending_identity(chat_home_network):
    page, state = chat_home_network
    state['lan_mode'] = 'reject_409'
    panel = open_lan(page, state)
    panel.get_by_role('button', name='連上家庭網路', exact=True).click()
    wait_submission(page, state)
    newer = 'c' * 32
    page.evaluate('([key,id])=>sessionStorage.setItem(key,id)', [STORAGE, newer])
    state['lan_jobs'][newer] = {'id': newer, 'request': {'id': newer, 'action': 'configure_lan'},
                              'state': 'running', 'at': time.time()}
    page.clock.run_for(5500)
    route, status = state['lan_rejections'].pop()
    route.fulfill(status=status, content_type='application/json', body='{"detail":"fixture_rejected"}')
    expect(panel.get_by_role('region', name='家庭區網接入')).to_have_attribute('aria-busy', 'false')
    expect(panel.get_by_role('button', name='正在連接…', exact=True)).to_be_disabled()
    assert page.evaluate('(key)=>sessionStorage.getItem(key)', STORAGE) == newer
    assert len(state['lan_posts']) == 1 and not sent_messages(state)
