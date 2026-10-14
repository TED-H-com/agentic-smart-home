import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import httpx
import pytest
from agentic_controller.app import create_app
from agentic_controller.ha import HomeAssistant

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "host"))
from agentic_host.artifacts import fetch  # noqa: E402
from agentic_host.daemon import Jobs  # noqa: E402
from agentic_host.manager import HostError, Manager, atomic, domain_xml, network_xml, validate_profile  # noqa: E402


def module(path):
    spec = importlib.util.spec_from_file_location("host_script", ROOT / path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_broker_restart_keeps_the_socket_directory_containers_mount():
    # The Runner and Controller bind-mount /run/agentic-home when they start. systemd removes a
    # RuntimeDirectory on stop by default, so a broker-only restart would leave them a stale directory.
    unit = (ROOT / "host/systemd/agentic-home-host.service").read_text().splitlines()
    assert "RuntimeDirectory=agentic-home" in unit and "RuntimeDirectoryPreserve=yes" in unit
    compose = (ROOT / "host/compose.yaml").read_text()
    assert compose.count("/run/agentic-home:/run/agentic-home") == 2


def test_locked_profiles_and_real_libvirt_schema():
    bases = json.loads((ROOT / "host/bases.lock.json").read_text())
    for path in (ROOT / "host/profiles").glob("*.json"):
        profile = validate_profile(json.loads(path.read_text()), bases)
        tree = ET.fromstring(domain_xml(profile, "/var/lib/libvirt/images/agentic-home/haos.qcow2",
                                        "/var/lib/libvirt/images/agentic-home/nvram.fd", "21a5a5c2-1111-4444-aaaa-abcdef123456"))
        assert tree.findtext("os/type") == "hvm"
        assert tree.findtext("os/loader") == "/usr/share/OVMF/OVMF_CODE_4M.fd"
        assert tree.find("devices/interface/source").get("network") == "agentic-home"
        assert tree.findtext("vcpu") == "2"
    assert ET.fromstring(network_xml("owned")).find("ip/dhcp/host").get("ip") == "192.168.177.2"


def test_installer_does_not_embed_disk_or_account_and_integrity_before_extract():
    build = module("scripts/build_host.py")
    config = build.autoinstall({"id": "laptop-amd64"})["autoinstall"]
    assert set(config["interactive-sections"]) == {"network", "storage", "identity"}
    assert "identity" not in config and "storage" not in config
    assert "sha256sum -c" in config["late-commands"][0]
    grub = build.boot_config("menuentry 'upstream rescue' {}")
    assert "autoinstall ds=nocloud\\;s=/cdrom/agentic-seed/" in grub
    assert "upstream rescue" in grub


def test_corrupt_cache_and_unknown_base_refused(tmp_path):
    (tmp_path / "image.iso").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        fetch({"filename": "image.iso", "sha256": "0" * 64, "url": "https://example.invalid"}, tmp_path)
    with pytest.raises(HostError, match="Profile fields"):
        validate_profile({"base": "unknown"}, {"bases": {}})


def test_never_operates_unowned_domain(tmp_path):
    calls = []
    def execute(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="<domain><uuid>someone-else</uuid></domain>")
    manager = Manager(tmp_path / "state", tmp_path / "product", tmp_path / "images", execute)
    atomic(manager.state_file, {"owner": "our-owner"})
    with pytest.raises(HostError, match="not owned"):
        manager.action("shutdown")
    assert not any("shutdown" in call for call in calls)
    with pytest.raises(HostError, match="Unknown lifecycle"):
        manager.action("shell")


def test_interrupted_jobs_never_replay(tmp_path):
    manager = Manager(tmp_path)
    atomic(tmp_path / "jobs.json", {"request-id": {"state": "running", "id": "request-id",
                                                 "request": {"action": "snapshot", "id": "request-id"}}})
    jobs = Jobs(manager)
    assert jobs.items["request-id"]["state"] == "needs_reconcile"
    assert jobs.start({"id": "request-id", "action": "snapshot"})["state"] == "needs_reconcile"
    with pytest.raises(HostError, match="Idempotency"):
        jobs.start({"id": "request-id", "action": "restore"})


def test_status_bridge_update_contract_comes_from_fixed_updater_not_state(tmp_path):
    from agentic_host.updater import INGRESS_BRIDGE_CONTRACT

    calls = []

    def execute(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1, stdout="")

    manager = Manager(tmp_path, product=tmp_path / "product", execute=execute)
    # Neither persisted state nor a product version is a capability proof.
    atomic(manager.state_file, {"controller_update": {"ingress_bridge_contract": 999}})
    status = manager.status()
    contract = status["controller_update"]["ingress_bridge_contract"]
    assert type(contract) is int and contract == INGRESS_BRIDGE_CONTRACT == 1
    assert calls == [("virsh", "--connect", "qemu:///system", "domstate", "agentic-home-haos"),
                     ("ip", "-json", "route", "show", "default")]
    assert manager.state()["controller_update"]["ingress_bridge_contract"] == 999


def test_forwarding_recovery_never_modifies_an_external_network(tmp_path):
    calls = []
    owned = False

    def execute(*args, **kwargs):
        calls.append(args)
        if "net-dumpxml" in args:
            return SimpleNamespace(returncode=0, stdout=network_xml("our-owner" if owned else "other-owner"))
        return SimpleNamespace(returncode=1 if "-C" in args else 0, stdout="")

    manager = Manager(tmp_path, execute=execute)
    atomic(manager.state_file, {"owner": "our-owner"})
    manager.refresh_forwarding()
    assert not any(call[0] == "iptables" for call in calls)
    owned = True
    manager.refresh_forwarding()
    changes = [call for call in calls if "-I" in call]
    assert len(changes) == 2 and all("virbr-ah" in call for call in changes)


def test_restore_blocks_start_and_path_traversal(tmp_path):
    manager = Manager(tmp_path)
    atomic(manager.state_file, {"owner": "owner", "restore_pending": "snapshot"})
    with pytest.raises(HostError, match="Interrupted restore"):
        manager.start()
    with pytest.raises(HostError, match="Invalid snapshot"):
        manager.restore("../../another-vm")


async def test_host_auth_works_without_ha_and_rejects_forged_ingress(store, ha, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_DATA", str(tmp_path))
    (tmp_path / "access-key").write_text("local-machine-secret-key-12345")
    app = create_app(store=store, ha=ha, host_mode=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8099") as client:
        assert (await client.get("/api/session", headers={"X-Remote-User-Id": "owner"})).status_code == 401
        assert (await client.get("/api/deployment")).json() == {"mode": "host", "authenticated": False}
        assert (await client.post("/api/host/login", json={"key": "local-machine-secret-key-12345"})).status_code == 403
        client.headers["Origin"] = "http://localhost:8099"
        response = await client.post("/api/host/login", json={"key": "local-machine-secret-key-12345"})
        assert response.status_code == 200 and "httponly" in response.headers["set-cookie"].lower()
        session = (await client.get("/api/session")).json()
        assert session["principal"] == "host-owner"
        client.headers["X-Agentic-CSRF"] = session["csrf"]
        assert (await client.post("/api/pause", json={"paused": True})).status_code == 200
        # A local host session is not evidence of an HA owner: household mutation still fails closed.
        assert (await client.post("/api/policy", json={"entities": []})).status_code == 403
        assert (await client.get("/api/session", headers={"Host": "evil.test"})).status_code == 403


async def test_external_backup_uses_owner_websocket_and_waits_for_completion():
    class External(HomeAssistant):
        def __init__(self):
            super().__init__("test", external=True)
            self.calls = []

        async def ws(self, command, **kwargs):
            self.calls.append((command, kwargs))
            return {"slug": "abcdef12", "homeassistant": True, "healthy": True}

    ha = External()
    assert (await ha.backup("test"))["id"] == "abcdef12"
    first = ha.calls[1]
    assert first[0] == "supervisor/api" and first[1]["endpoint"] == "/backups/new/partial"
    assert first[1]["data"]["background"] is False and first[1]["_receive_timeout"] == 610


async def test_external_agent_diagnoses_with_ha_unavailable_without_running_host_action(store, ha, tmp_path, monkeypatch):
    import asyncio

    from agentic_controller import host
    calls = []

    async def host_call(method="GET", body=None, *, store):
        calls.append(method)
        assert method == "GET"
        return {"vm": "shut off", "kvm": True, "provisioned": True, "network": "nat", "storage_free_bytes": 10**11}

    class Runner:
        async def run(self, request):
            assert 'shut off' in request["prompt"] and "token" not in request["prompt"]
            return {"message": "HAOS 已關機，可以啟動。", "suggested_action": "start"}

    monkeypatch.setattr(host, "host_call", host_call)
    monkeypatch.setenv("AGENTIC_DATA", str(tmp_path))
    (tmp_path / "access-key").write_text("local-machine-secret-key-12345")
    app = create_app(store=store, ha=ha, runner=Runner(), host_mode=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8099",
                                headers={"Origin": "http://localhost:8099"}) as client:
        await client.post("/api/host/login", json={"key": "local-machine-secret-key-12345"})
        client.headers["X-Agentic-CSRF"] = (await client.get("/api/session")).json()["csrf"]
        client.headers["Idempotency-Key"] = "host-test-question"
        response = await client.post("/api/host/ask", json={"message": "為什麼 HA 沒有回應？"})
        assert response.status_code == 202
        await asyncio.gather(*list(app.state.running.values()))
        result = await client.get("/api/host/tasks/" + response.json()["id"])
        assert result.json()["answer"]["suggested_action"] == "start"
        assert calls == ["GET"] and not ha.writes
        again = await client.post("/api/host/ask", json={"message": "為什麼 HA 沒有回應？"})
        assert again.json()["id"] == response.json()["id"] and calls == ["GET"]


def test_stage_same_target_and_manifest_tampering(tmp_path, monkeypatch):
    import hashlib
    install = module("host/install.py")
    target = tmp_path / "target"
    root = target / "opt/agentic-home"
    (root / "host/systemd").mkdir(parents=True)
    (target / "etc/systemd/system").mkdir(parents=True)
    (target / "etc/os-release").write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
    unit = root / "host/systemd/agentic-home-firstboot.service"
    unit.write_text("[Unit]\nDescription=fixture\n")
    (root / "BUNDLE.sha256.json").write_text(json.dumps({"host/systemd/agentic-home-firstboot.service":
                                                        hashlib.sha256(unit.read_bytes()).hexdigest()}))
    monkeypatch.setattr(install, "ROOT", root)
    install.stage_target(target, {"id": "fixture"})
    assert (target / "etc/systemd/system/multi-user.target.wants/agentic-home-firstboot.service").is_symlink()
    unit.write_text("tampered")
    with pytest.raises(HostError, match="checksum"):
        install.verify_bundle(root)


def test_desktop_key_is_not_distributed_to_additional_linux_accounts(tmp_path, monkeypatch):
    import stat
    install = module("host/install.py")
    accounts = []
    for uid in (1000, 1001, 1002):
        home = tmp_path / str(uid)
        home.mkdir()
        accounts.append(SimpleNamespace(pw_uid=uid, pw_gid=uid, pw_dir=str(home)))
    monkeypatch.setattr(install.pwd, "getpwall", lambda: accounts)
    ownership = []
    monkeypatch.setattr(install.os, "chown", lambda path, uid, gid: ownership.append((uid, gid)))
    key = tmp_path / "key"
    key.write_text("fixture-only-key")
    install.desktop_login_note(key)
    assert ownership == [(1000, 1000)]
    assert stat.S_IMODE((tmp_path / "1000/Agentic-Home.txt").stat().st_mode) == 0o600
    assert not (tmp_path / "1001/Agentic-Home.txt").exists()
    assert not (tmp_path / "1002/Agentic-Home.txt").exists()


def test_installed_manifest_matches_release_and_owner_console_permissions(tmp_path, monkeypatch):
    import stat
    import tomllib
    install = module("host/install.py")
    monkeypatch.setattr(install, "STATE", tmp_path)
    ownership = []
    monkeypatch.setattr(install.os, "chown", lambda path, uid, gid: ownership.append((path, uid, gid)))
    install.record_installation({"id": "laptop-amd64"})
    path = tmp_path / "installed.json"
    metadata = json.loads(path.read_text())
    release = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert metadata["version"] == release
    assert metadata["profile"] == "laptop-amd64" and metadata["installed_at"] > 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert ownership == [(path, 0, 1000)]
