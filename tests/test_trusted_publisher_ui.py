"""Compiled official-update owner flow with protocol fixtures, no real trust enrollment."""
import json
import time
from pathlib import Path

import pytest
from playwright.sync_api import expect
from test_worker_ui import common, ui_page  # noqa: F401

TOKEN = 'fixture-publisher-approval-' + 'x' * 24


def summary(state='approval_required'):
    return {'contract': 1, 'state': state, 'message': '請確認安裝媒體附帶的官方更新來源。',
            'code': 'official_publisher_owner_approval_required',
            'publisher': 'ted-h-com-agentic-smart-home', 'repository': 'TED-H-com/agentic-smart-home',
            'channel': 'stable', 'image_repository': 'ghcr.io/ted-h-com/agentic-smart-home-controller',
            'key_sha256': 'abcdef0123456789' * 4, 'review_digest': 'b' * 64,
            'scope': 'controller_only_same_schema', 'authority': 'signed_controller_images_only'}


def mount(page, origin, publisher=None):
    state = {'publisher': publisher or summary(), 'trusted': False, 'fail': False, 'lose': False,
             'expires': time.time() + 300}
    posts, errors = [], []
    def route(route):
        request = route.request
        path = request.url.split('/api/')[1]
        body = request.post_data_json if request.method == 'POST' else None
        if path == 'updates/publisher':
            if state['fail']:
                route.fulfill(status=503, content_type='application/json', body='{}')
                return
            value = state['publisher']
        elif path == 'updates/publisher/offer':
            assert body == {} and request.headers['x-agentic-csrf'] == 'fixture-csrf'
            posts.append((path, body))
            value = {**state['publisher'], 'approval': TOKEN, 'expires_at': state['expires']}
        elif path == 'updates/publisher/approve':
            posts.append((path, body))
            assert body == {'review_digest': state['publisher']['review_digest'], 'approval': TOKEN}
            assert request.headers['x-agentic-csrf'] == 'fixture-csrf'
            state['publisher'] = {**state['publisher'], 'state': 'trusted', 'code': 'official_publisher_trusted'}
            state['trusted'] = True
            if state['lose']:
                route.abort()
                return
            value = state['publisher']
        elif path == 'updates':
            value = {'trusted_publisher_configured': state['trusted'], 'records': []}
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
    expect(page.get_by_role('heading', name='官方更新來源', exact=True)).to_be_visible()
    return state, posts, errors


@pytest.mark.parametrize('width', [390, 768, 1440])
def test_owner_reviews_exact_official_fingerprint_then_enables_signed_update_files(ui_page, width):  # noqa: F811
    page, origin = ui_page
    page.set_viewport_size({'width': width, 'height': 900 if width == 1440 else 844})
    state, posts, errors = mount(page, origin)
    expect(page.get_by_text('TED-H-com/agentic-smart-home', exact=True)).to_be_visible()
    expect(page.get_by_text('abcdef0123456789' * 4, exact=True)).to_be_visible()
    expect(page.get_by_text('穩定版', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='信任此官方更新來源')).to_have_count(0)
    page.get_by_role('button', name='設定官方更新來源', exact=True).click()
    review = page.get_by_role('region', name='確認官方更新來源')
    expect(review).to_contain_text('每個版本仍需你確認安裝')
    expect(review).to_contain_text('更新範圍為管理服務')
    assert posts == [('updates/publisher/offer', {})]
    expect(review.get_by_role('textbox')).to_have_count(0)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    output = Path('evidence/ui/trusted-publisher')
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f'owner-review-fixture-{width}.png'), full_page=True)
    (output / 'fixture.json').write_text(json.dumps({
        'kind': 'protocol_fixture', 'source': 'tests/test_trusted_publisher_ui.py',
        'viewports': [390, 768, 1440],
        'claim': 'Owner reviews an exact fictional official publisher pin before trusting it',
        'limits': ['No real publisher trust or release; fingerprint is a fixture', 'No device or household access'],
    }, indent=2) + '\n')
    review.get_by_role('button', name='信任此官方更新來源', exact=True).click()
    expect(page.get_by_text('已信任官方簽署的管理服務更新。', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='選擇更新說明檔', exact=True)).to_be_visible()
    assert posts[-1] == ('updates/publisher/approve', {'review_digest': 'b' * 64, 'approval': TOKEN})
    assert not [item for item in posts if item[0] in {'updates/apply', 'updates/stage'}]
    assert not errors


@pytest.mark.parametrize('code,message', [
    ('official_publisher_pin_not_shipped', '需取得含官方更新來源憑證的受信任主機版本。'),
    ('official_publisher_host_unsupported', '這台主機版本尚不支援設定官方更新來源，需取得含官方更新來源憑證的受信任主機版本。'),
])
def test_missing_official_pin_or_old_host_has_no_key_paste_or_trust_workaround(ui_page, code, message):  # noqa: F811
    page, origin = ui_page
    value = {**summary('unavailable'), 'code': code, 'message': message, 'key_sha256': None,
             'review_digest': None, 'image_repository': None}
    state, posts, errors = mount(page, origin, value)
    expect(page.get_by_text(message, exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='設定官方更新來源')).to_have_count(0)
    expect(page.get_by_role('textbox')).to_have_count(0)
    assert not posts and not errors


@pytest.mark.parametrize('fault', ['pin_changed', 'read_failed', 'expired'])
def test_stale_or_unreadable_official_review_never_offers_approval(ui_page, fault):  # noqa: F811
    page, origin = ui_page
    state, posts, errors = mount(page, origin)
    if fault == 'expired':
        state['expires'] = time.time() + 2
    page.get_by_role('button', name='設定官方更新來源', exact=True).click()
    expect(page.get_by_role('region', name='確認官方更新來源')).to_be_visible()
    if fault == 'pin_changed':
        state['publisher']['review_digest'] = 'c' * 64
        page.clock.run_for(4500)
    elif fault == 'read_failed':
        state['fail'] = True
        page.clock.run_for(4500)
        expect(page.get_by_role('alert')).to_contain_text('暫時無法核對官方更新來源')
    else:
        page.clock.run_for(3500)
        expect(page.get_by_text('確認已過期，請重新查看來源。', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='信任此官方更新來源')).to_have_count(0)
    assert posts == [('updates/publisher/offer', {})] and not errors


def test_lost_approval_reply_reads_actual_trust_without_reapplying(ui_page):  # noqa: F811
    page, origin = ui_page
    state, posts, errors = mount(page, origin)
    page.get_by_role('button', name='設定官方更新來源', exact=True).click()
    state['lose'] = True
    page.get_by_role('button', name='信任此官方更新來源', exact=True).click()
    expect(page.get_by_text('已信任官方簽署的管理服務更新。', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='選擇更新說明檔', exact=True)).to_be_visible()
    assert len([item for item in posts if item[0] == 'updates/publisher/approve']) == 1
    assert not errors
