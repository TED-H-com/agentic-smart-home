"""Host bundle owner flow with fictional signed release receipts, no real update."""
import json
import time
from pathlib import Path

import pytest
from playwright.sync_api import expect
from test_host_updates_controller import CANDIDATE, TOKEN, record, status
from test_worker_ui import common, ui_page  # noqa: F401


def mount(page, origin):
    state = {'value': status(), 'offline': False, 'lose_apply': False}
    posts, errors = [], []
    def route(route):
        request = route.request
        path = request.url.split('/api/')[1]
        body = request.post_data_json if request.method == 'POST' else None
        if path == 'host-updates':
            if state['offline']:
                route.fulfill(status=503, content_type='application/json', body=json.dumps({'detail': '主機管理服務正在重新啟動，連線恢復後會自動核對結果。'}))
                return
            value = state['value']
        elif path.startswith('host-updates/'):
            assert request.headers['x-agentic-csrf'] == 'fixture-csrf'
            posts.append((path, body))
            if path.endswith('/check'):
                assert body == {}
                state['value']['check'].update(state='checking', message='正在檢查官方版本。')
                value = state['value']
            elif path.endswith('/apply'):
                assert body == {'candidate_digest': CANDIDATE, 'approval_id': TOKEN}
                state['value']['records'][0].update(state='queued', message='正在建立還原點。')
                if state['lose_apply']:
                    route.abort()
                    return
                value = state['value']['records'][0]
            elif path.endswith('/rollback-offer'):
                assert body == {'candidate_digest': CANDIDATE}
                state['value']['records'][0].update(rollback_approval_id='z' * 43, rollback_expires_at=time.time() + 300)
                value = state['value']['records'][0]
            elif path.endswith('/rollback'):
                assert body == {'candidate_digest': CANDIDATE, 'approval_id': 'z' * 43}
                state['value']['records'][0].update(state='rolling_back', message='正在恢復前一版本。')
                value = state['value']['records'][0]
            else:
                raise AssertionError(path)
        elif path == 'updates':
            value = {'trusted_publisher_configured': True, 'records': []}
        elif path == 'updates/publisher':
            value = {'contract': 1, 'state': 'trusted', 'code': 'official_publisher_trusted',
                     'message': '已信任官方更新來源。', 'publisher': 'ted-h-com-agentic-smart-home',
                     'repository': 'TED-H-com/agentic-smart-home', 'channel': 'stable',
                     'image_repository': 'ghcr.io/ted-h-com/agentic-smart-home-controller',
                     'key_sha256': 'f' * 64, 'review_digest': 'e' * 64,
                     'scope': 'controller_only_same_schema', 'authority': 'signed_controller_images_only'}
        else:
            value = common(path)
            if value is None:
                raise AssertionError(path)
        route.fulfill(status=200, content_type='application/json', body=json.dumps(value))
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/api/**', route)
    page.clock.install()
    page.goto(origin + '/#host')
    page.locator('summary').filter(has_text='版本').click()
    expect(page.get_by_role('button', name='檢查主機更新', exact=True)).to_be_visible()
    return state, posts, errors


def finish_check(page, state):
    state['value']['records'] = [record()]
    state['value']['check'].update(state='available', candidate_digest=CANDIDATE)
    page.clock.run_for(3500)
    expect(page.get_by_role('region', name='確認主機更新')).to_be_visible()


@pytest.mark.parametrize('width', [390, 768, 1440])
def test_one_owner_approval_per_exact_version_after_check_and_restore_point_notice(ui_page, width):  # noqa: F811
    page, origin = ui_page
    page.set_viewport_size({'width': width, 'height': 900 if width == 1440 else 844})
    state, posts, errors = mount(page, origin)
    page.get_by_role('button', name='檢查主機更新', exact=True).click()
    expect(page.get_by_text('正在檢查官方版本，準備完成後會請你確認。')).to_be_visible()
    assert posts == [('host-updates/check', {})]
    finish_check(page, state)
    review = page.get_by_role('region', name='確認主機更新')
    expect(review).to_contain_text('更新至 0.12.0')
    expect(review).to_contain_text('先建立還原點')
    expect(review).to_contain_text('家庭可能短暫暫停')
    expect(review).to_contain_text('管理服務會重新啟動')
    assert not [item for item in posts if item[0] == 'host-updates/apply']
    page.get_by_text('主機更新技術資料', exact=True).click()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    output = Path(__file__).parents[1] / 'evidence/ui/host-bundle-updates'
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f'review-fixture-{width}.png'), full_page=True)
    review.get_by_role('button', name='確認更新主機至 0.12.0', exact=True).click()
    expect(page.get_by_text('準備更新', exact=True)).to_be_visible()
    assert len([item for item in posts if item[0] == 'host-updates/apply']) == 1
    expect(page.get_by_role('button', name='檢查主機更新', exact=True)).to_be_disabled()
    assert not errors


def test_broker_restart_and_lost_apply_reply_resume_persistent_state_without_repeat(ui_page):  # noqa: F811
    page, origin = ui_page
    state, posts, errors = mount(page, origin)
    finish_check(page, state)
    state['lose_apply'] = True
    page.get_by_role('button', name='確認更新主機至 0.12.0', exact=True).click()
    state['offline'] = True
    page.clock.run_for(3500)
    expect(page.get_by_role('alert').filter(has_text='管理服務正在重新啟動')).to_be_visible()
    state['offline'] = False
    state['value']['records'][0].update(state='deployed', message='主機更新已完成並核對。')
    page.reload()
    page.locator('summary').filter(has_text='版本').click()
    expect(page.get_by_text('主機更新已完成並核對。', exact=True)).to_be_visible()
    assert len([item for item in posts if item[0] == 'host-updates/apply']) == 1 and not errors


def test_automatic_rollback_is_plain_and_manual_rollback_has_its_own_exact_approval(ui_page):  # noqa: F811
    page, origin = ui_page
    state, posts, errors = mount(page, origin)
    state['value']['records'] = [record('rolled_back')]
    state['value']['records'][0].update(auto_rollback=True, error_code='host_update_health_failed')
    page.clock.run_for(3500)
    expect(page.get_by_text('這次更新未能通過檢查，已自動恢復更新前的主機程式；目前家庭資料保持原狀。', exact=True)).to_be_visible()
    assert 'host_update_health_failed' not in page.locator('body').inner_text()
    state['value']['records'][0].update(state='deployed', auto_rollback=False, error_code=None)
    page.clock.run_for(3500)
    page.get_by_role('button', name='退回更新前版本', exact=True).click()
    expect(page.get_by_role('region', name='確認主機退版')).to_be_visible()
    assert not [item for item in posts if item[0] == 'host-updates/rollback']
    page.get_by_role('button', name='確認退回更新前版本', exact=True).click()
    assert posts[-1] == ('host-updates/rollback', {'candidate_digest': CANDIDATE, 'approval_id': 'z' * 43})
    assert not errors


@pytest.mark.parametrize('fault', ['source_absent', 'old_host', 'incompatible', 'expired'])
def test_unavailable_protocol_or_expired_review_never_enables_apply(ui_page, fault):  # noqa: F811
    page, origin = ui_page
    state, posts, errors = mount(page, origin)
    if fault in {'source_absent', 'old_host'}:
        state['value'].update(available=False, compatibility=None, message='主機需要先經你核准一次更新啟用；不需重裝。')
        page.clock.run_for(3500)
        expect(page.get_by_text('主機需要先經你核准一次更新啟用；不需重裝。')).to_be_visible()
        expect(page.get_by_role('button', name='檢查主機更新', exact=True)).to_be_disabled()
    else:
        finish_check(page, state)
        if fault == 'incompatible':
            state['value']['compatibility']['controller_schema'] = 4
        else:
            state['value']['records'][0]['approval_expires_at'] = time.time() - 1
        page.clock.run_for(3500)
        if fault == 'incompatible':
            expect(page.get_by_role('alert')).to_contain_text('主機更新介面不相容')
            expect(page.get_by_role('button', name='確認更新主機至 0.12.0', exact=True)).to_be_disabled()
        else:
            expect(page.get_by_text('確認已過期，請按「檢查主機更新」重新核對版本。')).to_be_visible()
            expect(page.get_by_role('button', name='確認更新主機至 0.12.0', exact=True)).to_have_count(0)
    assert not [item for item in posts if item[0] == 'host-updates/apply'] and not errors


def test_malformed_advanced_envelope_never_echoes_file_contents(ui_page):  # noqa: F811
    page, origin = ui_page
    state, posts, errors = mount(page, origin)
    page.get_by_text('進階：匯入已簽署更新檔', exact=True).click()
    page.get_by_label('主機更新說明檔', exact=True).set_input_files({
        'name': 'invalid.json', 'mimeType': 'application/json', 'buffer': b'private-fixture-should-not-display'})
    page.get_by_role('button', name='核對主機更新檔', exact=True).click()
    expect(page.get_by_role('alert')).to_contain_text('無法讀取主機更新說明檔')
    assert 'private-fixture-should-not-display' not in page.locator('body').inner_text()
    assert not posts and not errors
