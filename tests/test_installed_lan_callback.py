"""Fake-only callback and private checkpoint regressions; no Docker or network."""
import errno
import json
import stat
import subprocess
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest
import test_linux_install as install

URL = "http://198.18.42.108"
ISO = "a" * 64
NATIVE = {name: True for name in (
    "management_reachable", "dhcp_ipv4", "ipv6_ra", "lan_default_gateway", "management_gateway_absent",
    "native_discovery_adapter_auto", "native_mdns_flow_observed", "product_observation_ready")}


def wire(status="failed", code="socket_timeout"):
    return json.dumps({"schema": 1, "kind": "installed_ha_lan_callback", "status": status, "error_code": code})


@pytest.mark.parametrize(("error", "code"), [
    (TimeoutError("private URL"), "socket_timeout"),
    (urllib.error.URLError(TimeoutError("private URL")), "socket_timeout"),
    (urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, "private address")), "connection_refused"),
    (urllib.error.URLError(OSError(errno.ENETUNREACH, "private route")), "network_unreachable"),
    (OSError(errno.EHOSTUNREACH, "private route"), "network_unreachable"),
    (RuntimeError("private details"), "unexpected_error"),
])
def test_request_error_retains_original_cause_and_one_ten_second_attempt(monkeypatch, capsys, error, code):
    calls = []
    def opening(url, timeout):
        calls.append((url, timeout))
        raise error
    monkeypatch.setattr(urllib.request, "urlopen", opening)
    with pytest.raises(type(error)) as caught:
        exec(install.lan_callback_probe_script(URL), {})
    assert caught.value is error and calls == [(URL + "/api/", 10)]
    output = capsys.readouterr().out
    assert install.lan_callback_diagnostics(output, succeeded=False) == {"status": "failed", "error_code": code}
    assert "private" not in output and "http:" not in output


@pytest.mark.parametrize("status", [200, 401, 403, 404, 503])
def test_only_native_401_is_success_and_http_failure_stays_an_assertion(monkeypatch, capsys, status):
    calls = []
    def opening(url, timeout):
        calls.append((url, timeout))
        if status != 200:
            raise urllib.error.HTTPError(url, status, "private body", {}, None)
    monkeypatch.setattr(urllib.request, "urlopen", opening)
    if status == 401:
        exec(install.lan_callback_probe_script(URL), {})
    else:
        with pytest.raises(AssertionError):
            exec(install.lan_callback_probe_script(URL), {})
    output = capsys.readouterr().out
    assert install.lan_callback_diagnostics(output, succeeded=status == 401) == {
        "status": "passed" if status == 401 else "failed",
        "error_code": None if status == 401 else "unexpected_http_status"}
    assert calls == [(URL + "/api/", 10)]


@pytest.mark.parametrize("raw", [
    "", "private stdout", " " * 2049, b"\xff", "\ud800", "[]",
    wire().replace('"schema": 1', '"schema": 1, "schema": 1'),
    wire().replace('"status": "failed"', '"status": "failed", "status": "passed"'),
    wire().replace('"schema": 1', '"schema": true'),
    wire().replace('"schema": 1', '"schema": NaN'),
    wire().replace('"schema": 1', '"schema": Infinity'),
    wire().replace('"schema": 1', '"schema": 1e9999'),
    wire().replace('"schema": 1', '"schema": -1e9999'),
    wire().replace('"socket_timeout"', '"private-secret"'),
    wire().replace('"socket_timeout"', '[]'),
    wire().replace('"socket_timeout"', 'null'),
    wire()[:-1] + ', "url": "http://private.invalid"}',
    "[" * 1000 + "0" + "]" * 1000,
])
def test_untrusted_or_ambiguous_diagnostics_are_not_retained(raw):
    assert install.lan_callback_diagnostics(raw, succeeded=False) is None


def test_exit_status_cannot_be_overruled_by_a_success_diagnostic():
    assert install.lan_callback_diagnostics(wire("passed", None), succeeded=False) is None
    assert install.lan_callback_diagnostics(wire(), succeeded=True) is None


