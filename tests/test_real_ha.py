"""Real Core, auth, config API and helper events; Supervisor backup is a labeled fixture.

Run through pytest-disktmp. The container and disk-backed config are always removed.
No provider account or existing HA instance is accessed.
"""
import asyncio
import contextlib
import json
import os
import secrets
import subprocess
import uuid
from pathlib import Path

import aiohttp
import pytest
from agentic_controller.discovery import snapshot as discovery_snapshot
from agentic_controller.engine import Engine
from agentic_controller.ha import TESTED_CORE_VERSIONS, HAError, HomeAssistant
from agentic_controller.models import HELPERS, digest, utcnow
from agentic_controller.store import Store
from conftest import TARGET

IMAGE_REPOSITORY = "ghcr.io/home-assistant/home-assistant"


def docker(*args):
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(params=sorted(TESTED_CORE_VERSIONS))
def real_container(tmp_path, request):
    if not request.config.getoption("--real-ha"):
        pytest.skip("Use --real-ha to run the isolated real Core integration")
    assert str(tmp_path).startswith(str(Path.home() / "agent-work/tmp/pytest"))
    config = tmp_path / "ha-config"
    config.mkdir()
    (config / "configuration.yaml").write_text("""homeassistant:
  name: Agentic isolated integration
  latitude: 0
  longitude: 0
  elevation: 0
  unit_system: metric
  time_zone: UTC
http:
  server_port: 8123
frontend:
api:
websocket_api:
config:
onboarding:
automation: !include automations.yaml
input_boolean:
  agentic_motion_test:
    name: Agentic motion test
  agentic_lamp_test:
    name: Agentic lamp test
""")
    (config / "automations.yaml").write_text("[]\n")
    name = "agentic-home-test-" + uuid.uuid4().hex[:12]
    image = IMAGE_REPOSITORY + ":" + request.param
    docker("pull", image)
    try:
        docker("run", "-d", "--name", name, "--memory", "1536m", "--cpus", "2",
               "-p", "127.0.0.1::8123", "-v", f"{config}:/config", image)
        port = json.loads(docker("inspect", name))[0]["NetworkSettings"]["Ports"]["8123/tcp"][0]["HostPort"]
        yield name, f"http://127.0.0.1:{port}", config
    finally:
        subprocess.run(["docker", "exec", name, "chown", "-R", f"{os.getuid()}:{os.getgid()}", "/config"], capture_output=True)
        subprocess.run(["docker", "rm", "-fv", name], capture_output=True)


@pytest.fixture
def core_version(request):
    return request.node.callspec.params["real_container"]


@pytest.mark.parametrize("version", sorted(TESTED_CORE_VERSIONS))
async def test_tested_core_patch_passes_write_compatibility(version, monkeypatch):
    ha = HomeAssistant("isolated-fixture-token")
    async def config(method, path):
        assert (method, path) == ("GET", "/config")
        return {"version": version}
    monkeypatch.setattr(ha, "request", config)
    await ha.ensure_write_compatibility()


@pytest.mark.parametrize("version", [None, "", "2026.10.0", "2026.10.3", "2026.11.0", "2026.10.2.dev0"])
async def test_unknown_core_stops_before_native_rule_validation(version, monkeypatch):
    ha = HomeAssistant("isolated-fixture-token")
    calls = []
    async def config(method, path):
        calls.append((method, path))
        return {"version": version}
    async def unexpected(*args, **kwargs):
        pytest.fail("Unknown Core must not reach native rule validation")
    monkeypatch.setattr(ha, "request", config)
    monkeypatch.setattr(ha, "ws", unexpected)
    with pytest.raises(HAError) as rejected:
        await ha.validate({})
    assert rejected.value.status == 409
    assert calls == [("GET", "/config")]


async def wait_ready(base):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
        for _ in range(180):
            try:
                async with session.get(base + "/api/onboarding") as response:
                    if response.status in {200, 404}:
                        return
            except (aiohttp.ClientError, TimeoutError):
                pass
            await asyncio.sleep(1)
    raise AssertionError("Isolated HA did not become ready")


class RealCore(HomeAssistant):
    async def backup(self, name):
        # Core container has no Supervisor. This is explicitly NOT backup platform evidence.
        return {"id": "supervisor-contract-fixture", "name": name, "created_at": utcnow()}


