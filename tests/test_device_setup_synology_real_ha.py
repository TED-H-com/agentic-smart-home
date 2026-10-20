"""Pinned real Core + hand-authored loopback DSM; never physical NAS acceptance.

Run serially with pytest-disktmp --real-ha. No images are pulled. Native flow,
py-synologydsm-api, platforms, coordinator, backup agent and registries are real.
Only SSDP observation, DSM responses and absent Supervisor backup are fixtures.
"""
import asyncio
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import uuid
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from agentic_controller import device_setup as setup_api
from agentic_controller.device_setup import DeviceSetupApprove, DeviceSetupCreate, DeviceSetups
from agentic_controller.ha import HomeAssistant
from agentic_controller.models import utcnow
from agentic_controller.store import Store
from test_device_setup_real_ha import docker, owner_request, owner_token

CORE_VERSIONS = ("2026.10.1", "2026.10.2")
DOMAIN = "agentic_synology_fixture"
JOURNAL = "agentic_synology_fixture_requests.jsonl"
MAC = "00:11:32:AA:BB:CC"
SERIAL = "SYNTHETIC-DSM-0001"
LOCATION = "Synthetic Synology qualification"
BACKUP_PATH = "ha_backup_synthetic_synology_qualification"
NOT_TESTED = ["physical NAS", "real DSM firmware", "LAN SSDP multicast", "Supervisor backup", "HAOS",
              "live household", "DSM-side trusted-device state", "backup upload"]
LINK_SCHEMA = [
    {"type": "string", "name": "username", "required": True, "default": ""},
    {"type": "string", "name": "password", "required": True, "default": ""},
    {"type": "string", "name": "port", "required": False, "optional": True, "default": ""},
    {"type": "boolean", "name": "ssl", "required": False, "optional": True, "default": True},
    {"type": "boolean", "name": "verify_ssl", "required": False, "optional": True, "default": False}]
OTP_SCHEMA = [{"type": "string", "name": "otp_code", "required": True}]


@pytest.fixture
def synology_scenario(request):
    two_step = getattr(request, "param", True)
    return {"synthetic": True, "username": "synthetic-account-" + secrets.token_hex(6),
        "password": "synthetic-password-" + secrets.token_hex(12), "otp_required": two_step,
        "otp_code": "731894", "writable_shares": ([{"name": "家庭備份", "path": "/home-backups"},
            {"name": "備用資料夾", "path": "/spare"}] if two_step else []),
        "serial": SERIAL, "mac": MAC, "model": "DS920+", "hostname": "synthetic-dsm",
        "friendly_name": "Synthetic NAS"}


