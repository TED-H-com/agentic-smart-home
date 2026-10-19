"""Household availability wins over automatic restore-point protection."""
import json
from pathlib import Path

import pytest
from playwright.sync_api import expect
from test_worker_ui import common, ui_page  # noqa: F401


@pytest.mark.parametrize('width', [390, 768, 1440])
@pytest.mark.parametrize('state', ['critical', 'invalid'])
def test_snapshot_alert_keeps_ha_running_until_explicit_owner_pause(ui_page, state, width):  # noqa: F811
    page, origin = ui_page
    page.set_viewport_size({'width': width, 'height': 900 if width == 1440 else 844})
    posts, errors = [], []
    value = {**common('host/status'), 'root_cow': {
        'state': state,
        'message': '還原點空間不足，家庭繼續運作。' if state == 'critical' else '還原點已失效，家庭繼續運作。'}}

    def route(route):
        request = route.request
        path = request.url.split('/api/')[1]
        if path == 'host/status':
            result = value
        elif path == 'host/actions':
            posts.append(request.post_data_json)
            assert posts[-1] == {'action': 'shutdown', 'snapshot': None}
            assert request.headers.get('idempotency-key')
            value['vm'] = 'shut off'
            result = {'id': 'cow-pause-fixture', 'state': 'running'}
        else:
            result = common(path)
            assert result is not None, path
        route.fulfill(status=200, content_type='application/json', body=json.dumps(result))

    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/api/**', route)
    page.clock.install()
    page.goto(origin + '/#host')
    expect(page.get_by_role('alert')).to_contain_text('家庭繼續運作')
    page.clock.run_for(65000)
    assert posts == [] and value['vm'] == 'running'
    pause = page.get_by_role('button', name='暫停 HA 以保護還原點', exact=True)
    if state == 'critical':
        expect(pause).to_be_enabled()
        expect(page.get_by_text('暫停後家庭自動化也會停止；其他系統寫入仍可能用盡還原點空間。')).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        output = Path('evidence/ui/root-cow')
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / f'critical-fixture-{width}.png'), full_page=True)
        (output / 'fixture.json').write_text(json.dumps({
            'kind': 'protocol_fixture', 'source': 'tests/test_host_cow_ui.py',
            'viewports': [390, 768, 1440],
            'claim': 'Owner-controlled HA pause; critical capacity does not shut the household down automatically',
            'limits': ['No real LVM mutation or household/device access'],
        }, indent=2) + '\n')
        pause.click()
        expect(pause).to_have_count(0)
        assert len(posts) == 1 and value['vm'] == 'shut off'
    else:
        expect(pause).to_have_count(0)
        assert posts == []
    assert errors == []
