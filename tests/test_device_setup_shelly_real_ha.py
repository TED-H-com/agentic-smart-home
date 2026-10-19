"""Real pinned Core plus synthetic Gen1 SHSW-1 HTTP/CoIoT, no household access.

The native flow, aioshelly client, coordinator and registries are unmodified.
The reported model is not physical generation attestation. These tests qualify
only the synthetic Gen1 protocol, never generic RPC/Bluetooth/device control.
Run serially with pytest-disktmp --real-ha; local images are never pulled.
"""
import asyncio
import hashlib
import json
import os
import shutil
import socket
import subprocess
import uuid
from pathlib import Path

import pytest
from agentic_controller.ha import HomeAssistant
from agentic_controller.models import utcnow
from agentic_controller.store import Store
from test_device_setup_real_ha import docker, owner_token

CORE_VERSIONS = ("2026.10.1", "2026.10.2")
MAC = "AABBCC112233"
COUNTER = "/states/sensor.agentic_shelly_fixture_requests"


@pytest.fixture(params=CORE_VERSIONS, ids=lambda version: "core-" + version)
def shelly_core_container(tmp_path, request):
    if not request.config.getoption("--real-ha"):
        pytest.skip("Use --real-ha for isolated native Shelly qualification")
    assert tmp_path.resolve().is_relative_to((Path.home() / "agent-work/tmp/pytest").resolve())
    image_ref = "ghcr.io/home-assistant/home-assistant:" + request.param
    image = json.loads(docker("image", "inspect", image_ref))[0]
    config = tmp_path / "ha-config"
    config.mkdir()
    shutil.copytree(Path(__file__).parent / "fixtures/agentic_shelly_fixture",
        config / "custom_components/agentic_shelly_fixture", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (config / "configuration.yaml").write_text("""homeassistant:
  name: Isolated synthetic Shelly contract test
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
shelly:
  coap_port: 5684
agentic_shelly_fixture:
""")
    run_id = uuid.uuid4().hex
    name = "agentic-shelly-contract-" + run_id[:12]
    # Docker's port=0 allocation changes on restart. Preserve the test's exact
    # loopback authority endpoint across the full process restart instead.
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        fixed_port = reservation.getsockname()[1]
    try:
        docker("run", "-d", "--name", name, "--memory", "1536m", "--cpus", "2",
            "-p", f"127.0.0.1:{fixed_port}:8123", "-v", f"{config}:/config", image["Id"])
        container = json.loads(docker("inspect", name))[0]
        assert container["Image"] == image["Id"]
        port = container["NetworkSettings"]["Ports"]["8123/tcp"][0]["HostPort"]
        yield name, f"http://127.0.0.1:{port}", config, {
            "expected_core": request.param, "image": image_ref, "image_id": container["Image"],
            "repo_digests": image.get("RepoDigests", []), "run_id": run_id, "started_at": utcnow()}
    finally:
        subprocess.run(["docker", "exec", name, "chown", "-R", f"{os.getuid()}:{os.getgid()}", "/config"],
                       capture_output=True)
        subprocess.run(["docker", "rm", "-fv", name], capture_output=True)


async def wait_ready(ha):
    for _ in range(120):
        try:
            services = await ha.ws("get_services")
            if "discover" in services.get("agentic_shelly_fixture", {}):
                return
        except Exception:
            pass
        await asyncio.sleep(0.5)
    raise AssertionError("Synthetic Shelly endpoint failed to load")


async def discover(ha):
    await wait_ready(ha)
    await ha.request("POST", "/services/agentic_shelly_fixture/discover", {})
    flows = [flow for flow in await ha.ws("config_entries/flow/progress") if flow["handler"] == "shelly"]
    assert len(flows) == 1, "Unmodified native Shelly validation did not produce one confirmation"
    flow = flows[0]
    assert flow["step_id"] == "confirm_discovery"
    assert flow["context"]["source"] == "zeroconf" and flow["context"]["unique_id"] == MAC
    form = await ha.request("GET", "/config/config_entries/flow/" + flow["flow_id"])
    assert {key: form[key] for key in ("type", "handler", "step_id", "data_schema", "errors")} == {
        "type": "form", "handler": "shelly", "step_id": "confirm_discovery", "data_schema": [], "errors": {}}
    assert form["description_placeholders"]["model"] == "Shelly 1"
    assert form["description_placeholders"]["host"] == "127.0.0.1"
    return flow, form


async def readback(ha, entry_id):
    for _ in range(120):
        entries = [entry for entry in await ha.ws("config_entries/get") if entry["domain"] == "shelly"]
        assert len(entries) == 1 and entries[0]["entry_id"] == entry_id
        devices = [device for device in await ha.ws("config/device_registry/list") if entry_id in device["config_entries"]]
        entities = [entity for entity in await ha.ws("config/entity_registry/list") if entity["config_entry_id"] == entry_id]
        states = {state["entity_id"]: state for state in await ha.request("GET", "/states")}
        switches = [entity for entity in entities if entity["entity_id"].startswith("switch.")
                    and entity["entity_id"] in states and states[entity["entity_id"]]["state"] == "off"]
        if entries[0]["state"] == "loaded" and len(devices) == 1 and len(switches) == 1:
            assert ["shelly", MAC] in devices[0]["identifiers"]
            assert switches[0]["device_id"] == devices[0]["id"]
            return entries[0], devices[0], entities, switches[0]
        await asyncio.sleep(0.5)
    raise AssertionError("Native Shelly coordinator did not load an off relay entity")


def preserve_evidence(tmp_path, report):
    root = Path(__file__).parents[1]
    sources = ["tests/test_device_setup_shelly_real_ha.py", "tests/fixtures/agentic_shelly_fixture/__init__.py",
               "tests/fixtures/agentic_shelly_fixture/manifest.json", "controller/agentic_controller/device_setup.py"]
    report["source_sha256"] = {path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in sources}
    encoded = json.dumps(report, indent=2) + "\n"
    (tmp_path / "shelly-qualification.json").write_text(encoded)
    if configured := os.environ.get("AGENTIC_SHELLY_EVIDENCE_DIR"):
        destination = Path(configured).expanduser().resolve()
        assert destination.is_relative_to((Path.home() / "agent-work/tmp").resolve())
        destination.mkdir(parents=True, exist_ok=True)
        with (destination / f"{report['mode']}-{report['core']}-{report['run_id']}.json").open("x") as output:
            output.write(encoded)


async def test_real_native_shelly_confirmation_startup_and_core_restart(shelly_core_container, tmp_path):
    name, base, config, provenance = shelly_core_container
    token = await owner_token(base)
    store = Store(tmp_path / "controller.sqlite3")
    ha = HomeAssistant(token, base + "/api", base.replace("http:", "ws:") + "/api/websocket")
    ha.store = store
    await ha.start()
    try:
        core = (await ha.request("GET", "/config"))["version"]
        assert core == provenance["expected_core"]
        flow, _ = await discover(ha)
        before = await ha.request("GET", COUNTER)
        assert before["attributes"]["rejected_operations"] == 0
        response = await ha.request("POST", "/config/config_entries/flow/" + flow["flow_id"], {})
        assert response["type"] == "create_entry" and response["handler"] == "shelly"
        assert response["flow_id"] == flow["flow_id"]
        entry_id = response["result"]["entry_id"]
        assert response["result"]["domain"] == "shelly" and response["result"]["source"] == "zeroconf"
        _, device, entities, switch = await readback(ha, entry_id)
        counter = await ha.request("GET", COUNTER)
        assert counter["attributes"]["rejected_operations"] == 0
        first_count = int(counter["state"])
        await ha.close()
        await asyncio.to_thread(docker, "restart", "--time", "30", name)
        await ha.start()
        await wait_ready(ha)
        _, restarted_device, restarted_entities, restarted_switch = await readback(ha, entry_id)
        assert restarted_device["id"] == device["id"] and restarted_switch["entity_id"] == switch["entity_id"]
        assert {entity["entity_id"] for entity in entities} == {entity["entity_id"] for entity in restarted_entities}
        counter = await ha.request("GET", COUNTER)
        operations = counter["attributes"]["operations"]
        assert int(counter["state"]) > first_count
        assert operations["fixture_boot"] == 2 and counter["attributes"]["rejected_operations"] == 0
        assert set(operations) == {"fixture_boot", "http:/shelly", "http:/settings", "http:/status",
                                  "http:/cit/d", "coap:/cit/s"}
        assert all(operations[key] >= 3 for key in ("http:/shelly", "http:/settings", "http:/status", "http:/cit/d"))
        journal = [json.loads(line) for line in (config / "agentic_shelly_fixture_requests.jsonl").read_text().splitlines()]
        assert len(journal) == int(counter["state"]) and all(row["operation"] != "rejected" for row in journal)
        preserve_evidence(tmp_path, {**provenance, "core": core, "mode": "native", "model": "SHSW-1",
            "reported_model": "Shelly 1", "result": "all_runtime_assertions_passed", "finished_at": utcnow(),
            "operations": operations, "device_count": 1, "entity_count": len(entities),
            "switch_state": "off", "full_core_restart": True, "confirmation_posts": 1,
            "real": ["pinned Core", "native Shelly flow", "unmodified aioshelly HTTP/CoIoT",
                     "native coordinator and relay entity", "exact registry readback", "full Core restart"],
            "fixtures": ["synthetic Zeroconf injection", "loopback SHSW-1 HTTP and CoIoT responder"],
            "not_tested": ["physical Shelly", "physical generation attestation", "RPC/Bluetooth variants",
                           "LAN multicast discovery", "Supervisor backup", "HAOS", "live household",
                           "product DeviceSetups service", "relay actuation", "unlisted Core versions"]})
    finally:
        await ha.close()
        store.close()