async def test_real_core_proposal_deploy_trigger_restart(real_container, core_version, tmp_path, intent):
    name, base, config = real_container
    await wait_ready(base)
    async with aiohttp.ClientSession() as session:
        async with session.post(base + "/api/onboarding/users", json={"client_id": base + "/", "name": "Lab Owner",
                                "username": "lab-owner", "password": secrets.token_urlsafe(32), "language": "en"}) as response:
            user_result = await response.json()
            assert response.status == 200, user_result
        async with session.post(base + "/auth/token", data={"grant_type": "authorization_code",
                                "code": user_result["auth_code"], "client_id": base + "/"}) as response:
            auth = await response.json()
            assert response.status == 200, "Could not obtain isolated test authorization"
    ha = RealCore(auth["access_token"], base + "/api", base.replace("http:", "ws:") + "/api/websocket")
    store = Store(tmp_path / "controller.sqlite3")
    ha.store = store
    await ha.start()
    observer = asyncio.create_task(ha.watch())
    try:
        owner = await ha.ws("auth/current_user")
        owner_id = owner["id"]
        assert await ha.owner(owner_id)
        discovered = await discovery_snapshot(ha)
        native_sources = [source for source in discovered.sources if source.location == "home_assistant"]
        assert all(source.state == "ok" for source in native_sources), discovered.model_dump_json()
        assert all(source.upstream_version == core_version for source in native_sources)
        # This isolated config has no LAN discovery enabled. Empty flows test the
        # actual upstream read contract, not physical discovery or whole-home coverage.
        assert discovered.candidates == []
        # The onboarding HTTP endpoint can be ready before input_boolean has finished loading.
        for _ in range(60):
            ids = {item["entity_id"] for item in await ha.request("GET", "/states")}
            if HELPERS.issubset(ids):
                break
            await asyncio.sleep(1)
        else:
            raise AssertionError("The real Core helper integration did not finish loading")
        store.set_setting("policy", {"entities": [{"entity_id": id, "read": True, "write": "dedicated_helper",
                                                   "purpose": "isolated real HA helper"} for id in HELPERS]})
        engine = Engine(store, ha)
        task = engine.task("real Core helper integration", "explicit contract fixture")
        task = await engine.propose(task, intent)
        p = store.get("proposal", task["proposal_id"])
        assert await ha.config(p["resource_id"]) is None
        assert not (config / "automations.yaml").read_text().strip().startswith("- id:")
        engine.approve(p, owner_id, p["digest"], "real-approval")
        await engine.deploy(p, owner_id)
        task = store.get("task", task["id"])
        assert task["state"] == "verifying", json.dumps({"task": task, "expected": p["staged"],
                                               "readback": await ha.config(p["resource_id"])}, ensure_ascii=False, indent=2)
        deployed = await ha.config(p["resource_id"])
        assert digest(deployed) == p["final_hash"] and "initial_state" not in deployed
        for _ in range(30):
            if ha.connected:
                break
            await asyncio.sleep(0.1)
        p = store.get("proposal", p["id"])
        await engine.helper_test(p, owner_id, "real-helper-test")
        evidence = None
        for _ in range(100):
            evidence = ha.evidence(p)
            if evidence:
                break
            await asyncio.sleep(0.1)
        assert (await ha.request("GET", "/states/" + TARGET))["state"] == "on"
        assert evidence, "Source transition / automation context / target context were not correlated"
        before_restart = evidence
        await asyncio.to_thread(docker, "restart", name)
        # Docker may reassign an ephemeral published port across a container restart.
        port = json.loads(docker("inspect", name))[0]["NetworkSettings"]["Ports"]["8123/tcp"][0]["HostPort"]
        base = f"http://127.0.0.1:{port}"
        observer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await observer
        ha.base, ha.websocket = base + "/api", base.replace("http:", "ws:") + "/api/websocket"
        observer = asyncio.create_task(ha.watch())
        await wait_ready(base)
        for _ in range(60):
            try:
                entity = await ha.automation_entity(p["resource_id"], attempts=1)
                if entity["state"] == "on" and ha.connected:
                    break
            except Exception:
                pass
            await asyncio.sleep(1)
        else:
            raise AssertionError("Automation did not restore enabled state after real Core restart")
        # The automation can come back before input_boolean has reloaded. Wait on the
        # same inventory the engine checks, so the helper test does not race the reload.
        for _ in range(60):
            try:
                if HELPERS.issubset({item["entity_id"] for item in await ha.inventory()}):
                    break
            except Exception:
                pass
            await asyncio.sleep(1)
        else:
            raise AssertionError("The real Core helper integration did not reload after restart")
        # Reset the evidence cutoff so the pre-restart run cannot pass this assertion.
        p["deployed_at"] = utcnow()
        await engine.helper_test(p, owner_id, "real-helper-test-after-restart")
        after_restart = None
        for _ in range(100):
            after_restart = ha.evidence(p)
            if after_restart:
                break
            await asyncio.sleep(0.1)
        assert after_restart, "Rule did not execute after real Core restart"
        task2 = engine.task("same rule again", "fixture")
        duplicate = await engine.propose(task2, intent)
        assert duplicate["resource_id"] == p["resource_id"] and duplicate["state"] == "succeeded"
        image = IMAGE_REPOSITORY + ":" + core_version
        assert (await ha.request("GET", "/config"))["version"] == core_version
        report = {"run_at": utcnow(), "core": core_version,
                  "image": image, "image_digest": json.loads(docker("image", "inspect", image))[0]["RepoDigests"],
                  "real": ["owner WebSocket authorization", "states and registries", "native validate_config",
                           "automation config POST/GET", "staging and activation", "helper state transition",
                           "correlated automation/target events", "enabled state and real trigger after Core restart"],
                  "fixtures": ["Supervisor backup response", "Agent structured intent (no cloud login)"],
                  "before_restart": before_restart, "after_restart": after_restart,
                  "not_tested": ["HAOS Ingress", "Supervisor backup role", "laptop hardware", "cloud account login", "72h soak"]}
        evidence_dir = Path(__file__).parents[1] / "evidence"
        filename = "real-core-integration.json" if core_version == "2026.10.1" else f"real-core-integration-{core_version}.json"
        (evidence_dir / filename).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    finally:
        observer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await observer
        await ha.close()
        store.close()
