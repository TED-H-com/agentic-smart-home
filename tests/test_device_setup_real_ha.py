"""Exact Core 2026.10.1 / 2026.10.2 IPP ABI matrix; synthetic printer and backup.

Run with pytest-disktmp and --real-ha. This test uses locally available pinned
Core images and never contacts an existing HA or physical device. The
test-only integration injects a ZeroconfServiceInfo through native flow init and
serves binary IPP on container loopback. HA's IPP flow, aioipp parser, coordinator,
config-entry API and registry are real and unpatched. Supervisor backup remains
an explicitly labeled fixture, not backup/HAOS/physical-device acceptance.

Reviewed upstream: https://github.com/home-assistant/core/tree/2026.10.1/
homeassistant/components/ipp and components/config/config_entries.py;
helpers/service_info/zeroconf.py. The runtime test, not those references alone,
establishes compatibility with the local image.

Set AGENTIC_IPP_EVIDENCE_DIR to an isolated directory below ~/agent-work/tmp to
preserve sanitized per-version results outside pytest's successful-temp cleanup.
Unlisted versions receive no qualification from this matrix.
"""
import asyncio
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from agentic_controller.device_setup import KIND, DeviceSetupApprove, DeviceSetupCreate, DeviceSetups
from agentic_controller.ha import HomeAssistant
from agentic_controller.models import utcnow
from agentic_controller.store import Store
from starlette.requests import Request

CORE_VERSIONS = ("2026.10.1", "2026.10.2")
IDENTITY = "11111111-2222-4333-8444-555555555555"
COUNTER = "/states/sensor.agentic_ipp_fixture_requests"


