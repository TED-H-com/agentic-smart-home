"""Saved setup history in compiled UI; synthetic HA, account and device data only."""
import copy
import json
import re

import pytest
from playwright.sync_api import expect
from test_conversation_discovery_ui import (  # noqa: F401
    conversation_browser,
    printer_result,
    sent_messages,
    setup_posts,
    setup_result,
)


@pytest.fixture
def setup_history(conversation_browser):  # noqa: F811
    page, state = conversation_browser
    first = setup_result(printer_result()['candidates'][0])
    first.update(state='configured', can_approve=False, can_reconcile=True,
                 message='已核對合成接入設定。', observed_at='2026-10-19T12:01:00Z')
    second = copy.deepcopy(first)
    second.update(id='d' * 32, created_at='2026-10-19T12:02:00Z', updated_at='2026-10-19T12:02:00Z')
    # Same candidate/name deliberately: selection must bind the saved operation ID.
    state.update(setups={first['id']: first, second['id']: second}, held=[], hold_id=None,
                 invalid_list=None, invalid_detail=None, lose_reconcile=False)

    def handle(route):
        request = route.request
        path = request.url.split('/api/', 1)[1]
        body = request.post_data_json if request.method == 'POST' else None
        state['requests'].append((request.method, path, body, request.headers.get('idempotency-key')))
        if path == 'device-setups':
            assert request.method == 'GET', 'History must never create another setup.'
            result = state['invalid_list'] or {'contract_version': 1, 'setups': list(state['setups'].values())}
        else:
            identifier = path.split('/')[1]
            result = copy.deepcopy(state['setups'][identifier])
            if path.endswith('/reconcile'):
                assert request.method == 'POST'
                assert request.headers.get('x-agentic-csrf') == 'fixture-csrf'
                assert request.headers.get('idempotency-key')
                assert body == {'expected_revision': result['revision']}
                result.update(state='configured', revision=result['revision'] + 1,
                              message='已核對合成接入設定。')
                state['setups'][identifier] = result
                if state['lose_reconcile']:
                    route.abort('failed')
                    return
            else:
                assert request.method == 'GET', 'History must never approve or replay.'
                if state['invalid_detail']:
                    result.update(state['invalid_detail'])
        if state['hold_id'] == path:
            state['held'].append((route, copy.deepcopy(result)))
            return
        route.fulfill(content_type='application/json', body=json.dumps(result))

    page.route('**/api/device-setups**', handle)
    yield page, state
    for route, result in state['held']:
        try:
            route.fulfill(content_type='application/json', body=json.dumps(result))
        except Exception:
            pass  # A page closed after revocation may have cancelled the request.


def entry(page):
    return page.get_by_role('button', name='設備接入進度', exact=True)


def history_panel(page):
    return page.get_by_role('region', name='設備接入進度', exact=True)


def select(page, identifier='b' * 32):
    history_panel(page).get_by_role('button').filter(has_text=f'紀錄 {identifier[:8]}').click()
    return history_panel(page).get_by_role('region', name='接入 書房印表機（合成資料）', exact=True)


def setup_reads(state):
    return [path for method, path, _, _ in state['requests']
            if method == 'GET' and path.startswith('device-setups')]


def test_saved_setup_is_lazy_and_available_after_new_chat_and_reload(setup_history, tmp_path):
    page, state = setup_history
    page.goto(state['url'] + '#gateway')
    expect(entry(page)).to_be_enabled()
    assert setup_reads(state) == []
    page.get_by_label('對話訊息', exact=True).fill('先問一件事')
    page.get_by_role('button', name='送出訊息', exact=True).click()
    page.get_by_role('button', name='新對話', exact=True).click()
    entry(page).click()
    expect(history_panel(page).get_by_text('紀錄 bbbbbbbb', exact=True)).to_be_visible()
    assert setup_reads(state) == ['device-setups']
    panel = select(page)
    expect(panel.get_by_text('已核對合成接入設定。', exact=True)).to_be_visible()
    assert setup_reads(state) == ['device-setups', 'device-setups/' + 'b' * 32]
    assert setup_posts(state) == []
    for width in (1440, 768, 390):
        page.set_viewport_size({'width': width, 'height': 900 if width == 1440 else 844})
        share = panel.get_by_role('button', name='請管家解讀結果', exact=True)
        share.scroll_into_view_if_needed()
        assert share.bounding_box()['height'] >= 44
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.screenshot(path=str(tmp_path / f'setup-history-synthetic-{width}.png'), full_page=True)
    page.reload()
    expect(entry(page)).to_be_enabled()
    assert setup_reads(state) == ['device-setups', 'device-setups/' + 'b' * 32]
    entry(page).click()
    select(page)
    assert setup_posts(state) == []


