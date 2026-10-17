"""Administrator exclusions must stop execution and credential writes before they start.

All subprocess hooks are sentinels. These tests never invoke a provider CLI.
"""
import hashlib
import json
import subprocess
from pathlib import Path

import httpx
import pytest
import yaml
from agentic_runner.app import Broker, create_api
from agentic_runner.providers import ProviderError, Providers


@pytest.fixture
def disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_DISABLED_PROVIDERS", "grok")
    return Providers(tmp_path)


@pytest.mark.parametrize("value", ["unknown", "grok,", "GROK", "grok\ncodex", "grok," * 40])
def test_invalid_policy_fails_before_creating_runtime_files(tmp_path, monkeypatch, value):
    monkeypatch.setenv("AGENTIC_DISABLED_PROVIDERS", value)
    with pytest.raises(ProviderError, match="AGENTIC_DISABLED_PROVIDERS"):
        Providers(tmp_path / "runtime")
    assert not (tmp_path / "runtime").exists()


async def test_disabled_status_does_not_probe_read_credentials_or_clear_stop_fence(disabled, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Excluded status reached an executable or credential reader")
    monkeypatch.setattr(disabled, "active_auth", forbidden)
    monkeypatch.setattr(disabled, "profile", forbidden)
    monkeypatch.setattr(disabled, "probe", forbidden)
    disabled.unconfirmed["fixture"] = {"reason": "unconfirmed"}
    observed = await disabled.health("grok")
    assert observed["status"] == "unavailable" and observed["reason"] == "provider_disabled"
    assert observed["version"] == "unknown" and observed["version_matches"] is False
    assert observed["hardware_verified"] is False and disabled.unconfirmed


@pytest.mark.parametrize("operation", ["spawn", "probe", "secret", "begin_secret", "device", "logout", "run", "write"])
async def test_disabled_operations_preserve_existing_credentials_without_starting_any_child(disabled, monkeypatch, operation):
    profile = disabled.profile("grok")
    secret = profile / "credential.json"
    secret.write_text(json.dumps({"method": "api_key", "value": "existing-fixture-value"}))
    marker = profile / "verified"
    marker.write_text("existing-fixture-marker")
    before = {p.name: p.read_bytes() for p in profile.iterdir()}
    def forbidden(*args, **kwargs):
        pytest.fail("Excluded operation reached process creation")
    monkeypatch.setattr("agentic_runner.providers.asyncio.create_subprocess_exec", forbidden)
    with pytest.raises(ProviderError, match="provider_disabled"):
        if operation == "spawn":
            await disabled.spawn("grok", ["--version"])
        elif operation == "probe":
            await disabled.probe("grok")
        elif operation == "secret":
            await disabled.secret_login("grok", "api_key", "replacement-fixture")
        elif operation == "begin_secret":
            await disabled.begin_secret_auth("fixture", "grok", "api_key", "replacement-fixture")
        elif operation == "device":
            await disabled.begin_device_auth("fixture", "grok")
        elif operation == "logout":
            await disabled.logout("grok")
        elif operation == "run":
            await disabled.run("fixture", "grok", "fixture", {"type": "object"})
        else:
            disabled._write_secret("grok", "api_key", "replacement-fixture")
    assert {p.name: p.read_bytes() for p in profile.iterdir()} == before
    assert not disabled.auth_sessions and not disabled.processes and not disabled.probes
    assert not (disabled.root / "work").exists()


@pytest.mark.parametrize("provider,setting", [("codex", "grok"), ("claude", "grok"), ("agy", "grok"), ("grok", "")])
async def test_enabled_providers_keep_their_spawn_path_without_running_cli(tmp_path, monkeypatch, provider, setting):
    monkeypatch.setenv("AGENTIC_DISABLED_PROVIDERS", setting)
    providers = Providers(tmp_path)
    calls = []
    monkeypatch.setattr("agentic_runner.providers.shutil.which", lambda name: "/fixture/" + name)
    async def sentinel(binary, *args, **kwargs):
        calls.append((binary, args))
        raise RuntimeError("sentinel reached; no executable started")
    monkeypatch.setattr("agentic_runner.providers.asyncio.create_subprocess_exec", sentinel)
    with pytest.raises(RuntimeError, match="sentinel reached"):
        await providers.spawn(provider, ["--version"])
    assert calls == [(provider, ("--version",))]


async def test_http_catalog_and_auth_enforce_policy_with_no_fake_connected_status(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_DISABLED_PROVIDERS", "codex,claude,agy,grok")
    broker = Broker(tmp_path)
    broker.settings({"token_hash": hashlib.sha256(b"fixture").hexdigest()})
    def forbidden(*args, **kwargs):
        pytest.fail("HTTP operation reached a CLI")
    monkeypatch.setattr("agentic_runner.providers.asyncio.create_subprocess_exec", forbidden)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_api(broker)), base_url="https://runner",
                                     headers={"Authorization": "Bearer fixture"}) as client:
            status = (await client.get("/status")).json()
            assert {p["provider"] for p in status["providers"]} == {"codex", "claude", "agy", "grok"}
            assert all(p["status"] == "unavailable" and p["reason"] == "provider_disabled"
                       for p in status["providers"])
            for method in ("api_key", "device_code"):
                response = await client.post("/providers/grok/auth", json={"method": method, "secret": "fixture"})
                assert response.status_code == 422 and "provider_disabled" in response.json()["detail"]
            assert (await client.post("/providers/grok/logout")).status_code == 422
        assert not broker.providers.auth_sessions
        assert list(broker.providers.profile("grok").iterdir()) == []
    finally:
        broker.db.close()


def test_policy_is_fixed_for_runtime_and_can_be_changed_only_for_next_start(disabled, monkeypatch):
    monkeypatch.setenv("AGENTIC_DISABLED_PROVIDERS", "")
    with pytest.raises(ProviderError, match="provider_disabled"):
        disabled.ensure_provider_enabled("grok")
    restarted = Providers(disabled.root)
    restarted.ensure_provider_enabled("grok")


def test_install_fixture_seeds_compose_policy_without_changing_product_payload(tmp_path):
    from test_linux_install import provider_policy_seed

    target = tmp_path / "target"
    target.mkdir()
    commands = [command.replace("/target/", str(target) + "/") for command in provider_policy_seed()]
    subprocess.run(["bash", "-e", "-c", "\n".join(commands)], check=True)
    path = target / "var/lib/agentic-home/host/runtime.env"
    assert path.read_text() == "AGENTIC_DISABLED_PROVIDERS=grok\n"
    assert path.stat().st_mode & 0o777 == 0o600
    assert not (target / "opt").exists()
    compose = yaml.safe_load((Path(__file__).parents[1] / "host/compose.yaml").read_text())
    assert compose["services"]["runner"]["environment"]["AGENTIC_DISABLED_PROVIDERS"] == "${AGENTIC_DISABLED_PROVIDERS:-}"
