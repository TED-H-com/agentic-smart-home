import hashlib
import importlib.util
import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]


def load(path):
    spec = importlib.util.spec_from_file_location("live_preflight_test_module", ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def preflight(monkeypatch):
    module = load("host/live_preflight.py")
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(module, "read_system_file", lambda path, limit: "MemTotal:       8388608 kB\n")
    return module


def test_hardware_check_passes_without_loading_a_module_when_kvm_works(preflight, monkeypatch, capsys):
    monkeypatch.setattr(preflight, "kvm_ready", lambda: True)
    monkeypatch.setattr(preflight.subprocess, "run", lambda *args, **kwargs: pytest.fail("unexpected module load"))
    assert preflight.main(["--minimum-memory-mib", "7680"]) == 0
    output = capsys.readouterr()
    assert "8192 MiB RAM, working KVM" in output.out and output.err == ""


@pytest.mark.parametrize(("architecture", "memory", "reason"), [
    ("aarch64", "MemTotal: 16777216 kB\n", "Intel/AMD x86-64"),
    ("x86_64", "MemTotal: 4194304 kB\n", "4096 MiB"),
    ("x86_64", "MemTotal: unknown\n", "Cannot read installed RAM"),
])
def test_unsupported_hardware_stops_before_kvm_preparation(preflight, monkeypatch, capsys,
                                                         architecture, memory, reason):
    monkeypatch.setattr(preflight.platform, "machine", lambda: architecture)
    monkeypatch.setattr(preflight, "read_system_file", lambda path, limit: memory)
    monkeypatch.setattr(preflight, "prepare_kvm", lambda: pytest.fail("must fail before kernel preparation"))
    assert preflight.main(["--minimum-memory-mib", "7680"]) == 1
    output = capsys.readouterr()
    assert reason in output.err and "No installation disk" in output.err
    assert "restart the USB installer" in output.err and output.out == ""
    assert len(output.err) < 1024


def test_virtualization_failure_has_bios_and_nested_vm_recovery(preflight, monkeypatch, capsys):
    monkeypatch.setattr(preflight, "prepare_kvm", lambda: False)
    assert preflight.main(["--minimum-memory-mib", "7680"]) == 1
    error = capsys.readouterr().err
    assert "VT-x / AMD-V in UEFI/BIOS" in error and "nested virtualization" in error


@pytest.mark.parametrize(("flags", "module"), [("fpu vmx sse", "kvm_intel"), ("fpu svm sse", "kvm_amd")])
def test_live_kernel_loads_only_fixed_vendor_module_and_rechecks_real_api(preflight, monkeypatch, flags, module):
    calls, attempts = [], iter([False, False, True])
    monkeypatch.setattr(preflight, "kvm_ready", lambda: next(attempts))
    monkeypatch.setattr(preflight, "read_system_file", lambda path, limit: "flags\t: " + flags + "\n")
    monkeypatch.setattr(preflight.os, "access", lambda path, mode: path == "/usr/sbin/modprobe")
    monkeypatch.setattr(preflight.time, "sleep", lambda duration: None)

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(preflight.subprocess, "run", run)
    assert preflight.prepare_kvm() is True
    assert calls == [(["/usr/sbin/modprobe", "--", module], {
        "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
        "timeout": 10, "check": False})]


@pytest.mark.parametrize("failure", ["cpu_flags", "missing_modprobe", "module_error", "module_timeout", "not_ready"])
def test_missing_virtualization_fails_with_bounded_preparation(preflight, monkeypatch, failure):
    calls = []
    monkeypatch.setattr(preflight, "kvm_ready", lambda: False)
    monkeypatch.setattr(preflight, "read_system_file", lambda path, limit:
                        "flags: fpu sse\n" if failure == "cpu_flags" else "flags: fpu vmx\n")
    monkeypatch.setattr(preflight.os, "access", lambda path, mode: failure != "missing_modprobe")
    ticks = iter([100.0, 103.0])
    monkeypatch.setattr(preflight.time, "monotonic", lambda: next(ticks))

    def run(command, **kwargs):
        calls.append(command)
        if failure == "module_timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return SimpleNamespace(returncode=1 if failure == "module_error" else 0)

    monkeypatch.setattr(preflight.subprocess, "run", run)
    assert preflight.prepare_kvm() is False
    assert len(calls) == (0 if failure in {"cpu_flags", "missing_modprobe"} else 1)


@pytest.mark.parametrize("failure", [None, "regular_file", "api_version", "create_vm", "open"])
def test_kvm_probe_creates_no_guest_and_closes_every_open_descriptor(preflight, monkeypatch, failure):
    closed, ioctls, opened = [], [], []

    def open_device(path, flags):
        opened.append(path)
        assert flags == os.O_RDWR | os.O_CLOEXEC
        if failure == "open":
            raise PermissionError("fixture")
        return 42

    def ioctl(descriptor, request, argument):
        ioctls.append(request)
        assert descriptor == 42 and argument == 0
        if request == 0xAE00:
            return 99 if failure == "api_version" else 12
        assert request == 0xAE01
        if failure == "create_vm":
            raise OSError("fixture")
        return 43

    monkeypatch.setattr(preflight, "os", SimpleNamespace(
        open=open_device, close=closed.append, O_RDWR=os.O_RDWR, O_CLOEXEC=os.O_CLOEXEC,
        fstat=lambda descriptor: SimpleNamespace(st_mode=stat.S_IFREG if failure == "regular_file" else stat.S_IFCHR)))
    monkeypatch.setattr(preflight.fcntl, "ioctl", ioctl)
    assert preflight.kvm_ready() is (failure is None)
    assert opened == ["/dev/kvm"]
    assert closed == ([] if failure == "open" else [43, 42] if failure is None else [42])
    if failure in {"open", "regular_file"}:
        assert ioctls == []
    elif failure == "api_version":
        assert ioctls == [0xAE00]


@pytest.mark.parametrize("profile_id", ["laptop-amd64", "server-amd64"])
def test_public_and_isolated_installer_keep_same_hardware_gate(profile_id):
    build = load("scripts/build_host.py")
    profile = json.loads((ROOT / "host/profiles" / (profile_id + ".json")).read_text())
    public = build.autoinstall(profile)["autoinstall"]
    fixture = build.autoinstall({"id": profile_id})["autoinstall"]
    assert public == fixture
    assert public["early-commands"] == [["/usr/bin/python3", "/cdrom/agentic-home/live-preflight.py",
                                         "--minimum-memory-mib", str(profile["minimum_host_memory_mib"])]]
    assert public["interactive-sections"] == ["network", "storage", "identity"]
    assert "storage" not in public and "identity" not in public
    assert "sha256sum -c" in public["late-commands"][0]


def test_iso_packages_live_gate_and_includes_it_in_media_integrity(tmp_path, monkeypatch):
    build = load("scripts/build_host.py")
    monkeypatch.setattr(build, "ROOT", tmp_path)
    (tmp_path / "build").mkdir()
    (tmp_path / "host").mkdir()
    (tmp_path / "host/bases.lock.json").write_text(json.dumps({"bases": {"fixture": {}}}))
    helper = tmp_path / "host/live_preflight.py"
    helper.write_bytes((ROOT / "host/live_preflight.py").read_bytes())
    product = tmp_path / "product.tar"
    product.write_bytes(b"synthetic product archive")
    monkeypatch.setattr(build, "fetch", lambda *args: tmp_path / "base.iso")
    checked = []

    def xorriso(*args):
        if "-extract" in args:
            for index, value in enumerate(args):
                if value == "-extract":
                    path = Path(args[index + 2])
                    path.write_text("menuentry 'upstream rescue' {}\n" if path.suffix == ".cfg" else "")
        else:
            mappings = {str(args[index + 2]): Path(args[index + 1])
                        for index, value in enumerate(args) if value == "-map"}
            assert mappings["/agentic-home/live-preflight.py"].read_bytes() == helper.read_bytes()
            sums = mappings["/md5sum.txt"].read_text()
            assert hashlib.md5(helper.read_bytes()).hexdigest() + "  ./agentic-home/live-preflight.py" in sums
            seed = mappings["/agentic-seed"].joinpath("user-data").read_text().split("\n", 1)[1]
            assert json.loads(seed)["autoinstall"]["early-commands"][0][1] == "/cdrom/agentic-home/live-preflight.py"
            assert "upstream rescue" in mappings["/boot/grub/grub.cfg"].read_text()
            Path(args[args.index("-outdev") + 1]).write_bytes(b"synthetic ISO")
            checked.append(True)

    monkeypatch.setattr(build, "xorriso", xorriso)
    destination = tmp_path / "product.iso"
    build.iso({"id": "fixture", "base": "fixture", "minimum_host_memory_mib": 7680}, product, destination)
    assert checked == [True] and destination.read_bytes() == b"synthetic ISO"
    assert product.read_bytes() == b"synthetic product archive"
