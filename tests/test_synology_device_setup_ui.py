"""Compiled NAS setup card; synthetic protocol replies, accounts and devices only."""
import copy
import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import expect
from test_conversation_discovery_ui import (  # noqa: F401
    conversation_browser,
    printer_result,
    sent_messages,
    setup_posts,
    setup_result,
)

NAME = '書房 NAS（合成設備）'
IDENTIFIER = 'f' * 32
USERNAME = 'synthetic-nas-owner'
PASSWORD = 'SYNTHETIC-NAS-PASSWORD-ONLY'
OTP = '823617'
PROPOSAL = ('確認後會先備份家庭設定，再用你輸入的帳號登入這台 NAS 並接入。'
            '帳號密碼只交給家庭系統保存，不會傳給管家，也不授予 AI 控制權限。')
MESSAGES = {
    'link': '請輸入這台 NAS 的帳號與密碼。',
    'invalid_auth': '帳號或密碼不正確，請再輸入一次。',
    '2sa': '這個帳號開啟了兩步驟驗證。請輸入驗證器 App 目前顯示的驗證碼；通過後 NAS 會記住這台家庭系統。',
    'otp_failed': '驗證碼不正確或已過期，請輸入新的驗證碼。',
    'backup_share': '家庭系統可以把備份存到這台 NAS。請選一個共用資料夾。',
}
OPTIONS = [{'value': '/volume1/home-backups', 'label': '家庭備份'},
           {'value': '/volume2/long-share', 'label': '另一個共用資料夾（合成資料）'}]


def nas_discovery(method='ssdp'):
    value = printer_result()
    value['candidates'][0].update(id='synthetic-nas-flow', integration_domain='synology_dsm',
        title=NAME, integration_name='網路儲存設備', discovery_method=method, confirmation_step='link',
        next_step='選擇「協助設定」，在接入卡輸入 NAS 帳號密碼；帳號密碼不會傳給管家。')
    return value


def nas_setup():
    value = setup_result(nas_discovery()['candidates'][0])
    value.update(id=IDENTIFIER, message=PROPOSAL, input=None)
    value['candidate']['confirmation_step'] = 'link'
    return value


def waiting(value, step, error=None, can_submit=True):
    value.update(state='needs_input', revision=value['revision'] + 1, can_approve=False,
        can_reconcile=True, message=MESSAGES[error or step],
        input={'step': step, 'error': error, 'can_submit': can_submit,
               'options': copy.deepcopy(OPTIONS) if step == 'backup_share' else []})
    return value


def configured(value):
    value.update(state='configured', revision=value['revision'] + 1, input=None, can_approve=False,
        can_reconcile=True, message='已核對這台 NAS 的接入設定。', backup_id='synthetic-ha-backup',
        integration={'id': 'synthetic-nas-entry', 'domain': 'synology_dsm', 'title': NAME, 'state': 'loaded'},
        device_count=1, entity_count=2, available_entity_count=2, observed_at='2026-10-19T12:01:00Z')
    return value


