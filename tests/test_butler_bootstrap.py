"""Fresh household setup and shipped-record recovery, using protocol fixtures only."""
import copy

import pytest
from agentic_controller.bootstrap import Bootstrap
from agentic_controller.models import BootstrapPreferences, BootstrapStart
from test_bootstrap import setup, start_legacy  # noqa: F401 - pytest fixture


async def test_new_setup_prepares_ha_without_cli_or_model_and_requires_only_hermes_login(setup, store, monkeypatch):
    bootstrap, backend, runner, hermes, gateway = setup
    original_capabilities = hermes.capabilities

    async def cloud_unavailable():
        raise AssertionError("HA preparation must not query the model service")

    monkeypatch.setattr(hermes, "capabilities", cloud_unavailable)
    first = await bootstrap.start(BootstrapStart(provider="codex", channel="web"), "start")
    assert [step["id"] for step in first["steps"] if step["state"] == "skipped"] == [
        "cli_auth", "cli_assist", "channel"]
    assert bootstrap.record()["workflow"] == "butler_v1"
    await bootstrap.advance()
    assert bootstrap.public()["current_step"] == "ha_provision"
    backend.finish("provision")
    await bootstrap.advance()
    backend.finish("start")
    await bootstrap.advance()
    assert bootstrap.public()["current_step"] == "hermes_install"
    assert [body["action"] for method, body in backend.calls if method == "POST"] == [
        "provision", "start", "bootstrap_hermes"]
    assert not runner.jobs and not gateway.messages

    monkeypatch.setattr(hermes, "capabilities", original_capabilities)
    backend.finish("bootstrap_hermes")
    await bootstrap.advance()
    assert bootstrap.public()["human_action"]["kind"] == "hermes_login"
    assert backend.value["vm"] == "running"
    hermes.runtime.update(state="ready", auth_verified_at="fixture-time")
    backend.value["bootstrap"]["ha"].update(reachable=True, onboarding_complete=True)
    store.set_setting("owner", "owner")
    await bootstrap.advance()
    assert bootstrap.public()["state"] == "completed"
    assert not runner.jobs and not gateway.messages
    posts = [body for method, body in backend.calls if method == "POST"]
    await bootstrap.start(BootstrapStart(provider="codex", channel="web"), "start")
    await bootstrap.advance()
    assert [body for method, body in backend.calls if method == "POST"] == posts


async def test_new_ha_job_survives_lost_reply_and_restart_without_replaying(setup, store, ha, monkeypatch):
    bootstrap, backend, runner, hermes, gateway = setup
    backend.lost = True
    await bootstrap.start(BootstrapStart(provider="codex", channel="web"), "start")
    await bootstrap.advance()
    record = bootstrap.record()
    saved_jobs = copy.deepcopy(record["jobs"])
    saved_steps = {item["id"]: copy.deepcopy(item) for item in record["steps"]
                   if item["state"] in {"completed", "skipped"}}
    restarted = Bootstrap(store, runner, hermes, gateway, ha, "fixture-controller", host_request=backend)
    monkeypatch.setattr(restarted, "resume", lambda: None)
    await restarted.advance()
    observed_job = restarted.record()["jobs"]["ha_provision:provision"]
    assert observed_job["request"] == saved_jobs["ha_provision:provision"]["request"]
    assert observed_job["state"] == "running"
    assert {item["id"]: item for item in restarted.record()["steps"]
            if item["id"] in saved_steps} == saved_steps
    assert len([method for method, _ in backend.calls if method == "POST"]) == 1
    backend.value["jobs"][0]["state"] = "needs_reconcile"
    await restarted.advance()
    assert restarted.public()["state"] == "blocked"
    await restarted.retry("recheck")
    await restarted.advance()
    assert restarted.public()["state"] == "blocked"
    assert restarted.record()["jobs"]["ha_provision:provision"]["request"] == saved_jobs["ha_provision:provision"]["request"]
    assert len([method for method, _ in backend.calls if method == "POST"]) == 1


@pytest.mark.parametrize("gate", ["paused", "restore_pending", "kvm"])
async def test_new_ha_preparation_preserves_pause_restore_and_hardware_gates(setup, store, gate):
    bootstrap, backend, _, _, _ = setup
    await bootstrap.start(BootstrapStart(provider="codex", channel="web"), "start")
    if gate == "paused":
        store.set_setting("paused", True)
    else:
        backend.value[gate] = "pending-backup" if gate == "restore_pending" else False
    await bootstrap.advance()
    assert bootstrap.public()["state"] == "blocked"
    assert all(method == "GET" for method, _ in backend.calls)


async def test_new_explicit_channel_can_be_deferred_after_vm_preparation(setup):
    bootstrap, backend, _, _, _ = setup
    await bootstrap.start(BootstrapStart(provider="codex", channel="line"), "start")
    await bootstrap.advance()
    saved = copy.deepcopy(bootstrap.record()["jobs"])
    await bootstrap.preferences(BootstrapPreferences(channel="web"), "use-web")
    assert bootstrap.public()["channel"] == "web"
    assert bootstrap.record()["jobs"] == saved
    await bootstrap.advance()
    assert len([method for method, _ in backend.calls if method == "POST"]) == 1


@pytest.mark.parametrize("reason", ["network", "timeout", "rate_limited", "cli_output_unrecognized"])
async def test_existing_cli_transient_status_requires_recheck_not_new_login(setup, reason):
    bootstrap, backend, runner, _, _ = setup
    await start_legacy(bootstrap, BootstrapStart(provider="codex", channel="web"), "legacy")
    runner.reason = reason
    await bootstrap.advance()
    assert bootstrap.public()["state"] == "blocked"
    assert bootstrap.public()["human_action"]["kind"] == "retry"
    assert bootstrap.public()["human_action"]["code"] == reason
    assert not runner.jobs and all(method == "GET" for method, _ in backend.calls)
    runner.reason, runner.connected = None, True
    await bootstrap.retry("recheck")
    await bootstrap.advance()
    assert bootstrap.public()["current_step"] == "hermes_install"
    assert len(runner.jobs) == 1


async def test_legacy_completed_steps_and_ha_job_remain_unchanged_on_resume(setup, store, ha, monkeypatch):
    bootstrap, backend, runner, hermes, gateway = setup
    await start_legacy(bootstrap, BootstrapStart(provider="codex", channel="web"), "legacy")
    record = bootstrap.record()
    for step in record["steps"]:
        step.update(state="skipped" if step["id"] == "channel" else "completed",
                    code="historical_checkpoint", verified_at="fixture-original-time")
    record["steps"][-1].update(state="waiting_input", verified_at=None)
    record.update(current_step="ha_native", state="waiting_input")
    record["jobs"] = {"ha_provision:provision": {
        "request": {"id": "original-ha-job", "action": "provision"}, "state": "succeeded"}}
    bootstrap.save(record)
    original = copy.deepcopy(record)
    backend.value["bootstrap"]["ha"].update(reachable=True, onboarding_complete=True)
    store.set_setting("owner", "owner")
    restarted = Bootstrap(store, runner, hermes, gateway, ha, "fixture-controller", host_request=backend)
    monkeypatch.setattr(restarted, "resume", lambda: None)
    await restarted.advance()
    completed = restarted.record()
    assert completed["state"] == "completed" and "workflow" not in completed
    assert completed["steps"][:-1] == original["steps"][:-1]
    assert completed["jobs"] == original["jobs"]
    assert not runner.calls and all(method == "GET" for method, _ in backend.calls)
    await restarted.advance()
    assert restarted.record() == completed
