"""Check the installed probe against actual capability/dispatch code, without a VM."""
import importlib.util
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from agentic_controller.runner import RunnerError
from agentic_controller.work import WorkerStart
from agentic_controller.workers import Workers


@pytest.fixture
def installed_probe():
    spec = importlib.util.spec_from_file_location("installed_probe_contract", Path(__file__).with_name("installed_probe.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def actual_capabilities(tmp_path, monkeypatch, store):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "host"))
    from agentic_host.coding_service import CodingService

    service = object.__new__(CodingService)
    service.root = tmp_path
    service.credential_path = tmp_path / "absent-role-auth.json"
    # This unit test substitutes only the prior hard-boundary observation.
    # The installed VM performs the actual Docker/namespace/quota checks.
    service.capability_cache = (time.monotonic(), True, None)
    calls = []

    class Peer:
        def __init__(self, host):
            self.host = host

        async def call(self, method, path, body=None):
            calls.append((self.host, method, path))
            if not self.host:
                raise RunnerError("not paired")
            assert method == "GET" and path == "/coding/capabilities"
            return service.capabilities()

    workers = Workers(store, {"host": Peer(True), "haos": Peer(False)}, None)
    workers.closing = True
    yield workers, await workers.capabilities(), calls
    await workers.close()


async def test_installed_probe_accepts_boundary_ready_but_blocks_missing_role_login(actual_capabilities, installed_probe):
    workers, observed, calls = actual_capabilities
    installed_probe.verify_workers(observed)
    body = WorkerStart(worker_id="host-coder", provider="codex", mode="research", goal="No auth must block")
    owner = SimpleNamespace(cookies={"agentic_host": str(int(time.time()) + 3600) + ".fixture"})
    first = await workers.submit(body, "installed-coding-without-login", request=owner)
    replay = await workers.submit(body, "installed-coding-without-login", request=owner)
    installed_probe.verify_auth_blocked(first)
    installed_probe.verify_auth_blocked(replay)
    assert first["id"] == replay["id"]
    assert workers.public_capabilities()["active_job_id"] is None
    assert all(method == "GET" for _, method, _ in calls)
    assert all(type(value) is bool for value in installed_probe.DIAGNOSTICS.values())


@pytest.mark.parametrize("fault", ["boundary_blocked", "already_authenticated", "unexpected_provider", "home_available"])
async def test_installed_probe_keeps_strict_readiness_and_no_account_requirements(actual_capabilities, installed_probe, fault):
    _, observed, _ = actual_capabilities
    host, home = observed["workers"]
    if fault == "boundary_blocked":
        host["available"] = False
    elif fault == "already_authenticated":
        host["providers"][0]["credential_state"] = "configured"
    elif fault == "unexpected_provider":
        host["providers"][0]["id"] = "claude"
    else:
        home["available"] = True
    with pytest.raises(AssertionError):
        installed_probe.verify_workers(observed)
    assert installed_probe.STAGE in {"host_worker_boundary", "host_worker_requires_role_login",
                                    "host_worker_codex_contract", "home_worker_not_paired"}


def test_installed_probe_refuses_execution_without_credentials(installed_probe):
    receipt = {"worker_id": "host-coder", "provider": "codex", "state": "blocked", "reason": "coding_needs_auth",
               "execution_started": True, "stop_confirmed": True, "artifacts": [], "evidence_ids": []}
    with pytest.raises(AssertionError, match="coding_without_login_blocked"):
        installed_probe.verify_auth_blocked(receipt)
