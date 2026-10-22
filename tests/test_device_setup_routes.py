"""Real Controller/Ingress route seams with synthetic HA; no household effects."""
import asyncio
import time

import pytest
from test_bridge_controller import call, complete, system  # noqa: F401
from test_device_setup import IPPHA


@pytest.fixture
def printer(ha):
    printer = IPPHA()
    ha.base, ha.websocket = printer.base, printer.websocket
    ha.ws, ha.request, ha.backup = printer.ws, printer.request, printer.backup
    return printer


async def proposal(host):
    response = await host.post('/api/device-setups', json={'candidate_id': 'pending'},
                               headers={'Idempotency-Key': 'printer-proposal'})
    assert response.status_code == 200, response.text
    return response.json()


def approval(value):
    return {'expected_revision': value['revision'], 'proposal_digest': value['proposal_digest']}


async def test_outer_proposal_and_sidebar_approval_share_one_durable_operation(system, printer, store):  # noqa: F811 - shared pytest fixture
    app, _, client, host, _, runtime = system
    before = store.setting('policy')
    value = await proposal(host)
    assert printer.posts == printer.backups == []
    assert not client.cookies
    listing = await call(system, '/api/device-setups')
    assert listing.status_code == 200 and listing.json()['setups'][0]['id'] == value['id']
    path = f'/api/device-setups/{value["id"]}'
    response = await call(system, path + '/approve', 'POST', approval(value))
    assert response.status_code == 202, response.text
    await complete(app)
    readback = await call(system, path)
    assert readback.status_code == 200 and readback.json()['state'] == 'configured'
    assert readback.json()['integration']['id'] == 'newentry'
    assert readback.json()['physical_verification'] == 'not_performed'
    assert len(printer.posts) == len(printer.backups) == 1
    # Same approval receipt may be read again, never applied twice.
    assert (await call(system, path + '/approve', 'POST', approval(value))).status_code == 202
    await complete(app)
    assert len(printer.posts) == 1
    assert store.setting('policy') == before and not runtime.calls


async def test_device_setup_routes_require_host_session_and_csrf(system, printer):  # noqa: F811 - shared pytest fixture
    _, _, _, host, _, _ = system
    body = {'candidate_id': 'pending'}
    response = await host.post('/api/device-setups', json=body,
                              headers={'X-Agentic-CSRF': '', 'Idempotency-Key': 'missing-csrf'})
    assert response.status_code == 403
    value = await proposal(host)
    host.cookies.clear()
    assert (await host.get('/api/device-setups')).status_code == 401
    assert (await host.post(f'/api/device-setups/{value["id"]}/approve', json=approval(value))).status_code == 401
    assert printer.posts == printer.backups == []


@pytest.mark.parametrize('path,method', [
    ('/api/device-setups?all=true', 'GET'), ('/api/device-setups/extra', 'GET'),
    ('/api/device-setups/' + 'a' * 32 + '/approve', 'GET'),
    ('/api/device-setups/' + 'a' * 32, 'POST'),
    ('/api/device-setups/' + 'a' * 32 + '/configure', 'POST'),
])
async def test_sidebar_does_not_gain_generic_setup_or_ha_api(system, path, method):  # noqa: F811 - shared pytest fixture
    assert (await call(system, path, method, {} if method == 'POST' else None)).status_code == 403


async def test_sidebar_setup_renews_owner_lease_without_browser_polling(system, printer):  # noqa: F811 - shared pytest fixture
    app, bridge, _, host, options, _ = system
    value = await proposal(host)
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow_backup():
        entered.set()
        await release.wait()
    printer.backup_hook = slow_backup
    path = f'/api/device-setups/{value["id"]}'
    response = await call(system, path + '/approve', 'POST', approval(value))
    assert response.status_code == 202
    await asyncio.wait_for(entered.wait(), 5)
    assert app.state.device_setups.bridge_active(options['pairing_id'])
    # Only the background watcher can refresh this; no browser GET is issued.
    bridge.owner_valid_until = time.time() + 1
    watcher = asyncio.create_task(bridge.watch_owner())
    try:
        async with asyncio.timeout(10):
            while bridge.owner_valid_until < time.time() + 5:
                await asyncio.sleep(.05)
        release.set()
        await complete(app)
        assert len(printer.posts) == 1
        assert (await host.get(path)).json()['state'] == 'configured'
    finally:
        release.set()
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


async def test_sidebar_revocation_stops_approved_setup_before_confirmation(system, printer):  # noqa: F811 - shared pytest fixture
    app, bridge, _, host, _, _ = system
    value = await proposal(host)
    entered = asyncio.Event()
    async def held_backup():
        entered.set()
        await asyncio.Event().wait()
    printer.backup_hook = held_backup
    path = f'/api/device-setups/{value["id"]}'
    assert (await call(system, path + '/approve', 'POST', approval(value))).status_code == 202
    await asyncio.wait_for(entered.wait(), 5)
    await bridge.revoke()
    await complete(app)
    assert printer.posts == []
    result = (await host.get(path)).json()
    assert result['state'] == 'blocked'