@pytest.fixture(params=CORE_VERSIONS, ids=lambda version: "core-" + version)
def synology_core_container(tmp_path, request, synology_scenario):
    if not request.config.getoption("--real-ha"):
        pytest.skip("Use --real-ha for isolated native Synology qualification")
    assert tmp_path.resolve().is_relative_to((Path.home() / "agent-work/tmp/pytest").resolve())
    image_ref = "ghcr.io/home-assistant/home-assistant:" + request.param
    image = json.loads(docker("image", "inspect", image_ref))[0]
    config = tmp_path / "ha-config"
    config.mkdir()
    shutil.copytree(Path(__file__).parent / "fixtures/agentic_synology_fixture",
        config / "custom_components/agentic_synology_fixture", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (config / "agentic_synology_scenario.json").write_text(json.dumps(synology_scenario))
    (config / "configuration.yaml").write_text(f"""homeassistant:
  name: {LOCATION}
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
agentic_synology_fixture:
""")
    run_id = uuid.uuid4().hex
    name = "agentic-synology-contract-" + run_id[:12]
    # Keep the exact authority endpoint across docker restart.
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    try:
        docker("run", "-d", "--pull", "never", "--name", name, "--memory", "1536m", "--cpus", "2",
            "-p", f"127.0.0.1:{port}:8123", "-v", f"{config}:/config", image["Id"])
        container = json.loads(docker("inspect", name))[0]
        assert container["Image"] == image["Id"]
        yield name, f"http://127.0.0.1:{port}", config, {
            "expected_core": request.param, "image": image_ref, "image_id": image["Id"],
            "repo_digests": image.get("RepoDigests", []), "run_id": run_id, "started_at": utcnow()}
    finally:
        subprocess.run(["docker", "exec", name, "chown", "-R", f"{os.getuid()}:{os.getgid()}", "/config"],
                       capture_output=True)
        subprocess.run(["docker", "rm", "-fv", name], capture_output=True)


async def wait_ready(ha):
    for _ in range(120):
        try:
            if "discover" in (await ha.ws("get_services")).get(DOMAIN, {}):
                return
        except Exception:
            pass
        await asyncio.sleep(0.5)
    raise AssertionError("Synthetic DSM endpoint failed to load")


def form_shape(flow_id, step, schema, errors=None, placeholders=None):
    return {"type": "form", "flow_id": flow_id, "handler": "synology_dsm", "step_id": step,
        "data_schema": schema, "errors": errors or {}, "description_placeholders": placeholders,
        "last_step": None, "preview": None}


def backup_schema(scenario):
    return [{"name": "backup_share", "required": True, "selector": {"select": {
        "options": [{"value": share["path"], "label": share["name"]} for share in scenario["writable_shares"]],
        "mode": "dropdown", "multiple": False, "custom_value": False, "sort": False}}},
        {"type": "string", "lengthMin": 1, "name": "backup_path", "required": True, "default": BACKUP_PATH}]


def assert_no_secrets(encoded, scenario):
    for key in ("username", "password", "otp_code"):
        assert scenario[key].encode() not in encoded, "Synthetic secret escaped its intended boundary"


def capture_form(provenance, scenario, label, response):
    """Persist an explicit shape projection immediately, never the raw response."""
    projection = {key: response[key] for key in ("type", "handler", "step_id", "data_schema", "errors",
        "description_placeholders", "last_step", "preview") if key in response}
    if "flow_id" in response:
        projection["flow_id"] = "<flow_id>"
    if "result" in response:
        projection["result_keys"] = sorted(response["result"])
        projection["result"] = {key: response["result"][key] for key in ("domain", "source", "state")
                                if key in response["result"]}
    # Even a failed link response includes the password in the schema default.
    if "data_schema" in projection:
        projection["data_schema"] = [{**field, **({"default": "<redacted>"} if
            field.get("name") in {"username", "password", "otp_code"} and "default" in field else {})}
            for field in projection["data_schema"]]
    encoded = json.dumps(projection, ensure_ascii=False)
    assert_no_secrets(encoded.encode(), scenario)
    target = Path(os.environ.get("AGENTIC_SYNOLOGY_FORMS_PATH", str(Path.home() /
        "agent-work/tmp/synology-setup-20261019/captured-forms.json"))).expanduser().resolve()
    assert target.is_relative_to((Path.home() / "agent-work/tmp").resolve())
    target.parent.mkdir(parents=True, exist_ok=True)
    saved = json.loads(target.read_text()) if target.exists() else {}
    variant = "2sa-shares" if scenario["otp_required"] else "no-2sa-no-shares"
    saved.setdefault(provenance["expected_core"], {}).setdefault(variant, {})[label] = projection
    target.write_text(json.dumps(saved, indent=2, ensure_ascii=False) + "\n")


async def discover(ha, provenance, scenario):
    await wait_ready(ha)
    await ha.request("POST", f"/services/{DOMAIN}/discover", {})
    flows = [flow for flow in await ha.ws("config_entries/flow/progress") if flow["handler"] == "synology_dsm"]
    assert len(flows) == 1
    flow = flows[0]
    assert flow["step_id"] == "link" and flow["context"]["source"] == "ssdp"
    assert flow["context"]["unique_id"] == MAC.replace(":", "")
    response = await ha.request("GET", "/config/config_entries/flow/" + flow["flow_id"])
    capture_form(provenance, scenario, "link-get", response)
    assert response == form_shape(flow["flow_id"], "link", LINK_SCHEMA,
        placeholders={"name": scenario["friendly_name"], "host": "127.0.0.1"})
    return flow


async def readback(ha, entry_id):
    for _ in range(120):
        entries = [row for row in await ha.ws("config_entries/get") if row["domain"] == "synology_dsm"]
        assert len(entries) == 1 and entries[0]["entry_id"] == entry_id
        devices = [row for row in await ha.ws("config/device_registry/list") if entry_id in row["config_entries"]]
        central = [row for row in devices if ["synology_dsm", SERIAL] in row["identifiers"]]
        entities = [row for row in await ha.ws("config/entity_registry/list") if row["config_entry_id"] == entry_id]
        states = {row["entity_id"]: row for row in await ha.request("GET", "/states")}
        available = [row for row in entities if row["entity_id"] in states
                     and states[row["entity_id"]]["state"] not in {"unavailable", "unknown"}]
        if entries[0]["state"] == "loaded" and len(central) == 1 and available:
            assert central[0]["identifiers"] == [["synology_dsm", SERIAL]]
            assert central[0]["connections"] == [["mac", MAC.lower()]]
            assert all(row["device_id"] in {device["id"] for device in devices} for row in entities)
            assert any(row["entity_id"].startswith("sensor.") for row in available)
            return central[0], entities, available, len(devices)
        await asyncio.sleep(0.5)
    raise AssertionError("Native DSM entry/platforms did not load")


async def inspect_entry(ha, expected_agents):
    await ha.request("POST", f"/services/{DOMAIN}/inspect_entry", {})
    attrs = (await ha.request("GET", "/states/sensor.agentic_synology_fixture_readback"))["attributes"]
    assert attrs["coordinator_success"] is True
    assert attrs["backup_agent_count"] == expected_agents and attrs["backup_count"] == 0


def journal(config, scenario):
    encoded = (config / JOURNAL).read_bytes()
    assert_no_secrets(encoded, scenario)
    rows = [json.loads(line) for line in encoded.splitlines()]
    assert all(set(row) == {"api", "method", "outcome"} for row in rows)
    assert not any(row["outcome"] == "rejected" for row in rows)
    return rows


def summarize(rows):
    return dict(sorted(Counter(" ".join((row["api"], row["method"], row["outcome"])) for row in rows).items()))


def preserve_evidence(tmp_path, report, scenario):
    root = Path(__file__).parents[1]
    sources = ["tests/test_device_setup_synology_real_ha.py", *sorted(str(path.relative_to(root)) for path in
        (root / "tests/fixtures/agentic_synology_fixture").glob("*") if path.is_file())]
    report["source_sha256"] = {path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in sources}
    report["not_tested"] = NOT_TESTED + (["product DeviceSetups service"] if report["mode"] == "native" else [])
    encoded = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    assert_no_secrets(encoded.encode(), scenario)
    (tmp_path / "synology-qualification.json").write_text(encoded)
    if configured := os.environ.get("AGENTIC_SYNOLOGY_EVIDENCE_DIR"):
        destination = Path(configured).expanduser().resolve()
        assert destination.is_relative_to((Path.home() / "agent-work/tmp").resolve())
        destination.mkdir(parents=True, exist_ok=True)
        with (destination / f"{report['mode']}-{report['core']}-{report['run_id']}.json").open("x") as output:
            output.write(encoded)


@pytest.mark.parametrize("synology_scenario", [True, False], ids=("2sa-shares", "no-2sa-no-shares"), indirect=True)
async def test_real_native_synology_flow_readback_restart(synology_core_container, synology_scenario, tmp_path):
    name, base, config, provenance = synology_core_container
    scenario = synology_scenario
    store = Store(tmp_path / "controller.sqlite3")
    ha = HomeAssistant(await owner_token(base), base + "/api", base.replace("http:", "ws:") + "/api/websocket")
    ha.store = store
    await ha.start()
    try:
        core = (await ha.request("GET", "/config"))["version"]
        assert core == provenance["expected_core"]
        flow = await discover(ha, provenance, scenario)
        path = "/config/config_entries/flow/" + flow["flow_id"]
        steps = []

        async def post(label, data):
            result = await ha.request("POST", path, data)
            capture_form(provenance, scenario, label, result)
            steps.append(label)
            return result

        wrong_password = "synthetic-wrong-password"
        result = await post("wrong-password", {"username": scenario["username"], "password": wrong_password})
        expected = [{**field, "default": scenario["username"] if field["name"] == "username" else wrong_password}
                    if field["name"] in {"username", "password"} else field for field in LINK_SCHEMA]
        assert result == form_shape(flow["flow_id"], "link", expected, {"username": "invalid_auth"},
            {"name": scenario["friendly_name"], "host": "127.0.0.1"})
        result = await post("credentials", {"username": scenario["username"], "password": scenario["password"]})
        if scenario["otp_required"]:
            assert result == form_shape(flow["flow_id"], "2sa", OTP_SCHEMA)
            result = await post("wrong-otp", {"otp_code": "000000"})
            assert result == form_shape(flow["flow_id"], "2sa", OTP_SCHEMA, {"otp_code": "otp_failed"})
            result = await post("correct-otp", {"otp_code": scenario["otp_code"]})
            assert result == {**form_shape(flow["flow_id"], "backup_share", backup_schema(scenario)), "errors": None}
            result = await post("backup-share", {"backup_share": "/home-backups", "backup_path": BACKUP_PATH})
        assert result["type"] == "create_entry" and result["handler"] == "synology_dsm"
        assert result["flow_id"] == flow["flow_id"]
        assert result["result"]["source"] == "ssdp" and result["result"]["domain"] == "synology_dsm"
        entry_id = result["result"]["entry_id"]
        creation_boundary = len(journal(config, scenario))
        device, entities, available, device_count = await readback(ha, entry_id)
        loaded_rows = journal(config, scenario)
        await inspect_entry(ha, int(scenario["otp_required"]))
        after_setup = journal(config, scenario)
        polling_boundary = len(after_setup)
        await inspect_entry(ha, int(scenario["otp_required"]))
        after_polling = journal(config, scenario)
        assert len(after_polling) > polling_boundary
        await ha.close()
        await asyncio.to_thread(docker, "restart", "--time", "30", name)
        await ha.start()
        await wait_ready(ha)
        restarted_device, restarted_entities, _, _ = await readback(ha, entry_id)
        assert restarted_device["id"] == device["id"]
        assert {row["entity_id"] for row in restarted_entities} == {row["entity_id"] for row in entities}
        await inspect_entry(ha, int(scenario["otp_required"]))
        rows = journal(config, scenario)
        operations = summarize(rows)
        assert operations["fixture boot ok"] == 2
        assert operations["SYNO.API.Auth login invalid_credentials"] == 1
        if scenario["otp_required"]:
            assert operations["SYNO.API.Auth login otp_required"] == 1
            assert operations["SYNO.API.Auth login otp_failed"] == 1
            assert operations["SYNO.API.Auth login ok_otp_device_token_issued"] == 1
            assert operations["SYNO.API.Auth login ok_device_token_otp_ignored"] == 1
            assert operations["SYNO.API.Auth login ok_device_token"] >= 2
        # Entry setup may begin before the HTTP create_entry receipt arrives.
        # Its own fresh API discovery precedes the new client login in the journal.
        successful_logins = [i for i, row in enumerate(loaded_rows) if row["api"] == "SYNO.API.Auth"
            and row["method"] == "login" and row["outcome"] in {"ok", "ok_device_token"}]
        setup_login = successful_logins[0 if scenario["otp_required"] else 1]
        setup_start = max(i for i in range(setup_login) if loaded_rows[i]["api"] == "SYNO.API.Info")
        preserve_evidence(tmp_path, {**provenance, "core": core, "mode": "native",
            "variant": "2sa-shares" if scenario["otp_required"] else "no-2sa-no-shares",
            "result": "all_runtime_assertions_passed", "finished_at": utcnow(), "operations": operations,
            "after_create_entry_operations": summarize(rows[creation_boundary:]),
            "config_flow_operations": summarize(loaded_rows[:setup_start]),
            "entry_setup_operations": summarize(loaded_rows[setup_start:]),
            "after_setup_polling_operations": summarize(after_polling[polling_boundary:]),
            "polling_method": "two explicit native central coordinator refreshes; backup agent list reads",
            "rejected_operations": 0, "flow_posts": len(steps), "full_core_restart": True,
            "device_connections": device["connections"], "device_count": device_count,
            "entity_count": len(entities), "available_entity_count": len(available),
            "backup_agent_count": int(scenario["otp_required"]),
            "real": ["pinned Core", "native SSDP flow", "unmodified py-synologydsm-api 2.10.3",
                "native coordinator/platforms", "native backup agent list", "registry/state readback", "Core restart"],
            "fixtures": ["synthetic SSDP injection", "hand-authored HTTPS loopback DSM", "trusted-device semantics"]},
            scenario)
    finally:
        await ha.close()
        store.close()


class SynologyCoreWithFixtureBackup(HomeAssistant):
    """Real transport/send guards; only absent Supervisor backup is substituted."""

    def __init__(self, *args):
        super().__init__(*args)
        self.posts = []
        self.backups = []
        self.events = []

    async def request(self, method, path, data=None, **kwargs):
        if method == "POST" and path.startswith("/config/config_entries/flow/"):
            # Never retain request bodies or native secret-bearing responses.
            self.posts.append(path)
            self.events.append("post")
        return await super().request(method, path, data, **kwargs)

    async def backup(self, name, *, send_guard):
        await send_guard()
        self.backups.append(name)
        self.events.append("backup")
        return {"id": "supervisor-synology-fixture", "name": name, "created_at": utcnow()}


def make_service(store, ha, spawn):
    return DeviceSetups(SimpleNamespace(state=SimpleNamespace()), store, ha,
        SimpleNamespace(verify=lambda _request: True), spawn=spawn)


def make_input(**values):
    # Delayed lookup keeps native qualification collected on the pre-feature base.
    return setup_api.DeviceSetupInput(**values)


async def test_real_service_synology_inputs_recovery_no_secret_persistence(
        synology_core_container, synology_scenario, tmp_path):
    name, base, config, provenance = synology_core_container
    scenario = synology_scenario
    store = Store(tmp_path / "controller.sqlite3")
    ha = SynologyCoreWithFixtureBackup(await owner_token(base), base + "/api",
        base.replace("http:", "ws:") + "/api/websocket")
    ha.store = store
    tasks = []

    def spawn(_id, coroutine):
        task = asyncio.create_task(coroutine)
        tasks.append(task)
        return task

    async def settled():
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=90)

    await ha.start()
    try:
        core = (await ha.request("GET", "/config"))["version"]
        assert core == provenance["expected_core"]
        owner = (await ha.ws("auth/current_user"))["id"]
        assert await ha.owner(owner)
        store.set_setting("owner", owner)
        store.set_setting("csrf_secret", secrets.token_hex(32))
        service = make_service(store, ha, spawn)
        flow = await discover(ha, provenance, scenario)
        proposal = await service.create(owner_request(owner, "synology-create"),
            DeviceSetupCreate(candidate_id=flow["flow_id"], integration_domain="synology_dsm"))
        assert proposal.state == "proposed" and proposal.can_approve
        assert not ha.posts and not ha.backups
        approval = DeviceSetupApprove(expected_revision=proposal.revision, proposal_digest=proposal.proposal_digest,
            credentials={"username": scenario["username"], "password": scenario["password"]})
        assert (await service.approve(owner_request(owner, "synology-approve"), proposal.id, approval)).state == "preparing"
        await settled()
        result = await service.detail(owner_request(owner, "read-2sa"), proposal.id)
        assert result.state == "needs_input" and result.input.step == "2sa" and result.input.error is None
        assert result.input.can_submit
        for key, otp, expected_step, expected_error in (
            ("wrong-otp", "000000", "2sa", "otp_failed"),
            ("correct-otp", scenario["otp_code"], "backup_share", None)):
            await service.input(owner_request(owner, key), proposal.id,
                make_input(expected_revision=result.revision, step="2sa", otp_code=otp))
            await settled()
            result = await service.detail(owner_request(owner, "read-" + key), proposal.id)
            assert result.state == "needs_input" and result.input.step == expected_step
            assert result.input.error == expected_error and result.input.can_submit
        assert [option.model_dump() for option in result.input.options] == [
            {"value": share["path"], "label": share["name"]} for share in scenario["writable_shares"]]
        await service.input(owner_request(owner, "select-share"), proposal.id,
            make_input(expected_revision=result.revision, step="backup_share", backup_share="/home-backups"))
        await settled()
        result = await service.detail(owner_request(owner, "read-created"), proposal.id)
        assert result.state == "configured"
        entry_id = result.integration.id
        device, entities, available, device_count = await readback(ha, entry_id)
        result = await service.detail(owner_request(owner, "read-ready"), proposal.id)
        assert result.device_count == device_count and result.entity_count == len(entities)
        assert result.available_entity_count == len(available) >= 1
        assert len(ha.posts) == 4 and len(ha.backups) == 1
        assert ha.events == ["backup", "post", "post", "post", "post"]
        await ha.close()
        store.close()
        await asyncio.to_thread(docker, "restart", "--time", "30", name)
        store = Store(tmp_path / "controller.sqlite3")
        ha.store = store
        await ha.start()
        await wait_ready(ha)
        restarted_device, restarted_entities, _, _ = await readback(ha, entry_id)
        assert restarted_device["id"] == device["id"]
        assert {row["entity_id"] for row in restarted_entities} == {row["entity_id"] for row in entities}
        service = make_service(store, ha, spawn)
        service.recover()
        assert (await service.approve(owner_request(owner, "synology-approve"), proposal.id, approval)).state == "configured"
        await settled()
        result = await service.detail(owner_request(owner, "read-recovered"), proposal.id)
        assert result.state == "configured" and result.integration.id == entry_id
        assert len(ha.posts) == 4 and len(ha.backups) == 1
        assert result.device_count == device_count and result.entity_count == len(entities)
        assert (tmp_path / "snapshots" / (proposal.id + ".sqlite3")).is_file()
        for target in [*tmp_path.glob("controller.sqlite3*"), *list((tmp_path / "snapshots").rglob("*"))]:
            if target.is_file():
                assert_no_secrets(target.read_bytes(), scenario)
        await inspect_entry(ha, 1)
        rows = journal(config, scenario)
        preserve_evidence(tmp_path, {**provenance, "core": core, "mode": "service",
            "result": "all_runtime_assertions_passed", "finished_at": utcnow(), "operations": summarize(rows),
            "rejected_operations": 0, "flow_posts": len(ha.posts), "fixture_backups": len(ha.backups),
            "backup_precedes_first_post": True, "full_core_restart": True, "controller_store_reopened": True,
            "approval_replay_caused_no_effects": True, "no_controller_secret_persistence": True,
            "device_count": device_count, "entity_count": len(entities), "available_entity_count": len(available),
            "real": ["Core owner check", "product DeviceSetups", "native flow and DSM library",
                "coordinator/platforms", "registry/state readback", "approval idempotency and store recovery"],
            "fixtures": ["SSDP injection", "HTTPS loopback DSM", "Supervisor backup", "outer HTTP session transport"]},
            scenario)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await ha.close()
        store.close()