def test_share_pins_exact_operation_keeps_draft_and_retries_same_message(setup_history):
    page, state = setup_history
    page.goto(state['url'] + '#gateway')
    page.get_by_label('對話訊息', exact=True).fill('尚未送出的草稿')
    entry(page).click()
    panel = select(page)
    state['fail_send'] = True
    panel.get_by_role('button', name='請管家解讀結果', exact=True).click()
    expect(page.get_by_text('服務暫時無法回應。', exact=True)).to_be_visible()
    state['fail_send'] = False
    panel.get_by_role('button', name='請管家解讀結果', exact=True).click()
    expect(page.get_by_text('合成的管家回覆。', exact=True)).to_be_visible()
    posts = [(body, key) for method, path, body, key in state['requests']
             if method == 'POST' and path == 'gateway/messages']
    assert len(posts) == 2 and posts[0] == posts[1]
    assert posts[0][0]['device_setup_id'] == 'b' * 32
    assert 'discover_devices' not in posts[0][0] and 'discover_environment' not in posts[0][0]
    expect(page.get_by_label('對話訊息', exact=True)).to_have_value('尚未送出的草稿')


def test_older_assistant_reply_keeps_saved_progress_and_composer_available(setup_history):
    page, state = setup_history
    state['response'] = {'state': 'awaiting_user', 'error_code': 'device_setup_status_unavailable',
                         'answer': '目前的智能管家版本尚未支援這項解讀；請先從「設備接入進度」查看結果。'}
    page.goto(state['url'] + '#gateway')
    page.get_by_label('對話訊息', exact=True).fill('保留這份草稿')
    entry(page).click()
    panel = select(page)
    panel.get_by_role('button', name='請管家解讀結果', exact=True).click()
    expect(page.get_by_text(state['response']['answer'], exact=True)).to_be_visible()
    expect(entry(page)).to_be_enabled()
    expect(page.get_by_label('對話訊息', exact=True)).to_have_value('保留這份草稿')
    expect(panel.get_by_role('button', name='重新讀取', exact=True)).to_be_enabled()
    expect(page.get_by_role('button', name='送出訊息', exact=True)).to_be_enabled()
    assert not setup_posts(state)
    page.get_by_role('button', name='送出訊息', exact=True).click()
    expect(page.get_by_label('對話訊息', exact=True)).to_have_value('')
    assert 'device_setup_id' not in sent_messages(state)[-1]
    assert setup_posts(state) == []


def test_saved_uncertain_operation_reconciles_once_after_lost_reply(setup_history):
    page, state = setup_history
    value = state['setups']['b' * 32]
    value.update(state='needs_reconcile', message='結果尚待確認。')
    state['lose_reconcile'] = True
    page.goto(state['url'] + '#gateway')
    entry(page).click()
    panel = select(page)
    expect(panel.get_by_role('button', name='確認接入', exact=True)).to_have_count(0)
    panel.get_by_role('button', name='確認接入結果', exact=True).click()
    expect(panel.get_by_text('已核對合成接入設定。', exact=True)).to_be_visible()
    assert [path for path, _, _ in setup_posts(state)] == ['device-setups/' + 'b' * 32 + '/reconcile']
    page.reload()
    entry(page).click()
    select(page)
    assert len(setup_posts(state)) == 1


def test_saved_running_operation_polls_without_approval(setup_history):
    page, state = setup_history
    value = state['setups']['b' * 32]
    value.update(state='preparing', can_reconcile=False, message='正在備份合成家庭設定。')
    page.goto(state['url'] + '#gateway')
    entry(page).click()
    panel = select(page)
    expect(panel.get_by_role('status')).to_contain_text('正在備份')
    value.update(state='configured', can_reconcile=True, revision=2, message='已核對合成接入設定。')
    expect(panel.get_by_text('已核對合成接入設定。', exact=True)).to_be_visible(timeout=10000)
    assert setup_posts(state) == []