@pytest.fixture
def nas_browser(conversation_browser):  # noqa: F811
    page, state = conversation_browser
    state.update(response={'discovery': nas_discovery()}, next_step=('2sa', None), held=[],
                 hold_reply=False, drop_reply=False, read_transition=None, detail_override=None,
                 fail_reads=False, consoles=[])
    page.on('console', lambda message: state['consoles'].append(message.text))

    def handle(route):
        request = route.request
        path = request.url.split('/api/', 1)[1]
        body = request.post_data_json if request.method == 'POST' else None
        state['requests'].append((request.method, path, body, request.headers.get('idempotency-key')))
        if body is not None:
            assert request.headers.get('x-agentic-csrf') == 'fixture-csrf'
            assert request.headers.get('idempotency-key')
        if state['fail_reads'] and body is None:
            route.abort('failed')
            return
        if path == 'device-setups' and body is None:
            result = {'contract_version': 1, 'setups': list(state['setups'].values())}
        elif path == 'device-setups':
            assert body == {'candidate_id': 'synthetic-nas-flow', 'integration_domain': 'synology_dsm'}
            result = nas_setup()
            result['candidate']['discovery_method'] = state['response']['discovery']['candidates'][0]['discovery_method']
            state['setups'][IDENTIFIER] = result
        else:
            assert path.split('/')[1] == IDENTIFIER
            result = state['setups'][IDENTIFIER]
            if path.endswith('/approve'):
                assert body == {'expected_revision': result['revision'], 'proposal_digest': result['proposal_digest'],
                                'credentials': {'username': USERNAME, 'password': PASSWORD}}
                result.update(state='preparing', revision=result['revision'] + 1, can_approve=False,
                              input=None, message='正在備份家庭設定，完成後才會接入。')
            elif path.endswith('/input'):
                assert body['expected_revision'] == result['revision']
                assert body['step'] == result['input']['step']
                if body['step'] == 'link':
                    assert body == {'expected_revision': result['revision'], 'step': 'link',
                                    'username': USERNAME, 'password': PASSWORD}
                elif body['step'] == '2sa':
                    assert body == {'expected_revision': result['revision'], 'step': '2sa', 'otp_code': OTP}
                else:
                    assert body == {'expected_revision': result['revision'], 'step': 'backup_share',
                                    'backup_share': OPTIONS[0]['value']}
                if state['next_step'] == 'configured':
                    configured(result)
                else:
                    waiting(result, *state['next_step'])
            elif path.endswith('/reconcile'):
                assert body == {'expected_revision': result['revision']}
                waiting(result, result['input']['step'])
            elif state['read_transition']:
                transition, state['read_transition'] = state['read_transition'], None
                if transition == 'applying':
                    result.update(state='applying', revision=result['revision'] + 1,
                                  message='正在接入這台 NAS。')
                else:
                    waiting(result, *transition)
            if body is None and state['detail_override']:
                result = {**copy.deepcopy(result), **state['detail_override']}
        if body is not None and state['drop_reply']:
            state['drop_reply'] = False
            route.abort('failed')
        elif body is not None and state['hold_reply']:
            state['hold_reply'] = False
            state['held'].append((route, copy.deepcopy(result)))
        else:
            route.fulfill(status=202 if body is not None else 200,
                          content_type='application/json', body=json.dumps(result))

    page.route('**/api/device-setups**', handle)
    yield page, state
    for route, result in state['held']:
        try:
            route.fulfill(content_type='application/json', body=json.dumps(result))
        except Exception:
            pass  # Revoked or closed pages may have cancelled the request.


def open_card(page, state):
    page.goto(state['url'] + '#gateway')
    page.get_by_role('button', name='探索家庭設備', exact=True).click()
    results = page.get_by_role('region', name='設備探索結果', exact=True)
    panel = results.get_by_role('region', name='接入 ' + NAME, exact=True)
    if not state['setups']:
        results.get_by_role('button', name='協助設定', exact=True).click()
    expect(panel.get_by_role('heading')).to_be_visible()
    expect(panel).to_have_attribute('aria-busy', 'false')
    return panel


def login(panel, button='登入並接入'):
    expect(panel.get_by_role('button', name=button, exact=True)).to_be_disabled()
    panel.get_by_label('NAS 帳號', exact=True).fill(USERNAME)
    panel.get_by_label('NAS 密碼', exact=True).fill(PASSWORD)
    expect(panel.get_by_role('button', name=button, exact=True)).to_be_enabled()
    panel.get_by_label('NAS 密碼', exact=True).press('Enter')


def assert_secrets_cleared(page, state, secret, suffix, occurrences=1):
    assert secret not in page.content()
    assert secret not in page.url
    assert secret not in page.evaluate('JSON.stringify([localStorage, sessionStorage])')
    assert secret not in '\n'.join(state['consoles'])
    requests = [(method, path, body, key) for method, path, body, key in state['requests']
                if secret in json.dumps(body, ensure_ascii=False)]
    assert len(requests) == occurrences
    assert all(method == 'POST' and path.endswith(suffix) and key for method, path, _, key in requests)


