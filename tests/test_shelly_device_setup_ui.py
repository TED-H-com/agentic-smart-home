"""Compiled Shelly setup UI with synthetic HA/account/device replies only."""
import copy
import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import expect
from test_conversation_discovery_ui import (  # noqa: F401
    conversation_browser, printer_result, sent_messages, setup_posts, setup_result,
)

NAME = '玄關 Shelly 1（合成設備）'
REVIEW = '確認後先備份家庭設定，再交由家庭接入；原生整合可能調整設備連線，不會切換繼電器或授予 AI 控制權限。'
IDENTIFIER = 'e' * 32


def shelly_discovery():
    value = printer_result()
    value['candidates'][0].update(id='synthetic-shelly-flow', integration_domain='shelly',
        title=NAME, integration_name='Shelly 設備', confirmation_step='confirm_discovery',
        next_step='先核對型號，再確認是否交由家庭接入。')
    return value


def shelly_setup(candidate=None):
    candidate = candidate or shelly_discovery()['candidates'][0]
    value = setup_result(candidate)
    value.update(id=IDENTIFIER, message=REVIEW)
    value['candidate']['confirmation_step'] = candidate.get('confirmation_step')
    return value


@pytest.fixture
def shelly_browser(conversation_browser):  # noqa: F811
    page, state = conversation_browser
    state.update(response={'discovery': shelly_discovery()}, setup_error=None, wrong_response=None,
                 detail_override=None, drop_setup_reply=False, drop_approval_reply=False, finish_setup=False)

    def handle(route):
        request = route.request
        path = request.url.split('/api/', 1)[1]
        body = request.post_data_json if request.method == 'POST' else None
        state['requests'].append((request.method, path, body, request.headers.get('idempotency-key')))
        if body is not None:
            assert request.headers.get('x-agentic-csrf') == 'fixture-csrf'
            assert request.headers.get('idempotency-key')
        if path == 'device-setups' and body is None:
            result = {'contract_version': 1, 'setups': list(state['setups'].values())}
        elif path == 'device-setups':
            assert body == {'candidate_id': 'synthetic-shelly-flow', 'integration_domain': 'shelly'}
            if state['setup_error']:
                route.fulfill(status=409, content_type='application/json', body=json.dumps({'detail': state['setup_error']}))
                return
            result = shelly_setup()
            if state['wrong_response'] == 'candidate_domain':
                result['candidate'].update(integration_domain='ipp', confirmation_step='zeroconf_confirm')
            elif state['wrong_response'] == 'confirmation_step':
                result['candidate']['confirmation_step'] = 'zeroconf_confirm'
            state['setups'][result['id']] = result
            if state.pop('drop_setup_reply', False):
                route.abort('failed')
                return
        else:
            identifier = path.split('/')[1]
            result = state['setups'][identifier]
            if path.endswith('/approve'):
                assert body == {'expected_revision': result['revision'], 'proposal_digest': result['proposal_digest']}
                result.update(state='preparing', can_approve=False, revision=result['revision'] + 1,
                              message='正在備份家庭設定，完成後才會接入。')
                if state.pop('drop_approval_reply', False):
                    route.abort('failed')
                    return
            elif path.endswith('/reconcile'):
                assert body == {'expected_revision': result['revision']}
                result.update(state='configured', can_reconcile=True, revision=result['revision'] + 1,
                              message='已核對合成設備的接入設定。')
            elif state['finish_setup'] and result['state'] in {'preparing', 'applying'}:
                result.update(state='configured', can_reconcile=True, revision=result['revision'] + 1,
                    message='已核對合成設備的接入設定。', backup_id='synthetic-ha-backup',
                    integration={'id': 'synthetic-entry', 'domain': 'shelly', 'title': NAME, 'state': 'loaded'},
                    device_count=1, entity_count=1, available_entity_count=1, observed_at='2026-10-19T12:01:00Z')
            if body is None and state['detail_override']:
                result = {**copy.deepcopy(result), **state['detail_override']}
        route.fulfill(content_type='application/json', body=json.dumps(result))

    page.route('**/api/device-setups**', handle)
    return page, state