@pytest.mark.parametrize('invalid', ['duplicate', 'oversized', 'identity', 'candidate'])
def test_history_rejects_malformed_or_rebound_records(setup_history, invalid):
    page, state = setup_history
    value = state['setups']['b' * 32]
    if invalid in {'duplicate', 'oversized'}:
        state['invalid_list'] = {'contract_version': 1, 'setups': [value] * (2 if invalid == 'duplicate' else 101)}
    elif invalid == 'identity':
        state['invalid_detail'] = {'id': 'd' * 32}
    else:
        state['invalid_detail'] = {'candidate': {**value['candidate'], 'id': 'another-device'}}
    page.goto(state['url'] + '#gateway')
    entry(page).click()
    if invalid not in {'duplicate', 'oversized'}:
        select(page)
    expect(history_panel(page).get_by_text('接入資料尚未確認，請重新讀取。', exact=True)).to_be_visible()
    expect(history_panel(page).get_by_role('button', name='請管家解讀結果', exact=True)).to_have_count(0)
    assert setup_posts(state) == []


@pytest.mark.parametrize('event', ['agentic-bridge-expired', 'agentic-bridge-unavailable'])
def test_late_history_cannot_resurrect_after_disconnect(setup_history, event):
    page, state = setup_history
    state['mode'] = 'bridge'
    state['hold_id'] = 'device-setups'
    page.goto(state['url'] + '#gateway')
    entry(page).click()
    expect(history_panel(page).get_by_role('status')).to_be_visible()
    page.evaluate('(event)=>window.dispatchEvent(new Event(event))', event)
    expect(entry(page)).not_to_be_visible()
    held = state['held'].pop()
    state['hold_id'] = None
    held[0].fulfill(content_type='application/json', body=json.dumps(held[1]))
    expect(history_panel(page)).to_have_count(0)
    page.get_by_role('button', name='重新連線', exact=True).click()
    expect(entry(page)).to_be_enabled()
    expect(history_panel(page)).to_have_count(0)
    assert setup_reads(state) == ['device-setups']
    entry(page).click()
    expect(history_panel(page).get_by_text('紀錄 bbbbbbbb', exact=True)).to_be_visible()
    assert setup_posts(state) == []


def test_changing_selection_discards_late_detail_and_history_never_approves(setup_history):
    page, state = setup_history
    state['hold_id'] = 'device-setups/' + 'b' * 32
    state['setups']['b' * 32].update(state='proposed', can_approve=True, can_reconcile=False)
    page.goto(state['url'] + '#gateway')
    entry(page).click()
    select(page)
    expect(history_panel(page).get_by_text('正在確認設備', exact=True)).to_be_visible()
    history_panel(page).get_by_role('button', name='返回接入清單', exact=True).click()
    panel = select(page, 'd' * 32)
    expect(panel.get_by_role('button', name='請管家解讀結果', exact=True)).to_be_enabled()
    held = state['held'].pop()
    held[0].fulfill(content_type='application/json', body=json.dumps(held[1]))
    panel.get_by_role('button', name='請管家解讀結果', exact=True).click()
    expect(page.get_by_text('合成的管家回覆。', exact=True)).to_be_visible()
    assert sent_messages(state)[-1]['device_setup_id'] == 'd' * 32
    assert setup_posts(state) == []


def test_history_sharing_is_unavailable_in_history_direct_and_pending_chat(setup_history):
    page, state = setup_history
    page.goto(state['url'] + '#gateway')
    expect(entry(page)).to_be_enabled()
    page.get_by_role('button', name=re.compile('^對話紀錄')).click()
    expect(entry(page)).to_have_count(0)
    page.get_by_role('button', name=re.compile('^對話紀錄')).click()
    page.get_by_text('進階', exact=True).click()
    page.get_by_label('對話方式', exact=True).select_option('direct')
    expect(entry(page)).to_have_count(0)
    page.get_by_label('對話方式', exact=True).select_option('hermes')
    expect(entry(page)).to_be_enabled()
    state['response'] = {'state': 'needs_reconcile'}
    page.get_by_label('對話訊息', exact=True).fill('合成的待確認對話')
    page.get_by_role('button', name='送出訊息', exact=True).click()
    expect(entry(page)).to_be_disabled()
    assert setup_reads(state) == [] and setup_posts(state) == []
