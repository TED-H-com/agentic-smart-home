"""Fake-only cleanup and firstboot failure checks; never start a VM or container."""
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_linux_install as install

NAME = "agentic-linux-install-test-fixture"


@pytest.mark.parametrize("fault", ["stop", "remove", "absence", "console"])
def test_cleanup_failure_is_bounded_and_does_not_skip_remaining_steps(tmp_path, fault):
    (tmp_path / "serial.log").write_text("private console output")
    calls = []

    def run(args, **options):
        calls.append(args)
        assert 0 < options["timeout"] <= 60
        assert options["stderr"] == subprocess.DEVNULL
        selected = (args[1] == "stop" and fault == "stop"
                    or args[1] == "rm" and args[-1] == NAME and fault == "remove"
                    or args[1] == "ps" and fault == "absence" or args[0] == "tail" and fault == "console")
        if selected:
            raise subprocess.TimeoutExpired(args, options["timeout"], output=b"private output", stderr=b"private error")
        return subprocess.CompletedProcess(args, 0, b"", b"private output")

    result = install.cleanup_installer(NAME, tmp_path, tmp_path / "private-console.log", run=run)
    assert [args[1] for args in calls] == ["stop", "-c", "rm", "ps"]
    assert result["status"] == ("failed" if fault == "absence" else "passed")
    assert any(step["status"] == "timeout" for step in result["steps"].values())
    serialized = json.dumps(result)
    assert "private" not in serialized and str(tmp_path) not in serialized and NAME not in serialized
    assert "stdout" not in serialized and "stderr" not in serialized
    assert (tmp_path / "private-console.log").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("failure", ["present", "daemon_error"])
def test_successful_removal_command_is_not_itself_proof_of_absence(tmp_path, failure):
    def run(args, **options):
        if args[1] == "ps":
            return subprocess.CompletedProcess(args, int(failure == "daemon_error"), b"owned-container\n", b"private")
        return subprocess.CompletedProcess(args, 0, b"", b"")
    result = install.cleanup_installer(NAME, tmp_path, tmp_path / "console.log", run=run)
    assert result["status"] == "failed"
    assert result["steps"]["vm_absent"]["status"] == "failed"


def test_private_console_copy_is_a_bounded_tail(tmp_path):
    source = b"earlier-private-data\n" * 10000 + b"newest-console-data\n"
    (tmp_path / "serial.log").write_bytes(source)
    calls = []
    def run(args, **options):
        calls.append(args)
        if args[0] == "tail":
            assert options["timeout"] == 10 and args[1:4] == ["-c", "65536", "--"]
            # Simulate the fixed utility's output without invoking a subprocess.
            options["stdout"].write(source[-int(args[2]):])
        return subprocess.CompletedProcess(args, 0, b"", b"")
    console = tmp_path / "console.log"
    result = install.cleanup_installer(NAME, tmp_path, console, run=run)
    assert result["status"] == "passed"
    assert console.read_bytes() == source[-install.INSTALL_CONSOLE_BYTES:]
    assert console.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_unsafe_console_destination_cannot_block_remaining_cleanup(tmp_path, kind):
    (tmp_path / "serial.log").write_text("private console output")
    console = tmp_path / "console.log"
    original = tmp_path / "untouched.txt"
    original.write_text("untouched")
    if kind == "symlink":
        console.symlink_to(original)
    else:
        os.mkfifo(console)
    calls = []
    def run(args, **options):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, b"", b"")
    result = install.cleanup_installer(NAME, tmp_path, console, run=run)
    assert [args[1] for args in calls] == ["stop", "rm", "ps"]
    assert result["status"] == "passed" and result["steps"]["console_tail"]["status"] == "failed"
    assert original.read_text() == "untouched"


@pytest.mark.parametrize("has_primary", [False, True])
@pytest.mark.parametrize("fault", ["cleanup", "receipt", "both"])
def test_cleanup_and_receipt_failure_preserve_primary_or_fail_success(tmp_path, monkeypatch, has_primary, fault):
    primary = AssertionError("original private error") if has_primary else None
    calls = []
    def run(args, **options):
        calls.append(args)
        if args[1] == "ps" and fault in {"cleanup", "both"}:
            raise OSError("private observation error")
        return subprocess.CompletedProcess(args, 0, b"", b"")
    report = tmp_path / "cleanup.json"
    if fault in {"receipt", "both"}:
        monkeypatch.setattr(Path, "write_text", lambda *_a, **_k: (_ for _ in ()).throw(OSError("private write error")))
    if has_primary:
        with pytest.raises(AssertionError) as caught:
            try:
                raise primary
            finally:
                result = install.finish_installer_cleanup(NAME, tmp_path, tmp_path / "console.log", report,
                                                          primary_error=primary, run=run)
                assert result["status"] == "failed"
                if fault in {"receipt", "both"}:
                    assert result["receipt"] == "write_failed"
        assert caught.value is primary
    else:
        with pytest.raises(AssertionError, match="cleanup incomplete"):
            install.finish_installer_cleanup(NAME, tmp_path, tmp_path / "console.log", report, run=run)
    assert [args[1] for args in calls] == ["stop", "rm", "ps"]


def test_firstboot_deadline_reaches_bounded_diagnostics_without_raw_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "ROOT", tmp_path)
    for name in ("dist", "scripts", "evidence", "build", "tests"):
        (tmp_path / name).mkdir()
    (tmp_path / "dist/agentic-home-0.10.0-laptop-amd64.iso").write_bytes(b"fixture")
    (tmp_path / "scripts/build_host.py").write_text(
        "def autoinstall(profile): return {'autoinstall': {'early-commands': [['python3', 'fixture-gate']], 'late-commands': [], 'interactive-sections': []}}\n")
    (tmp_path / "tests/ha_lan_peer.py").write_text("# fixture peer is never executed\n")
    def command(*args, **options):
        if args[0] == "ssh-keygen":
            (tmp_path / "test-key.pub").write_text("fixture-public-key")
        return ""
    deadlines, calls, original = [], [], []
    def wait(ready, seconds):
        deadlines.append(seconds)
        return seconds == 1800
    def diagnostics(error, phase, report, ssh):
        original.append(error)
        assert phase == "installed_firstboot" and ssh[0] == "ssh"
        raise OSError("private diagnostic error")
    def run(args, **options):
        assert args[0] != "ssh", "legacy raw journal call must not run"
        calls.append(args)
        assert "timeout" in options
        return subprocess.CompletedProcess(args, 0, b"", b"")
    monkeypatch.setattr(install, "command", command)
    monkeypatch.setattr(install, "wait_until", wait)
    monkeypatch.setattr(install, "record_install_failure", diagnostics)
    monkeypatch.setattr(install.subprocess, "run", run)
    request = SimpleNamespace(config=SimpleNamespace(getoption=lambda _: True))
    with pytest.raises(AssertionError, match="Firstboot did not complete within 60 minutes") as caught:
        install.test_full_linux_install_with_nested_haos(tmp_path, request)
    assert original == [caught.value] and deadlines == [1800, 3600]
    assert [args[1] for args in calls] == ["stop", "rm", "ps"]
    assert json.loads((tmp_path / "evidence/installed-cleanup-laptop-amd64.json").read_text())["status"] == "passed"
