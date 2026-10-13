import hashlib
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "host"))
from agentic_host import cli, support  # noqa: E402


@pytest.fixture
def inputs(tmp_path):
    root = tmp_path / "data"
    (root / "host").mkdir(parents=True)
    (root / "installed.json").write_text(json.dumps({"version": "0.3.1", "profile": "laptop-amd64",
                                                     "token": "credential-sentinel"}))
    (root / "host/state.json").write_text(json.dumps({"complete": True, "owner": "private-owner-id",
                                                      "restore_pending": "private-snapshot-path"}))
    release = tmp_path / "os-release"
    release.write_text('ID=ubuntu\nVERSION_ID="24.04"\nPRETTY_NAME="private-hostname"\n')
    calls = []

    def execute(name):
        calls.append(name)
        if name == "vm":
            return "ok", "shut off\n"
        if name == "journal":
            return "ok", json.dumps({"PRIORITY": "3", "MESSAGE": "credential-sentinel",
                                      "__CURSOR": "private-cursor", "URL": "https://private/?token=secret"}) + "\n"
        return "ok", ("LoadState=loaded\nActiveState=failed\nSubState=failed\nResult=exit-code\n"
                      "NRestarts=2\nEnvironment=credential-sentinel\nExecStart=/private/secret\n")

    return root, release, execute, calls