def test_nas_happy_path_clears_secrets_and_reads_configured_result(nas_browser):
    page, state = nas_browser
    page.clock.install()
    panel = open_card(page, state)
    expect(panel).to_contain_text(PROPOSAL)
    expect(panel.get_by_label('NAS 帳號', exact=True)).to_have_attribute('autocomplete', 'username')
    expect(panel.get_by_label('NAS 密碼', exact=True)).to_have_attribute('type', 'password')
    expect(panel.get_by_label('NAS 密碼', exact=True)).to_have_attribute('autocomplete', 'current-password')
    assert panel.locator('form').count() == 1
    state['hold_reply'] = True
    login(panel)
    expect(panel.get_by_label('NAS 帳號', exact=True)).to_have_value('')
    expect(panel.get_by_label('NAS 密碼', exact=True)).to_have_value('')
    assert_secrets_cleared(page, state, PASSWORD, '/approve')
    assert_secrets_cleared(page, state, USERNAME, '/approve')
    route, result = state['held'].pop()
    route.fulfill(status=202, content_type='application/json', body=json.dumps(result))
    expect(panel.get_by_role('status')).to_contain_text('正在備份家庭設定')
    state['read_transition'] = 'applying'
    page.clock.run_for(3100)
    expect(panel.get_by_role('status')).to_contain_text('正在接入這台 NAS')
    state['read_transition'] = ('2sa', None)
    page.clock.run_for(3100)
    otp = panel.get_by_label('驗證碼', exact=True)
    expect(otp).to_have_attribute('inputmode', 'numeric')
    expect(otp).to_have_attribute('autocomplete', 'one-time-code')
    expect(otp).to_have_attribute('maxlength', '8')
    otp.fill('123')
    expect(panel.get_by_role('button', name='送出驗證碼', exact=True)).to_be_disabled()
    otp.fill(OTP)
    state.update(next_step=('2sa', 'otp_failed'), hold_reply=True)
    otp.press('Enter')
    expect(otp).to_have_value('')
    route, result = state['held'].pop()
    route.fulfill(status=202, content_type='application/json', body=json.dumps(result))
    expect(panel.get_by_role('alert')).to_have_text(MESSAGES['otp_failed'])
    assert_secrets_cleared(page, state, OTP, '/input')
    state['next_step'] = ('backup_share', None)
    otp.fill(OTP)
    panel.get_by_role('button', name='送出驗證碼', exact=True).click()
    group = panel.get_by_role('group', name='備份資料夾', exact=True)
    expect(group.get_by_role('radio')).to_have_count(2)
    assert group.locator(':checked').count() == 0
    expect(panel.get_by_role('button', name='使用這個資料夾', exact=True)).to_be_disabled()
    group.get_by_label('家庭備份', exact=True).check()
    state['next_step'] = 'configured'
    panel.get_by_role('button', name='使用這個資料夾', exact=True).click()
    expect(panel.get_by_text('家庭已載入這項設備設定。', exact=True)).to_be_visible()
    expect(panel).to_contain_text('1 筆設備登錄 · 2 / 2 個功能目前可讀取')
    expect(panel).to_contain_text('尚未驗證實體功能')
    posts = setup_posts(state)
    assert [path for path, _, _ in posts] == ['device-setups', f'device-setups/{IDENTIFIER}/approve',
                                           *[f'device-setups/{IDENTIFIER}/input'] * 3]
    assert len({key for _, _, key in posts}) == len(posts)
    assert_secrets_cleared(page, state, PASSWORD, '/approve')
    assert_secrets_cleared(page, state, OTP, '/input', occurrences=2)
    assert len(sent_messages(state)) == 1


def test_invalid_auth_returns_to_link_with_fresh_explicit_submission(nas_browser):
    page, state = nas_browser
    page.clock.install()
    panel = open_card(page, state)
    login(panel)
    expect(panel.get_by_role('status')).to_contain_text('正在備份家庭設定')
    state['read_transition'] = ('link', 'invalid_auth')
    page.clock.run_for(3100)
    expect(panel.get_by_role('alert')).to_have_text(MESSAGES['invalid_auth'])
    state['next_step'] = ('link', 'invalid_auth')
    login(panel, '重新登入')
    expect(panel.get_by_label('NAS 密碼', exact=True)).to_have_value('')
    login(panel, '重新登入')
    expect(panel.get_by_label('NAS 帳號', exact=True)).to_have_value('')
    posts = setup_posts(state)
    assert len(posts) == 4 and len({key for _, _, key in posts}) == 4
    assert [path for path, _, _ in posts][-2:] == [f'device-setups/{IDENTIFIER}/input'] * 2
    assert PASSWORD not in page.content()


