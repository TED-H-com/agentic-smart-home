"""Capability contract checks; no package/service/network mutation on the host."""
import io
import json
import os
import subprocess
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from agentic_host import compatibility as contracts
from agentic_host.bootstrap import InitialSetup, native_ha_status
from agentic_host.manager import HostError, Manager, run

CAPABILITIES = '<capabilities><host/><guest><os_type>hvm</os_type><arch name="x86_64"><domain type="kvm"/></arch></guest></capabilities>'
LIMITS = {"MemoryLimit": True, "SwapLimit": True, "PidsLimit": True, "CpuCfsQuota": True,
          "CgroupVersion": "2", "SecurityOptions": ["name=seccomp,profile=builtin", "name=apparmor"]}


def compatible_run(*args, **kwargs):
    assert kwargs == {"timeout": 3, "check": False}
    if args[0] == "virsh":
        body = "qemu:///system" if args[-1] == "uri" else CAPABILITIES
    elif args[0] == "ip":
        body = '[{"dst":"default","dev":"eth0","flags":[],"new_upstream_field":{}}]'
    elif args[:2] == ("docker", "info"):
        body = json.dumps({**LIMITS, "ServerVersion": "unrecognized-new-version"})
    elif args[:3] == ("docker", "compose", "version"):
        body = '{"version":"future-build-with-valid-capabilities","new_field":123}'
    elif args[:3] == ("docker", "compose", "up"):
        body = "--wait --no-deps"
    elif args[0] == "cp":
        body = "--reflink --sparse"
    elif args[0] == "qemu-img" and args[1] == "--help":
        body = "info check resize --output=json"
    elif args[:2] == ("qemu-img", "info"):
        body = '{"format":"qcow2","virtual-size":1048576,"new_field":true}'
    else:
        raise AssertionError(args)
    return SimpleNamespace(returncode=0, stdout=body)


@pytest.mark.parametrize("action", sorted(contracts.ACTIONS))
def test_required_capabilities_accept_new_versions_and_additive_fields(action):
    result = contracts.inspect(compatible_run, action)
    assert result["state"] == "supported" and result["reuse_for_mutations"] is False
    assert all(action in item["affected_actions"] for item in result["checks"])
    assert "future" not in json.dumps(result) and "eth0" not in json.dumps(result)


@pytest.mark.parametrize("route", [
    {}, {"dst": "default", "dev": True}, {"dst": "default", "dev": "eth0", "flags": "up"},
    {"dst": "private-invalid", "dev": "eth0"}, {"dst": "default", "dev": "eth0", "gateway": "private-invalid"},
    {"dst": "default", "dev": "eth0", "gateway": True},
])
def test_invalid_machine_route_contract_never_reports_ready(route):
    observation = contracts.network_status(lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps([route])))
    assert observation["ready"] is False and observation["error_code"] == "route_contract_unavailable"
    assert "private" not in json.dumps(observation)


@pytest.mark.parametrize("route", [
    {"dst": "default", "dev": "eth0", "flags": ["linkdown"]},
    {"dst": "default", "dev": "eth0", "type": "blackhole"},
    {"dst": "192.0.2.0/24", "dev": "eth0"}, {"dst": "default"},
])
def test_route_presence_is_not_usable_default_or_internet_proof(route):
    observation = contracts.network_status(lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps([route])))
    assert observation["ready"] is False and observation["internet_verified"] is False


def test_dual_stack_overlap_check_and_actual_default_contract():
    value = contracts.routes('[{"dst":"::/0","dev":"eth0"},{"dst":"fe80::/64","dev":"eth0"},{"dst":"192.0.2.0/24","dev":"eth0"}]')
    assert contracts.route_overlap(value) is False
    assert contracts.default_route(value)
    assert contracts.route_overlap(value + [{"dst": "192.168.177.2", "dev": "eth0"}])


@pytest.mark.parametrize("exception,expected", [
    (FileNotFoundError("private"), "command_missing"), (PermissionError("private"), "permission_denied"),
    (subprocess.TimeoutExpired(["private"], 3), "probe_timeout"),
])
def test_missing_or_timed_out_dependency_is_scoped_and_redacted(exception, expected):
    def execute(*args, **kwargs):
        if args[0] == "cp":
            raise exception
        return compatible_run(*args, **kwargs)
    report = contracts.inspect(execute, "snapshot")
    assert report["state"] == "blocked"
    failure = next(item for item in report["checks"] if item["state"] == "blocked")
    assert failure["id"] == "sparse_copy" and failure["error_code"] == expected
    assert failure["affected_actions"] == ["restore", "snapshot"]
    assert "private" not in json.dumps(report)
    assert contracts.inspect(execute, "shutdown")["state"] == "supported"


def test_mutation_rechecks_changed_dependency_and_never_dispatches_when_blocked(tmp_path, monkeypatch):
    changed, writes = [False], []
    def execute(*args, **kwargs):
        if "--readonly" in args:
            if changed[0]:
                return SimpleNamespace(returncode=0, stdout='{"unexpected":"private"}')
            return compatible_run(*args, **kwargs)
        if args[-2:] == ("domstate", "agentic-home-haos"):
            return SimpleNamespace(returncode=0, stdout="shut off")
        writes.append(args)
        return SimpleNamespace(returncode=0, stdout="")
    manager = Manager(tmp_path, execute=execute)
    monkeypatch.setattr(manager, "owned", lambda: object())
    assert manager.compatibility()["state"] == "not_checked"
    assert manager.start()["vm"] == "running"
    assert len(writes) == 1
    changed[0] = True
    with pytest.raises(HostError, match="host_contract_libvirt_connection_unavailable"):
        manager.start()
    assert len(writes) == 1
    assert manager.compatibility()["state"] == "blocked"