def discover(page, state):
    page.goto(state['url'] + '#gateway')
    expect(page.get_by_role('button', name='探索家庭設備', exact=True)).to_be_enabled()
    assert setup_posts(state) == []
    page.get_by_role('button', name='探索家庭設備', exact=True).click()
    result = page.get_by_role('region', name='設備探索結果', exact=True)
    expect(result.get_by_text(NAME, exact=True)).to_be_visible()
    return result


def test_shelly_proposal_and_approval_are_explicit_and_device_neutral(shelly_browser, tmp_path):
    page, state = shelly_browser
    result = discover(page, state)
    expect(result.get_by_role('button', name='核對型號', exact=True)).to_be_enabled()
    assert setup_posts(state) == [] and len(sent_messages(state)) == 1
    result.get_by_role('button', name='核對型號', exact=True).click()
    panel = result.get_by_role('region', name='接入 ' + NAME, exact=True)
    expect(panel).to_contain_text('先備份家庭設定')
    expect(panel).to_contain_text('原生整合可能調整設備連線')
    expect(panel).to_contain_text('不會切換繼電器或授予 AI 控制權限')
    expect(panel.get_by_role('button', name='確認接入', exact=True)).to_be_enabled()
    assert [path for path, _, _ in setup_posts(state)] == ['device-setups']
    assert setup_posts(state)[0][1]['integration_domain'] == 'shelly'
    assert panel.get_by_label('密碼', exact=True).count() == 0
    evidence = Path(os.environ.get('SHELLY_UI_EVIDENCE', str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    for width in (1440, 768, 390):
        page.set_viewport_size({'width': width, 'height': 900 if width == 1440 else 844})
        approve = panel.get_by_role('button', name='確認接入', exact=True)
        approve.scroll_into_view_if_needed()
        assert approve.bounding_box()['height'] >= 44
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.screenshot(path=str(evidence / f'shelly-proposal-synthetic-{width}.png'), full_page=True)
    panel.get_by_role('button', name='確認接入', exact=True).click()
    expect(panel.get_by_role('status')).to_contain_text('正在備份家庭設定')
    assert len(setup_posts(state)) == 2
    state['finish_setup'] = True
    expect(panel.get_by_text('家庭已載入這項設備設定。', exact=True)).to_be_visible(timeout=10000)
    expect(panel.get_by_text('尚未驗證實體功能；可以先確認設備已開機。', exact=True)).to_be_visible()
    assert '印表機' not in panel.inner_text()
    assert len(setup_posts(state)) == 2


@pytest.mark.parametrize('changed', [
    {},
    {'confirmation_step': None},
    {'confirmation_step': 'zeroconf_confirm'},
    {'source_id': 'network_inventory'},
    {'integration_domain': 'synology_dsm'},
    {'discovery_method': 'dhcp'},
])
def test_shelly_wrong_source_step_or_domain_never_offers_approval(shelly_browser, changed):
    page, state = shelly_browser
    if changed:
        state['response']['discovery']['candidates'][0].update(changed)
    else:
        state['response']['discovery']['candidates'][0].pop('confirmation_step')
    result = discover(page, state)
    expect(result.get_by_role('button', name='核對型號', exact=True)).to_have_count(0)
    expect(result.get_by_role('button', name='確認接入', exact=True)).to_have_count(0)
    expect(result.locator('input[type=password]')).to_have_count(0)
    assert setup_posts(state) == []


def test_shelly_confirmation_step_array_is_not_coerced_to_authority(shelly_browser):
    page, state = shelly_browser
    state['response']['discovery']['candidates'][0]['confirmation_step'] = ['confirm_discovery']
    page.goto(state['url'] + '#gateway')
    page.get_by_role('button', name='探索家庭設備', exact=True).click()
    result = page.get_by_role('region', name='設備探索結果', exact=True)
    expect(result.get_by_text('暫時無法讀取探索結果，請再試一次。', exact=True)).to_be_visible()
    expect(result.get_by_role('button', name='核對型號', exact=True)).to_have_count(0)
    assert setup_posts(state) == []


@pytest.mark.parametrize('wrong', ['candidate_domain', 'confirmation_step', 'integration_domain'])
def test_shelly_rejects_rebound_setup_result(shelly_browser, wrong):
    page, state = shelly_browser
    if wrong == 'integration_domain':
        state['detail_override'] = {'integration': {'id': 'wrong-domain-entry', 'domain': 'ipp',
                                                   'title': '不同類型的設定', 'state': 'loaded'}}
    else:
        state['wrong_response'] = wrong
    result = discover(page, state)
    result.get_by_role('button', name='核對型號', exact=True).click()
    panel = result.get_by_role('region', name='接入 ' + NAME, exact=True)
    expect(panel.get_by_text('接入資料尚未確認，請重新讀取。', exact=True)).to_be_visible()
    if panel.get_by_role('button', name='確認接入', exact=True).count():
        expect(panel.get_by_role('button', name='確認接入', exact=True)).to_be_disabled()
    assert [path for path, _, _ in setup_posts(state)] == ['device-setups']


@pytest.mark.parametrize('reason', ['device_setup_model_unsupported', 'device_setup_core_unsupported', 'device_setup_flow_unsupported'])
def test_unsupported_shelly_has_native_handoff_without_retry_loop(shelly_browser, reason, tmp_path):
    page, state = shelly_browser
    state['setup_error'] = reason
    result = discover(page, state)
    result.get_by_role('button', name='核對型號', exact=True).click()
    panel = result.get_by_role('region', name='接入 ' + NAME, exact=True)
    expect(panel.get_by_role('heading', name='需要到家庭設定', exact=True)).to_be_visible()
    link = panel.get_by_role('link', name='開啟家庭設定 ↗', exact=True)
    expect(link).to_have_attribute('href', 'http://127.0.0.1:8123/config/integrations')
    expect(link).to_have_attribute('target', '_blank')
    expect(panel.get_by_role('button', name='確認接入', exact=True)).to_have_count(0)
    expect(panel.get_by_role('button', name='重試設定', exact=True)).to_have_count(0)
    if reason == 'device_setup_model_unsupported':
        expect(panel.get_by_text('這個型號尚未支援對話接入，請到家庭設定繼續。', exact=True)).to_be_visible()
        evidence = Path(os.environ.get('SHELLY_UI_EVIDENCE', str(tmp_path)))
        evidence.mkdir(parents=True, exist_ok=True)
        for width in (1440, 768, 390):
            page.set_viewport_size({'width': width, 'height': 900 if width == 1440 else 844})
            link.scroll_into_view_if_needed()
            assert link.bounding_box()['height'] >= 44
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            page.screenshot(path=str(evidence / f'shelly-unsupported-synthetic-{width}.png'), full_page=True)
    assert [path for path, _, _ in setup_posts(state)] == ['device-setups']


@pytest.mark.parametrize('lost', ['drop_setup_reply', 'drop_approval_reply'])
def test_shelly_lost_post_recovers_exact_saved_operation_without_resubmit(shelly_browser, lost):
    page, state = shelly_browser
    state[lost] = True
    result = discover(page, state)
    result.get_by_role('button', name='核對型號', exact=True).click()
    panel = result.get_by_role('region', name='接入 ' + NAME, exact=True)
    expect(panel.get_by_role('button', name='確認接入', exact=True)).to_be_enabled()
    panel.get_by_role('button', name='確認接入', exact=True).click()
    expect(panel.get_by_role('status')).to_contain_text('正在備份家庭設定')
    page.get_by_role('button', name='新對話', exact=True).click()
    page.reload()
    page.get_by_role('button', name='設備接入進度', exact=True).click()
    history = page.get_by_role('region', name='設備接入進度', exact=True)
    history.get_by_role('button').filter(has_text='紀錄 eeeeeeee').click()
    expect(history.get_by_role('status')).to_contain_text('正在備份家庭設定')
    assert [path for path, _, _ in setup_posts(state)] == ['device-setups', f'device-setups/{IDENTIFIER}/approve']
    state['finish_setup'] = True
    expect(history.get_by_text('家庭已載入這項設備設定。', exact=True)).to_be_visible(timeout=10000)
    history.get_by_role('button', name='請管家解讀結果', exact=True).click()
    expect(page.get_by_text('合成的管家回覆。', exact=True)).to_be_visible()
    assert sent_messages(state)[-1]['device_setup_id'] == IDENTIFIER
    assert len(setup_posts(state)) == 2