def test_private_partial_is_atomic_owner_only_and_stays_outside_package_inputs(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    (root / "build").mkdir(parents=True)
    (root / "evidence").mkdir()
    monkeypatch.setattr(install, "ROOT", root)
    request = SimpleNamespace(config=SimpleNamespace(option=SimpleNamespace(xmlpath=str(tmp_path / "junit.xml"))))
    path = install.lan_partial_path(request, "laptop-amd64")
    assert path == tmp_path / "installed-ha-lan-partial-laptop-amd64.json"
    assert install.retain_lan_partial(path, {"status": "incomplete", "complete": False})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob("*.partial-*"))
    previous = path.read_bytes()
    def broken_replace(*args):
        raise OSError("private path")
    monkeypatch.setattr(install.os, "replace", broken_replace)
    assert install.retain_lan_partial(path, {"status": "changed"}) is False
    assert path.read_bytes() == previous and not list(tmp_path.glob("*.partial-*"))
    for xml in (None, str(root / "evidence/pytest.xml"), str(root / "pytest.xml")):
        request.config.option.xmlpath = xml
        assert install.lan_partial_path(request, "server-amd64") == root / "build/installed-ha-lan-partial-server-amd64.json"
    with pytest.raises(AssertionError):
        install.lan_partial_path(request, "../../private")


@pytest.fixture
def rehearsal(tmp_path, monkeypatch):
    path = tmp_path / "partial.json"
    calls, phases, snapshots = [], [], []
    state = {"shutdown_calls": [], "failure_stage": "after_outer_reboot", "failure": subprocess.CalledProcessError(
        1, ["docker", "exec"], output=wire(), stderr="private original stderr")}
    def command(*args, **options):
        calls.append((args, options))
        if args[:2] == ("docker", "exec"):
            stage = "initial" if len(snapshots) == 0 else "after_outer_reboot"
            snapshots.append(json.loads(path.read_text()))
            assert snapshots[-1]["stages"][stage]["native"] == NATIVE
            assert snapshots[-1]["stages"][stage]["callback"]["status"] == "pending"
            assert options["timeout"] == 20 and options["input"] == install.lan_callback_probe_script(URL)
            if stage == state["failure_stage"]:
                raise state["failure"]
            return wire("passed", None)
        remote = args[-1]
        if "AGENTIC_LAN_PROBE_PHASE=onboard" in remote:
            return json.dumps({"native_owner_fixture": True, "product_owner_connection": True})
        if "AGENTIC_LAN_PROBE_PHASE=wired" in remote:
            return json.dumps({**NATIVE, "callback_url": URL})
        if "AGENTIC_LAN_PROBE_PHASE=nat" in remote:
            return json.dumps({"management_reachable": True, "nat_gateway_restored": True, "lan_nic_removed": True})
        if remote == "agentic-home ha-lan":
            return json.dumps({"interfaces": [{"address": "198.18.42.10", "id": "b" * 64}]})
        assert remote in {"agentic-home shutdown", "agentic-home start", "agentic-home configure_lan --disable-lan",
                          "agentic-home configure_lan --interface-id " + "b" * 64}
        return ""
    monkeypatch.setattr(install, "command", command)
    def readiness(args, **options):
        if args == ["ssh", "fixture", "agentic-home shutdown"]:
            state["shutdown_calls"].append((args, options))
            assert options["capture_output"] is True and options["text"] is True
            assert 0 < options["timeout"] <= 240
            identifier = f"{len(state['shutdown_calls']):032x}"
            job = {"id": identifier, "request": {"id": identifier, "action": "shutdown", "snapshot": None},
                   "state": "succeeded", "result": {"vm": "shut off"}}
            return subprocess.CompletedProcess(args, 0, f"Operation {identifier}\n" + json.dumps(job), "")
        assert args == ["ssh", "fixture", "python3 -"] and options["timeout"] == 15
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(install.subprocess, "run", readiness)
    def reboot(ssh, seconds):
        assert seconds == 900 and len(snapshots) == 1
        assert json.loads(path.read_text())["stages"]["initial"]["callback"]["status"] == "passed"
        return {"boot_id_changed": True, "service_active_in_new_boot": True, "future_private_field": "secret"}
    monkeypatch.setattr(install, "reboot_outer", reboot)
    def execute():
        return install.installed_lan(["ssh", "fixture"], "owned-container", phases.append, {},
                                     partial_path=path, profile="laptop-amd64", iso_hash=ISO)
    return execute, state, path, calls, phases, snapshots