def docker(*args):
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(params=CORE_VERSIONS, ids=lambda version: "core-" + version)
def ipp_core_container(tmp_path, request):
    if not request.config.getoption("--real-ha"):
        pytest.skip("Use --real-ha for the isolated real Core IPP contract test")
    assert tmp_path.resolve().is_relative_to((Path.home() / "agent-work/tmp/pytest").resolve())
    # Deliberately no pull: test the pinned image explicitly provisioned locally.
    version = request.param
    image_ref = "ghcr.io/home-assistant/home-assistant:" + version
    image = json.loads(docker("image", "inspect", image_ref))[0]
    config = tmp_path / "ha-config"
    config.mkdir()
    shutil.copytree(Path(__file__).parent / "fixtures/agentic_ipp_fixture",
                    config / "custom_components/agentic_ipp_fixture",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (config / "configuration.yaml").write_text("""homeassistant:
  name: Isolated synthetic IPP contract test
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
agentic_ipp_fixture:
""")
    run_id = uuid.uuid4().hex
    name = "agentic-ipp-contract-" + run_id[:12]
    try:
        docker("run", "-d", "--name", name, "--memory", "1536m", "--cpus", "2",
               "-p", "127.0.0.1::8123", "-v", f"{config}:/config", image["Id"])
        container = json.loads(docker("inspect", name))[0]
        assert container["Image"] == image["Id"]
        port = container["NetworkSettings"]["Ports"]["8123/tcp"][0]["HostPort"]
        yield f"http://127.0.0.1:{port}", {"expected_core": version, "image": image_ref,
            "image_id": container["Image"], "repo_digests": image.get("RepoDigests", []),
            "run_id": run_id, "started_at": utcnow()}
    finally:
        subprocess.run(["docker", "exec", name, "chown", "-R", f"{os.getuid()}:{os.getgid()}", "/config"],
                       capture_output=True)
        subprocess.run(["docker", "rm", "-fv", name], capture_output=True)


def preserve_evidence(tmp_path, report):
    """Export only reviewed synthetic observations, never responses or auth."""
    encoded = json.dumps(report, indent=2) + "\n"
    (tmp_path / "real-ipp-contract-evidence.json").write_text(encoded)
    if configured := os.environ.get("AGENTIC_IPP_EVIDENCE_DIR"):
        destination = Path(configured).expanduser().resolve()
        assert destination.is_relative_to((Path.home() / "agent-work/tmp").resolve()), (
            "IPP evidence export must remain in isolated task disk storage")
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / f"core-{report['core']}-{report['run_id']}.json"
        # A new per-run receipt never overwrites earlier qualification evidence.
        with target.open("x", encoding="utf-8") as output:
            output.write(encoded)


def evidence_source_hashes():
    root = Path(__file__).parents[1]
    sources = ("tests/test_device_setup_real_ha.py", "tests/fixtures/agentic_ipp_fixture/__init__.py",
               "tests/fixtures/agentic_ipp_fixture/manifest.json", "controller/agentic_controller/device_setup.py")
    return {source: hashlib.sha256((root / source).read_bytes()).hexdigest() for source in sources}


async def owner_token(base):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
        for _ in range(180):
            try:
                async with session.get(base + "/api/onboarding") as response:
                    if response.status == 200:
                        break
            except (aiohttp.ClientError, TimeoutError):
                pass
            await asyncio.sleep(1)
        else:
            raise AssertionError("Isolated Core did not expose onboarding")
        async with session.post(base + "/api/onboarding/users", json={"client_id": base + "/",
            "name": "IPP Contract Owner", "username": "ipp-test-owner",
            "password": secrets.token_urlsafe(32), "language": "en"}) as response:
            assert response.status == 200, "Isolated Core owner onboarding failed"
            onboarded = await response.json()
        async with session.post(base + "/auth/token", data={"grant_type": "authorization_code",
            "code": onboarded["auth_code"], "client_id": base + "/"}) as response:
            assert response.status == 200, "Isolated Core authorization failed"
            return (await response.json())["access_token"]


class CoreWithFixtureBackup(HomeAssistant):
    """Record real responses without replacing transport or production guards."""

    def __init__(self, *args):
        super().__init__(*args)
        self.confirmations = []
        self.backups = []

    async def request(self, method, path, data=None, **kwargs):
        result = await super().request(method, path, data, **kwargs)
        if method == "POST" and path.startswith("/config/config_entries/flow/"):
            self.confirmations.append({"path": path, "data": data, "result": result})
        return result

    async def backup(self, name, *, send_guard):
        # Core has no Supervisor. Honor the real production fence while making
        # this one unavailable platform boundary an explicit contract fixture.
        await send_guard()
        self.backups.append(name)
        return {"id": "supervisor-ipp-fixture", "name": name, "created_at": utcnow()}


def owner_request(owner, key):
    # HTTP session transport is outside this direct-service integration. The
    # authority still validates the actual freshly authenticated Core owner.
    return Request({"type": "http", "method": "POST", "path": "/api/device-setups",
        "headers": [(b"idempotency-key", key.encode())], "state": {"principal": owner}})


async def test_real_core_ipp_confirmation_and_exact_registry_readback(ipp_core_container, tmp_path):
    base, provenance = ipp_core_container
    token = await owner_token(base)
    store = Store(tmp_path / "controller.sqlite3")
    ha = CoreWithFixtureBackup(token, base + "/api", base.replace("http:", "ws:") + "/api/websocket")
    ha.store = store
    tasks = []

    def spawn(_id, coroutine):
        task = asyncio.create_task(coroutine)
        tasks.append(task)
        return task

    await ha.start()
    try:
        core_version = (await ha.request("GET", "/config"))["version"]
        assert core_version == provenance["expected_core"]
        owner = (await ha.ws("auth/current_user"))["id"]
        assert await ha.owner(owner)
        store.set_setting("owner", owner)
        store.set_setting("csrf_secret", secrets.token_hex(32))
        session = SimpleNamespace(verify=lambda _request: True)
        app = SimpleNamespace(state=SimpleNamespace())
        service = DeviceSetups(app, store, ha, session, spawn=spawn)
        policy = store.setting("policy")
        for _ in range(60):
            services = await ha.ws("get_services")
            if "discover" in services.get("agentic_ipp_fixture", {}):
                break
            await asyncio.sleep(1)
        else:
            raise AssertionError("Synthetic IPP fixture failed to load")
        baseline = await ha.ws("config_entries/get")
        assert not any(entry["domain"] == "ipp" for entry in baseline)
        await ha.request("POST", "/services/agentic_ipp_fixture/discover", {})
        flows = [flow for flow in await ha.ws("config_entries/flow/progress") if flow["handler"] == "ipp"]
        assert len(flows) == 1, "Native IPP flow did not accept the synthetic binary IPP response"
        flow = flows[0]
        assert flow["step_id"] == "zeroconf_confirm"
        assert flow["context"]["source"] == "zeroconf"
        assert flow["context"]["unique_id"] == IDENTITY
        path = "/config/config_entries/flow/" + flow["flow_id"]
        form = await ha.request("GET", path)
        assert {key: form[key] for key in ("type", "flow_id", "handler", "step_id", "data_schema", "errors")} == {
            "type": "form", "flow_id": flow["flow_id"], "handler": "ipp", "step_id": "zeroconf_confirm",
            "data_schema": [], "errors": {}}
        assert int((await ha.request("GET", COUNTER))["state"]) >= 1

        proposal = await service.create(owner_request(owner, "real-ipp-propose"),
                                        DeviceSetupCreate(candidate_id=flow["flow_id"]))
        assert proposal.state == "proposed" and proposal.can_approve
        assert not ha.backups and not ha.confirmations
        assert await ha.ws("config_entries/get") == baseline
        approval = DeviceSetupApprove(expected_revision=proposal.revision, proposal_digest=proposal.proposal_digest)
        assert (await service.approve(owner_request(owner, "real-ipp-approve"), proposal.id, approval)).state == "preparing"
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=60)
        result = await service.detail(owner_request(owner, "read"), proposal.id)
        assert result.state == "configured", result.model_dump_json()
        assert len(ha.confirmations) == len(ha.backups) == 1
        confirmation = ha.confirmations[0]
        assert confirmation["path"] == path and confirmation["data"] == {}
        response = confirmation["result"]
        assert response["type"] == "create_entry" and response["flow_id"] == flow["flow_id"]
        assert response["handler"] == "ipp"
        entry_id = response["result"]["entry_id"]
        assert response["result"]["domain"] == "ipp" and response["result"]["source"] == "zeroconf"
        assert result.integration.id == entry_id

        # Core can acknowledge the config entry before the sensor platform has
        # loaded. Only repeat reads; never replay approval to obtain readiness.
        for _ in range(60):
            result = await service.detail(owner_request(owner, "read"), proposal.id)
            if result.device_count == 1 and (result.available_entity_count or 0) >= 1:
                break
            await asyncio.sleep(0.5)
        else:
            raise AssertionError("Native IPP coordinator/registry did not load: " + result.model_dump_json())
        entries = [entry for entry in await ha.ws("config_entries/get") if entry["domain"] == "ipp"]
        assert len(entries) == 1 and entries[0]["entry_id"] == entry_id and entries[0]["state"] == "loaded"
        devices = [device for device in await ha.ws("config/device_registry/list") if entry_id in device["config_entries"]]
        assert len(devices) == 1 and ["ipp", IDENTITY] in devices[0]["identifiers"]
        assert [device.id for device in result.devices] == [devices[0]["id"]]
        entities = [entity for entity in await ha.ws("config/entity_registry/list") if entity["config_entry_id"] == entry_id]
        states = {state["entity_id"]: state for state in await ha.request("GET", "/states")}
        available = [entity for entity in entities if not entity.get("disabled_by") and entity["entity_id"] in states
                     and states[entity["entity_id"]]["state"] not in {"unavailable", "unknown"}]
        assert result.entity_count == len(entities) >= 1
        assert result.available_entity_count == len(available) >= 1
        assert any(states[entity["entity_id"]]["state"] == "idle" for entity in available)
        assert all(entity["device_id"] == devices[0]["id"] for entity in entities)
        counter = await ha.request("GET", COUNTER)
        assert int(counter["state"]) >= 2 and counter["attributes"]["rejected_operations"] == 0
        assert not any(flow["flow_id"] == proposal.candidate.id for flow in await ha.ws("config_entries/flow/progress"))

        # Recreate our service from persisted state; repeating the same approval
        # must read the exact native entry, with no second backup or flow POST.
        service = DeviceSetups(app, store, ha, session, spawn=spawn)
        assert (await service.approve(owner_request(owner, "real-ipp-approve"), proposal.id, approval)).state == "configured"
        recovered = await service.detail(owner_request(owner, "read-after-restart"), proposal.id)
        assert recovered.state == "configured" and recovered.integration.id == entry_id
        assert len(ha.confirmations) == len(ha.backups) == 1
        assert recovered.physical_verification == "not_performed"
        assert recovered.backup_id == "supervisor-ipp-fixture"
        assert store.setting("policy") == policy
        assert (tmp_path / "snapshots" / (proposal.id + ".sqlite3")).is_file()
        persisted = json.dumps(store.get(KIND, proposal.id)) + recovered.model_dump_json()
        assert token not in persisted and IDENTITY not in persisted and base not in persisted
        # Preserve the actual run separately for every image, before successful
        # pytest temp cleanup; no previous qualification is reused here.
        preserve_evidence(tmp_path, {
            **provenance, "core": core_version, "flow": "ipp/zeroconf_confirm", "state": recovered.state,
            "assertions_completed_at": utcnow(), "result": "all_runtime_assertions_passed",
            "source_sha256": evidence_source_hashes(),
            "device_count": recovered.device_count, "entity_count": recovered.entity_count,
            "available_entity_count": recovered.available_entity_count,
            "confirmation_posts": len(ha.confirmations), "fixture_backups": len(ha.backups),
            "ipp_attribute_requests": int(counter["state"]),
            "rejected_printer_operations": counter["attributes"]["rejected_operations"],
            "physical_verification": recovered.physical_verification,
            "service_recreated_without_effect_replay": True,
            "real": ["Core owner check", "native flow progress/GET/POST result", "aioipp HTTP and binary decoding",
                     "native IPP coordinator", "exact device/entity/state readback", "persisted approval idempotency"],
            "fixtures": ["synthetic ZeroconfServiceInfo", "loopback IPP printer", "Supervisor backup response",
                         "outer HTTP session transport"],
            "not_tested": ["physical printer", "LAN multicast discovery", "Supervisor backup", "HAOS", "live household",
                           "full Core/process restart", "real Core lost-response recovery", "unlisted Core versions"]
        })
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await ha.close()
        store.close()
