"""Exact-version fencing fixtures; real patch qualification is a separate test."""
from types import SimpleNamespace

import pytest
from agentic_controller.device_setup import DeviceSetupCreate, DeviceSetups
from fastapi import HTTPException
from test_device_setup import IPPHA, request


@pytest.mark.parametrize("version", [
    "2026.10.0", "2026.10.3", "2026.11.0", "2026.10.2b0", "2026.10.2.dev0",
    "2026.10.2+custom", "2026.10.2 ", "2026.10", "", None, 2026102,
])
async def test_unqualified_core_version_rejects_proposal_before_any_effect(store, version):
    store.set_setting("csrf_secret", "11" * 32)
    store.set_setting("owner", "owner")
    ha = IPPHA()
    ha.config = {"version": version}
    service = DeviceSetups(SimpleNamespace(state=SimpleNamespace()), store, ha,
                          SimpleNamespace(verify=lambda _request: True))
    with pytest.raises(HTTPException) as exc:
        await service.create(request(), DeviceSetupCreate(candidate_id="pending"))
    assert exc.value.status_code == 409
    assert exc.value.detail == "device_setup_core_unsupported"
    assert not ha.backups and not ha.posts and not service.running
    assert (await service.listing(request())).setups == []