@pytest.mark.parametrize("stage", ["initial", "after_outer_reboot"])
@pytest.mark.parametrize("kind", ["request", "outer_timeout", "malformed", "false_success"])
def test_failure_keeps_native_partial_and_original_exception_without_retry(rehearsal, stage, kind):
    execute, state, path, calls, phases, snapshots = rehearsal
    state["failure_stage"] = stage
    if kind == "outer_timeout":
        state["failure"] = subprocess.TimeoutExpired(["docker", "exec"], 20, output=wire("passed", None))
    elif kind in {"malformed", "false_success"}:
        state["failure"].stdout = "private" if kind == "malformed" else wire("passed", None)
    with pytest.raises(type(state["failure"])) as caught:
        execute()
    assert caught.value is state["failure"]
    value = json.loads(path.read_text())
    assert value["profile"] == "laptop-amd64" and value["iso_sha256"] == ISO
    assert value["status"] == "incomplete" and value["complete"] is False
    assert value["stages"][stage]["native"] == NATIVE
    expected = {"request": "socket_timeout", "outer_timeout": "peer_process_timeout",
                "malformed": "peer_process_failed", "false_success": "peer_process_failed"}[kind]
    assert value["stages"][stage]["callback"] == {"status": "failed", "error_code": expected}
    assert len(snapshots) == (1 if stage == "initial" else 2)
    assert len(state["shutdown_calls"]) == 1
    assert not any(args[-1] == "agentic-home configure_lan --disable-lan" for args, _ in calls)
    if stage == "after_outer_reboot":
        assert value["stages"]["initial"]["callback"]["status"] == "passed"
        assert value["outer_reboot_observation"] == {"boot_id_changed": True, "service_active_in_new_boot": True}
    assert not any(secret in path.read_text() for secret in (URL, "private", "secret", "owned-container", "callback_url"))


def test_success_still_requires_both_callbacks_and_nat_removal_but_partial_is_never_full_pass(rehearsal):
    execute, state, path, calls, phases, snapshots = rehearsal
    state["failure_stage"] = None
    result = execute()
    assert result["result"] == "passed" and result["removed"]["nat_gateway_restored"]
    assert len(state["shutdown_calls"]) == 2
    assert result["initial"]["native_callback_reachable_from_lan_peer"]
    assert result["after_outer_reboot"]["native_callback_reachable_from_lan_peer"]
    value = json.loads(path.read_text())
    assert value["status"] == "incomplete" and value["complete"] is False and len(snapshots) == 2
    assert all(row["callback"] == {"status": "passed", "error_code": None} for row in value["stages"].values())


def test_failed_checkpoint_write_cannot_replace_callback_failure(rehearsal, monkeypatch):
    execute, state, path, calls, phases, snapshots = rehearsal
    original = install.retain_lan_partial
    def retaining(path, value):
        if any(row["callback"]["status"] == "failed" for row in value["stages"].values()):
            return False
        return original(path, value)
    monkeypatch.setattr(install, "retain_lan_partial", retaining)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        execute()
    assert caught.value is state["failure"]
    assert len(state["shutdown_calls"]) == 1
    value = json.loads(path.read_text())
    assert value["stages"]["initial"]["callback"]["status"] == "passed"
    assert value["stages"]["after_outer_reboot"]["native"] == NATIVE
    assert value["stages"]["after_outer_reboot"]["callback"]["status"] == "pending"
