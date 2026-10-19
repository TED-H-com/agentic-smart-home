"""Outer host authorization and explicit LAN choice, without changing a host."""
import httpx
import pytest
from agentic_controller.app import create_app
from agentic_controller.host import HostAction
from pydantic import ValidationError


@pytest.mark.parametrize('body', [
    {'action': 'configure_lan', 'interface_id': 'enp2s0'},
    {'action': 'configure_lan', 'interface_id': 'a' * 64, 'snapshot': '20261017T000000Z-1234abcd'},
    {'action': 'start', 'interface_id': None},
    {'action': 'shutdown', 'interface_id': 'a' * 64},
    {'action': 'configure_lan', 'interface_id': None, 'command': 'anything'},
])
def test_interface_selection_cannot_be_invalid_or_injected_into_other_actions(body):
    with pytest.raises(ValidationError):
        HostAction.model_validate(body)


@pytest.mark.parametrize('selection', ['auto', None, 'a' * 64])
async def test_lan_action_requires_local_login_csrf_and_preserves_exact_choice(store, ha, tmp_path, monkeypatch, selection):
    monkeypatch.setenv('AGENTIC_DATA', str(tmp_path))
    monkeypatch.setenv('AGENTIC_LINE_CONFIG', str(tmp_path / 'line/gateway.json'))
    (tmp_path / 'access-key').write_text('synthetic-laptop-access-key')
    calls = []

    async def host_call(method='GET', body=None, *, store):
        calls.append((method, body))
        return {'id': body['id'], 'state': 'running'}

    monkeypatch.setattr('agentic_controller.host.host_call', host_call)
    app = create_app(store=store, ha=ha, host_mode=True)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost:8099',
                                     headers={'Origin': 'http://localhost:8099', 'Idempotency-Key': 'fixture-lan-job'}) as client:
            body = {'action': 'configure_lan'} if selection == 'auto' else {'action': 'configure_lan', 'interface_id': selection}
            assert (await client.post('/api/host/actions', json=body)).status_code == 401
            assert (await client.post('/api/host/login', json={'key': 'synthetic-laptop-access-key'})).status_code == 200
            assert (await client.post('/api/host/actions', json=body)).status_code == 403
            client.headers['X-Agentic-CSRF'] = (await client.get('/api/session')).json()['csrf']
            response = await client.post('/api/host/actions', json=body)
            assert response.status_code == 202 and response.json()['state'] == 'running'
            assert calls == [('POST', {'id': 'fixture-lan-job', **body})]
            await client.post('/api/host/actions', json={'action': 'start'})
            assert calls[-1] == ('POST', {'id': 'fixture-lan-job', 'action': 'start', 'snapshot': None})
    finally:
        await app.state.gateway.close()


def test_omitted_interface_means_automatic_selection_and_explicit_null_still_disables():
    automatic = HostAction.model_validate({'action': 'configure_lan'})
    assert 'interface_id' not in automatic.model_fields_set
    disabled = HostAction.model_validate({'action': 'configure_lan', 'interface_id': None})
    assert 'interface_id' in disabled.model_fields_set and disabled.interface_id is None