def test_expired_input_only_reconciles_before_accepting_input(nas_browser):
    page, state = nas_browser
    state['setups'][IDENTIFIER] = waiting(nas_setup(), '2sa', can_submit=False)
    panel = open_card(page, state)
    expect(panel.get_by_text('這一步已逾時，請重新讀取後繼續。', exact=True)).to_be_visible()
    expect(panel.locator('form,input')).to_have_count(0)
    expect(panel.get_by_role('button')).to_have_count(1)
    panel.get_by_role('button', name='重新讀取', exact=True).click()
    expect(panel.get_by_label('驗證碼', exact=True)).to_be_visible()
    assert setup_posts(state) == [(f'device-setups/{IDENTIFIER}/reconcile', {'expected_revision': 2},
                                   setup_posts(state)[0][2])]


def test_expired_input_recovers_lost_reconcile_and_failed_readback(nas_browser):
    page, state = nas_browser
    state['setups'][IDENTIFIER] = waiting(nas_setup(), '2sa', can_submit=False)
    panel = open_card(page, state)
    state.update(drop_reply=True, fail_reads=True)
    panel.get_by_role('button', name='重新讀取', exact=True).click()
    expect(panel.get_by_role('alert')).to_be_visible()
    expect(panel).to_have_attribute('aria-busy', 'false')
    state['fail_reads'] = False
    panel.get_by_role('button', name='重新讀取', exact=True).click()
    expect(panel.get_by_label('驗證碼', exact=True)).to_be_enabled()
    assert [path for path, _, _ in setup_posts(state)] == [f'device-setups/{IDENTIFIER}/reconcile']


@pytest.mark.parametrize('step', ['approve', 'link', '2sa', 'backup_share'])
@pytest.mark.parametrize('readback', [True, False])
def test_lost_reply_reads_only_and_never_replays_secrets(nas_browser, step, readback):
    page, state = nas_browser
    if step != 'approve':
        state['setups'][IDENTIFIER] = waiting(nas_setup(), step)
    panel = open_card(page, state)
    state.update(drop_reply=True, fail_reads=not readback)
    if step in {'approve', 'link'}:
        login(panel, '登入並接入' if step == 'approve' else '重新登入')
    elif step == '2sa':
        panel.get_by_label('驗證碼', exact=True).fill(OTP)
        panel.get_by_role('button', name='送出驗證碼', exact=True).click()
    else:
        panel.get_by_label('家庭備份', exact=True).check()
        panel.get_by_role('button', name='使用這個資料夾', exact=True).click()
    expect(panel).to_have_attribute('aria-busy', 'false' if step != 'approve' or not readback else 'true')
    if not readback:
        expect(panel.get_by_role('alert')).to_be_visible()
        state['fail_reads'] = False
        panel.get_by_role('button', name='重新讀取', exact=True).click()
    if step == 'approve':
        expect(panel.get_by_role('status')).to_contain_text('正在備份家庭設定')
    else:
        expect(panel.get_by_label('驗證碼', exact=True)).to_have_value('')
    posts = setup_posts(state)
    assert len(posts) == (2 if step == 'approve' else 1)
    count = len(state['requests'])
    page.reload()
    expect(page.get_by_role('region', name='接入 ' + NAME, exact=True)).to_be_visible()
    assert len(state['requests']) > count
    assert setup_posts(state) == posts
    secret = PASSWORD if step in {'approve', 'link'} else OTP if step == '2sa' else None
    if secret:
        assert_secrets_cleared(page, state, secret, '/approve' if step == 'approve' else '/input')


def test_history_can_submit_otp_without_sending_composer_or_approving(nas_browser):
    page, state = nas_browser
    state['setups'][IDENTIFIER] = waiting(nas_setup(), '2sa')
    page.goto(state['url'] + '#gateway')
    draft = page.get_by_label('對話訊息', exact=True)
    draft.fill('保留這份草稿')
    page.get_by_role('button', name='設備接入進度', exact=True).click()
    history = page.get_by_role('region', name='設備接入進度', exact=True)
    history.get_by_role('button').filter(has_text='紀錄 ffffffff').click()
    panel = history.get_by_role('region', name='接入 ' + NAME, exact=True)
    state['next_step'] = 'configured'
    panel.get_by_label('驗證碼', exact=True).fill(OTP)
    panel.get_by_label('驗證碼', exact=True).press('Enter')
    expect(panel.get_by_text('家庭已載入這項設備設定。', exact=True)).to_be_visible()
    expect(draft).to_have_value('保留這份草稿')
    assert sent_messages(state) == []
    assert [path for path, _, _ in setup_posts(state)] == [f'device-setups/{IDENTIFIER}/input']
    assert_secrets_cleared(page, state, OTP, '/input')
    assert not page.locator('form form').count()