@pytest.mark.parametrize("action", ["snapshot", "restore"])
def test_copy_capability_failure_precedes_shutdown_or_restore_marker(tmp_path, monkeypatch, action):
    manager = Manager(tmp_path)
    monkeypatch.setattr(manager, "owned", lambda: object())
    mutations = []
    def execute(*args, **kwargs):
        if args[0] == "cp":
            return SimpleNamespace(returncode=0, stdout="unsupported private text")
        if "--readonly" in args:
            return compatible_run(*args, **kwargs)
        mutations.append(args)
        raise AssertionError("No live effect expected")
    manager.run = execute
    with pytest.raises(HostError, match="host_contract_sparse_copy_unavailable"):
        manager.restore("20261012T000000Z-1234abcd") if action == "restore" else manager.snapshot()
    assert not mutations and not manager.state_file.exists()
    assert "private" not in json.dumps(manager.compatibility())


def test_get_status_survives_missing_virsh_and_does_not_write_preflight(tmp_path, monkeypatch):
    def absent(*args, **kwargs):
        raise FileNotFoundError("private path")
    manager = Manager(tmp_path, execute=absent)
    monkeypatch.setattr("agentic_host.bootstrap.native_ha_status", lambda: {"reachable": False})
    result = manager.status()
    assert result["vm"] == "unknown" and result["compatibility"]["state"] == "not_checked"
    assert result["bootstrap"]["network"]["ready"] is False
    assert not (tmp_path / "compatibility.json").exists()


def test_bootstrap_preflight_refusal_precedes_environment_build_or_activation(tmp_path):
    product = tmp_path / "product"
    (product / "hermes").mkdir(parents=True)
    (product / "hermes/hermes.lock.json").write_text('{"commit":"fixture"}')
    calls = []
    def execute(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("docker", "info"):
            return SimpleNamespace(returncode=0, stdout=json.dumps({**LIMITS, "MemoryLimit": False}))
        return compatible_run(*args, **kwargs)
    manager = Manager(tmp_path / "state", product=product, execute=execute)
    with pytest.raises(HostError, match="host_contract_docker_limits_unavailable"):
        InitialSetup(manager).install()
    assert not (manager.root / "runtime.env").exists()
    assert not (manager.root / "bootstrap-hermes.json").exists()
    assert not any(args[:2] == ("docker", "build") for args in calls)


def test_onboarding_private_contract_distinguishes_change_absence_and_completion():
    known = [{"step": step, "done": True} for step in ("user", "core_config", "integration")]
    def observe(value):
        return native_ha_status(lambda *args, **kwargs: io.BytesIO(json.dumps(value).encode()))
    result = observe(known)
    assert result["contract"]["state"] == "supported" and result["onboarding_complete"]
    result = observe(known + [{"step": "new_step", "done": True}])
    assert result["contract"]["state"] == "unsupported" and not result["onboarding_complete"]
    def absent(*args, **kwargs):
        raise urllib.error.HTTPError("fixed", 404, "private", {}, None)
    result = native_ha_status(absent)
    assert result["contract"]["state"] == "route_absent" and result["http_status"] == 404
    assert not result["reachable"] and not result["onboarding_complete"]


@pytest.mark.parametrize("value", [[], {"format": "raw", "virtual-size": 1024},
                                  {"format": "qcow2", "virtual-size": True},
                                  {"format": "qcow2", "virtual-size": 1024, "backing-filename": "private"}])
def test_changed_qemu_image_schema_or_backing_chain_refused(value):
    with pytest.raises(ValueError):
        contracts.check_image(lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(value)), Path("fixed"))


def test_actual_readonly_docker_ip_compose_contracts(request):
    if not request.config.getoption("--containers"):
        pytest.skip("requires actual local Docker and Linux network tools")
    report = contracts.inspect(run, "bootstrap_hermes")
    assert report["state"] == "supported", report


def test_actual_qemu_machine_json_with_disk_fixture(request, tmp_path):
    if not request.config.getoption("--containers"):
        pytest.skip("requires local builder fixture image")
    image = "agentic-home-builder:0.2.0"
    available = subprocess.run(["docker", "image", "inspect", image], capture_output=True, timeout=120)
    if available.returncode:
        pytest.skip("builder image is installed-VM tooling, not available in regular CI")
    prefix = ["docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
              "--security-opt", "no-new-privileges:true", "--user", f"{os.getuid()}:{os.getgid()}",
              "--memory", "128m", "--pids-limit", "32", "--cpus", "1",
              "--mount", f"type=bind,src={tmp_path},dst=/fixture", image]
    subprocess.run([*prefix, "qemu-img", "create", "-f", "qcow2", "/fixture/test.qcow2", "8M"],
                   capture_output=True, text=True, timeout=120, check=True)
    def execute(*args, **kwargs):
        assert args[:3] == ("qemu-img", "info", "--output=json")
        return subprocess.run([*prefix, *args], capture_output=True, text=True, timeout=120, check=False)
    assert contracts.check_image(execute, Path("/fixture/test.qcow2"))
