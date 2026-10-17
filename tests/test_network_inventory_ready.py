"""The real Core no-write assertion must begin after native startup mutations."""
import asyncio
from unittest.mock import AsyncMock, call

import pytest
from test_network_inventory_real_ha import ready_inventory_registry


def entries():
    return [{"domain": domain, "source": "system", "state": "loaded", "entry_id": domain}
            for domain in ("analytics", "backup")]


async def test_ready_registry_waits_for_started_flows_and_preserves_all_entries(monkeypatch):
    baseline = [*entries(), {"domain": "hue", "source": "user", "state": "loaded", "entry_id": "other"}]
    ha = AsyncMock()
    ha.request.side_effect = [{"state": "STARTING"}, {"state": "RUNNING"}, {"state": "RUNNING"}]
    ha.ws.side_effect = [[], baseline]
    sleep = AsyncMock()
    monkeypatch.setattr("test_network_inventory_real_ha.asyncio.sleep", sleep)

    assert await ready_inventory_registry(ha) is baseline
    assert sleep.await_count == 2
    assert ha.request.await_args_list == [call("GET", "/config")] * 3
    assert ha.ws.await_args_list == [call("config_entries/get")] * 2


@pytest.mark.parametrize("state,registry", [
    ("STARTING", entries()),
    (None, entries()),
    ("RUNNING", []),
    ("RUNNING", entries()[:1]),
    ("RUNNING", [dict(entry, source="user") for entry in entries()]),
    ("RUNNING", [dict(entry, state="setup_in_progress") for entry in entries()]),
])
async def test_ready_registry_times_out_without_complete_native_readiness(state, registry):
    ha = AsyncMock()
    ha.request.return_value = {"state": state}
    ha.ws.return_value = registry
    with pytest.raises(AssertionError, match="^Isolated Core startup registry did not become ready$"):
        await ready_inventory_registry(ha, timeout=0.01)
    if state != "RUNNING":
        ha.ws.assert_not_awaited()


async def test_ready_registry_deadline_cancels_stalled_native_request():
    cancelled = asyncio.Event()

    async def stalled(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    ha = AsyncMock()
    ha.request.side_effect = stalled
    with pytest.raises(AssertionError, match="startup registry did not become ready"):
        await ready_inventory_registry(ha, timeout=0.01)
    assert cancelled.is_set()
    ha.ws.assert_not_awaited()


async def test_ready_registry_does_not_hide_native_contract_or_authorization_errors():
    ha = AsyncMock()
    ha.request.side_effect = RuntimeError("native authorization rejected")
    with pytest.raises(RuntimeError, match="native authorization rejected"):
        await ready_inventory_registry(ha)
    ha.ws.assert_not_awaited()
