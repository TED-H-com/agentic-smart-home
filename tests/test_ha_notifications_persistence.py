"""Shutdown-state and swallowed-write behavior of the checked HA Store adapter."""
import asyncio
import importlib.util
import json
import sys
import types
from enum import Enum
from pathlib import Path

import pytest
from test_ha_notifications_queue import PACKAGE, QueueError


class CoreState(Enum):
    not_running = "NOT_RUNNING"
    starting = "STARTING"
    running = "RUNNING"
    stopping = "STOPPING"
    final_write = "FINAL_WRITE"
    stopped = "STOPPED"


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    class Store:
        saves = 0
        swallow = False

        def __init__(self, hass, version, key, *, private, atomic_writes):
            assert version == 1 and private and atomic_writes
            self.path, self.key = tmp_path / key, key

        async def async_save(self, data):
            self.saves += 1
            if not self.swallow:
                self.path.write_text(json.dumps({"version": 1, "minor_version": 1, "key": self.key, "data": data}))
                self.path.chmod(0o600)

    for name in ("homeassistant", "homeassistant.core", "homeassistant.helpers", "homeassistant.helpers.storage"):
        module = types.ModuleType(name)
        module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)
    sys.modules["homeassistant.core"].CoreState = CoreState
    sys.modules["homeassistant.helpers.storage"].Store = Store
    path = Path(__file__).parents[1] / "custom_components/agentic_home_notifications/persistence.py"
    spec = importlib.util.spec_from_file_location(PACKAGE + ".persistence_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    hass = types.SimpleNamespace(state=CoreState.running, async_add_executor_job=asyncio.to_thread)
    return module.CheckedStore(hass, "fixture"), hass


@pytest.mark.parametrize("state", [CoreState.not_running, CoreState.starting, CoreState.running])
async def test_real_setup_states_allow_only_verified_durable_write(adapter, state):
    persistence, hass = adapter
    hass.state = state
    await persistence.save({"revision": 1})
    assert await persistence.load() == {"revision": 1}
    assert persistence.store.saves == 1


@pytest.mark.parametrize("state", [CoreState.stopping, CoreState.final_write, CoreState.stopped])
async def test_all_shutdown_states_refuse_before_best_effort_store_save(adapter, state):
    persistence, hass = adapter
    hass.state = state
    with pytest.raises(QueueError, match="storage_unavailable"):
        await persistence.save({"revision": 1})
    assert persistence.store.saves == 0


async def test_swallowed_write_with_old_disk_revision_is_rejected(adapter):
    persistence, _ = adapter
    await persistence.save({"revision": 1})
    persistence.store.swallow = True
    with pytest.raises(QueueError, match="storage_unavailable"):
        await persistence.save({"revision": 2})
    assert await persistence.load() == {"revision": 1}