@pytest.mark.parametrize('event', ['agentic-session-expired', 'agentic-bridge-expired',
                                 'agentic-bridge-unavailable', 'unmount'])
@pytest.mark.parametrize('step', ['link', '2sa'])
def test_unsubmitted_secrets_are_cleared_on_session_loss_or_unmount(nas_browser, event, step):
    page, state = nas_browser
    if step == '2sa':
        state['setups'][IDENTIFIER] = waiting(nas_setup(), '2sa')
    panel = open_card(page, state)
    if step == 'link':
        panel.get_by_label('NAS 帳號', exact=True).fill(USERNAME)
        panel.get_by_label('NAS 密碼', exact=True).fill(PASSWORD)
    else:
        panel.get_by_label('驗證碼', exact=True).fill(OTP)
    if event == 'unmount':
        page.get_by_role('button', name='新對話', exact=True).click()
    else:
        page.evaluate('(name)=>window.dispatchEvent(new Event(name))', event)
    if event == 'agentic-bridge-unavailable':
        field = panel.get_by_label('NAS 密碼' if step == 'link' else '驗證碼', exact=True)
        expect(field).to_have_value('')
    else:
        expect(page.locator('.device-setup-input')).to_have_count(0)
    assert PASSWORD not in page.content()
    assert USERNAME not in page.content()
    assert OTP not in page.content()
    assert not any(path.endswith(('/approve', '/input')) for path, _, _ in setup_posts(state))


def test_needs_input_does_not_poll(nas_browser):
    page, state = nas_browser
    page.clock.install()
    state['setups'][IDENTIFIER] = waiting(nas_setup(), '2sa')
    panel = open_card(page, state)
    expect(panel.get_by_label('驗證碼', exact=True)).to_be_enabled()
    reads = [path for method, path, _, _ in state['requests'] if method == 'GET' and path.startswith('device-setups')]
    page.clock.run_for(12000)
    assert [path for method, path, _, _ in state['requests']
            if method == 'GET' and path.startswith('device-setups')] == reads


@pytest.mark.parametrize('changed', [
    {'input': None},
    {'input': []},
    {'input': {'step': 'unknown', 'error': None, 'options': [], 'can_submit': True}},
    {'input': {'step': ['2sa'], 'error': None, 'options': [], 'can_submit': True}},
    {'input': {'step': '2sa', 'error': 'unsafe', 'options': [], 'can_submit': True}},
    {'input': {'step': '2sa', 'error': ['otp_failed'], 'options': [], 'can_submit': True}},
    {'input': {'step': '2sa', 'options': [], 'can_submit': True}},
    {'input': {'step': '2sa', 'error': None, 'options': None, 'can_submit': True}},
    {'input': {'step': '2sa', 'error': None, 'options': [], 'can_submit': 'true'}},
    {'input': {'step': '2sa', 'error': None, 'options': OPTIONS, 'can_submit': True}},
    {'input': {'step': 'backup_share', 'error': None, 'options': OPTIONS * 2, 'can_submit': True}},
    {'input': {'step': 'backup_share', 'error': None, 'options': [
        {'value': f'/share/{i}', 'label': '資料夾'} for i in range(65)], 'can_submit': True}},
    {'input': {'step': 'backup_share', 'error': None, 'options': [
        {'value': 'x' * 256, 'label': '資料夾'}], 'can_submit': True}},
    {'input': {'step': 'backup_share', 'error': None, 'options': [
        {'value': '/share', 'label': 'x' * 161}], 'can_submit': True}},
    {'input': {'step': 'backup_share', 'error': None, 'options': [
        {'value': 1, 'label': '資料夾'}], 'can_submit': True}},
    {'input': {'step': 'backup_share', 'error': None, 'options': [
        {'value': '/share', 'label': 1}], 'can_submit': True}},
    {'state': 'configured'},
])
def test_invalid_input_projection_never_exposes_a_submission(nas_browser, changed):
    page, state = nas_browser
    value = waiting(nas_setup(), '2sa')
    value.update(changed)
    state['setups'][IDENTIFIER] = value
    page.goto(state['url'] + '#gateway')
    page.get_by_role('button', name='探索家庭設備', exact=True).click()
    expect(page.get_by_text('接入資料尚未確認，請重新讀取。', exact=True)).to_be_visible()
    expect(page.locator('.device-setup-input')).to_have_count(0)
    assert setup_posts(state) == []