def test_fixed_diagnostics_redact_inputs_and_label_actual_observation(inputs):
    root, release, execute, calls = inputs
    report = support.collect(root=root, os_release=release, execute=execute)
    serialized = json.dumps(report)
    for private in ("credential-sentinel", "private-owner", "private-snapshot", "private-hostname", "private-cursor",
                    "https://private", "/private/secret", str(root)):
        assert private not in serialized
    assert calls == [*support.SERVICES, "vm", "journal"]
    assert report["live_effects"] == [] and report["ha_backend"] == "none"
    observations = {item["target_component"]: item for item in report["observations"]}
    assert observations["managed_haos_vm"]["data"] == {
        "state": "shut off", "provisioned": True, "restore_pending": True}
    assert observations["product_service_log_severity"]["data"]["severity_counts"]["error"] == 1
    for item in observations.values():
        assert item["observed_from"] == "outer_host_console"
        assert item["observed_at"] and item["version_refs"]["installed_application"] == "0.3.1"
        content = json.dumps(item["data"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        assert hashlib.sha256(content).hexdigest() == item["content_digest"]


def test_unavailable_units_and_host_do_not_require_web_or_claim_core_health(tmp_path):
    report = support.collect(root=tmp_path / "absent", os_release=tmp_path / "absent-os",
                             execute=lambda name: ("permission_denied", "private error"))
    assert all(item["status"] == "not_available" for item in report["observations"])
    assert "private error" not in json.dumps(report)
    assert all(item["data"] is None for item in report["observations"] if item["target_component"] in support.SERVICES)


def test_non_regular_and_oversized_files_are_not_followed_or_exported(inputs, tmp_path):
    root, release, execute, _ = inputs
    secret = tmp_path / "secret"
    secret.write_text('{"version":"password-secret-value","profile":"another-private-value"}')
    (root / "installed.json").unlink()
    (root / "installed.json").symlink_to(secret)
    release.unlink()
    os.mkfifo(release)
    (root / "host/state.json").write_text("x" * 40000)
    started = time.monotonic()
    report = support.collect(root=root, os_release=release, execute=execute)
    assert time.monotonic() - started < 2
    assert "password-secret" not in json.dumps(report)
    assert report["observations"][0]["reason"] == "manifest_unavailable"
    assert report["observations"][1]["reason"] == "os_release_unavailable"


def test_unrecognized_metadata_is_not_copied_to_report(inputs):
    root, release, _, _ = inputs
    (root / "installed.json").write_text('{"version":"sensitive-value","profile":["sensitive-value"]}')
    report = support.collect(root=root, os_release=release, execute=lambda name: ("ok", "sensitive-value"))
    assert "sensitive-value" not in json.dumps(report)
    assert all(item["reason"] == "invalid_output" for item in report["observations"]
               if item["target_component"] in {*support.SERVICES, "managed_haos_vm", "product_service_log_severity"})
    assert support._journal('{"PRIORITY":["sensitive-value"]}') is not None
    with pytest.raises(ValueError, match="Unknown support"):
        support.probe("shell")


def test_fixed_commands_and_capture_strip_parent_credentials(monkeypatch):
    monkeypatch.setenv("PROVIDER_SECRET", "sensitive-value")
    reason, output = support._capture((sys.executable, "-c", "import os; print(os.getenv('PROVIDER_SECRET', 'absent'))"))
    assert reason == "ok" and output.strip() == "absent"
    assert "--readonly" in support.COMMANDS["vm"]
    assert "--output-fields=PRIORITY" in support.COMMANDS["journal"]
    assert all(command[0].startswith("/usr/bin/") for command in support.COMMANDS.values())
    assert not any("status" in command for command in support.COMMANDS.values())  # status can include raw logs


def test_probe_output_and_wall_budgets_stop_real_processes(monkeypatch):
    monkeypatch.setattr(support, "MAX_OUTPUT", 1024)
    reason, output = support._capture((sys.executable, "-c", "import os; os.write(1, b'x' * 65536)"))
    assert reason == "output_limit" and output == ""
    monkeypatch.setattr(support, "PROBE_SECONDS", 0.05)
    started = time.monotonic()
    reason, output = support._capture((sys.executable, "-c", "import time; time.sleep(60)"))
    assert reason == "timeout" and output == "" and time.monotonic() - started < 3


def test_support_bundle_is_private_hashed_and_never_overwrites(inputs, tmp_path, monkeypatch):
    root, release, execute, _ = inputs
    report = support.collect(root=root, os_release=release, execute=execute)
    monkeypatch.setattr(support, "collect", lambda: report)
    destination = tmp_path / "support.json"
    digest = support.bundle(destination)
    result = json.loads(destination.read_text())
    payload = json.dumps(result["report"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert result["payload_sha256"] == digest == hashlib.sha256(payload).hexdigest()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    original = destination.read_bytes()
    with pytest.raises(FileExistsError):
        support.bundle(destination)
    link = tmp_path / "link.json"
    link.symlink_to(destination)
    with pytest.raises(FileExistsError):
        support.bundle(link)
    assert destination.read_bytes() == original


def test_owner_rescue_cli_never_calls_controller_or_host_daemon(monkeypatch, capsys, tmp_path):
    def unavailable(*args):
        pytest.fail("diagnostics must work without the Web and the broker")

    monkeypatch.setattr(cli, "call", unavailable)
    monkeypatch.setattr(support, "collect", lambda: {"status": "fixture", "live_effects": []})
    monkeypatch.setattr(sys, "argv", ["agentic-home", "diagnostics"])
    cli.main()
    assert json.loads(capsys.readouterr().out)["status"] == "fixture"
    destination = tmp_path / "support.json"
    monkeypatch.setattr(sys, "argv", ["agentic-home", "support-bundle", "--output", str(destination)])
    cli.main()
    assert json.loads(capsys.readouterr().out)["state"] == "written"
    assert json.loads(destination.read_text())["report"]["status"] == "fixture"


def test_junit_evidence_redacts_machine_name_and_home():
    from conftest import redact_junit
    home = "/home/someone"
    xml = ('<testsuites><testsuite name="pytest" hostname="private-box" tests="1">'
           '<testcase><failure message="/home/someone/agent-work/tmp/x and /home/someone.d and /home/someone2"/>'
           '</testcase></testsuite></testsuites>')
    out = redact_junit(xml, home)
    assert 'hostname="redacted"' in out and "private-box" not in out
    assert "~/agent-work/tmp/x" in out and "/home/someone/" not in out
    # Only the exact home directory is replaced, not a longer sibling name.
    assert "/home/someone.d" in out and "/home/someone2" in out