@pytest.mark.parametrize('method', ['ssdp', 'zeroconf'])
def test_synology_supports_both_discovery_methods(nas_browser, method):
    page, state = nas_browser
    state['response'] = {'discovery': nas_discovery(method)}
    panel = open_card(page, state)
    expect(panel.get_by_label('NAS 密碼', exact=True)).to_be_visible()


@pytest.mark.parametrize('mode', ['host', 'bridge'])
def test_mobile_nas_card_opens_with_credentials_and_primary_in_view(nas_browser, mode):
    page, state = nas_browser
    state['mode'] = mode
    page.set_viewport_size({'width': 390, 'height': 844})
    panel = open_card(page, state)
    controls = [panel.get_by_label('NAS 帳號', exact=True), panel.get_by_label('NAS 密碼', exact=True),
                panel.get_by_role('button', name='登入並接入', exact=True)]
    for control in controls:
        box = control.bounding_box()
        assert box and box['height'] >= 44 and 0 <= box['y'] <= 844 - box['height']
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')


@pytest.mark.parametrize('changed', [{'source_id': 'network_inventory'}, {'discovery_method': 'dhcp'},
                                  {'confirmation_step': None}, {'confirmation_step': 'confirm_discovery'}])
def test_wrong_source_or_step_never_offers_nas_login(nas_browser, changed):
    page, state = nas_browser
    state['response']['discovery']['candidates'][0].update(changed)
    page.goto(state['url'] + '#gateway')
    page.get_by_role('button', name='探索家庭設備', exact=True).click()
    expect(page.get_by_role('region', name='設備探索結果', exact=True)).to_be_visible()
    expect(page.get_by_label('NAS 密碼', exact=True)).to_have_count(0)
    assert setup_posts(state) == []


@pytest.mark.parametrize('scene', ['proposal', '2sa', 'backup_share', 'error'])
def test_nas_synthetic_screenshots(nas_browser, tmp_path, scene):
    page, state = nas_browser
    if scene != 'proposal':
        state['setups'][IDENTIFIER] = waiting(nas_setup(), 'link' if scene == 'error' else scene,
                                             'invalid_auth' if scene == 'error' else None)
    panel = open_card(page, state)
    button_name = {'proposal': '登入並接入', '2sa': '送出驗證碼',
                   'backup_share': '使用這個資料夾', 'error': '重新登入'}[scene]
    evidence = Path(os.environ.get('SYNOLOGY_UI_EVIDENCE', str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    for width in (1440, 768, 390):
        height = 900 if width == 1440 else 844
        page.set_viewport_size({'width': width, 'height': height})
        primary = panel.get_by_role('button', name=button_name, exact=True)
        panel.scroll_into_view_if_needed()
        primary.scroll_into_view_if_needed()
        box = primary.bounding_box()
        assert box and box['height'] >= 44 and 0 <= box['y'] <= height - box['height']
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        for field in panel.locator('input:not([type=radio])').all():
            expect(field).to_have_value('')
            assert field.evaluate('element=>parseFloat(getComputedStyle(element).fontSize)') >= 16
        for radio in panel.get_by_role('radio').all():
            assert radio.locator('..').bounding_box()['height'] >= 44
        if scene == 'error':
            notice = panel.get_by_role('alert').bounding_box()
            assert notice and 0 <= notice['y'] <= height - notice['height']
        for field in panel.locator('input').all():
            field_box = field.bounding_box()
            assert field_box and 0 <= field_box['y'] <= height - field_box['height']
        page.screenshot(path=str(evidence / f'synology-{scene}-synthetic-{width}.png'))
    assert not any(path.endswith(('/approve', '/input')) for path, _, _ in setup_posts(state))
