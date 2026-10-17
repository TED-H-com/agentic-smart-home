"""Opt-in real libvirt parser/test driver; never the development system daemon."""
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "host"))
from agentic_host.ha_lan import network_mode_xml, validate_network  # noqa: E402
from agentic_host.manager import network_xml  # noqa: E402

OWNER = "3d58ff36-88e0-45b2-bb6d-d9aa718ad479"
NETWORK_UUID = "7857bb95-bd31-4e64-86f4-c42e509fb235"
NETWORK_MAC = "52:54:00:bb:17:01"


@pytest.fixture
def builder_image(request):
    if not request.config.getoption("--containers"):
        pytest.skip("Use --containers for isolated real libvirt test-driver coverage")
    try:
        result = subprocess.run(["docker", "image", "inspect", "agentic-home-builder:0.2.0", "--format", "{{.Id}}"],
                                capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        pytest.skip("Docker CLI unavailable; isolated libvirt coverage not run")
    if result.returncode != 0 and re.search(r"No such (?:image|object)", result.stderr, re.I):
        pytest.skip("Local libvirt builder image unavailable; this test never pulls or builds images")
    assert result.returncode == 0, "Cannot inspect the local libvirt test image"
    image = result.stdout.strip()
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", image)
    return image


def run_test_driver(tmp_path, commands, image):
    name = "agentic-lan-lab-smoke-libvirt-" + uuid.uuid4().hex[:10]
    try:
        return subprocess.run(["docker", "run", "--rm", "--pull=never", "-i", "--name", name, "--network", "none",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "64", "--memory", "256m", "--memory-swap", "256m", "--cpus", "1",
            "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/lab", "-v", f"{tmp_path}:/lab",
            image, "virsh", "--connect", "test:///default"],
            input="\n".join(commands) + "\n", capture_output=True, text=True, timeout=30)
    finally:
        subprocess.run(["docker", "rm", "-fv", name], capture_output=True, timeout=20)
        remaining = subprocess.run(["docker", "ps", "-a", "--filter", "name=^/" + name + "$",
                                    "--format", "{{.Names}}"], capture_output=True, text=True, check=True, timeout=10)
        assert not remaining.stdout.strip()


def test_real_libvirt_rejects_uuidless_redefinition_of_existing_network(tmp_path, builder_image):
    (tmp_path / "initial.xml").write_text(network_xml(OWNER))
    (tmp_path / "uuidless.xml").write_text(network_xml(OWNER, wired=True))
    result = run_test_driver(tmp_path, ["help net-port-list", "net-define /lab/initial.xml", "net-start agentic-home",
        "net-destroy agentic-home", "net-define /lab/uuidless.xml"], builder_image)
    # Interactive virsh reports command errors on stderr but exits zero at EOF.
    assert result.returncode == 0
    assert "already exists with uuid" in result.stderr, result.stderr
    assert "--uuid" in result.stdout
    assert "unsupported flags" not in result.stderr and "not supported" not in result.stderr
    print("Real isolated libvirt rejected changed network UUID: " + result.stderr.strip())


def test_real_libvirt_preserves_network_identity_through_enable_retry_and_disable(tmp_path, builder_image):
    initial = ET.fromstring(network_xml(OWNER))
    ET.SubElement(initial, "uuid").text = NETWORK_UUID
    ET.SubElement(initial, "mac", {"address": NETWORK_MAC})
    validate_network(initial, OWNER, {False})
    wired_xml = network_mode_xml(initial, wired=True)
    wired = ET.fromstring(wired_xml)
    validate_network(wired, OWNER, {True})
    disabled_xml = network_mode_xml(wired, wired=False)
    validate_network(ET.fromstring(disabled_xml), OWNER, {False})
    (tmp_path / "initial.xml").write_text(ET.tostring(initial, encoding="unicode"))
    (tmp_path / "wired.xml").write_text(wired_xml)
    (tmp_path / "disabled.xml").write_text(disabled_xml)
    # test:///default lacks the inactive XML flag and live-port API. Read the
    # actual inactive definition while stopped instead; never mask driver errors.
    commands = ["net-define /lab/initial.xml", "net-dumpxml agentic-home", "net-start agentic-home"]
    for filename in ("wired.xml", "wired.xml", "disabled.xml"):
        commands += ["net-destroy agentic-home", "net-define /lab/" + filename,
                     "net-dumpxml agentic-home", "net-start agentic-home"]
    result = run_test_driver(tmp_path, commands, builder_image)
    assert result.returncode == 0 and not result.stderr.strip(), result.stderr
    networks = [ET.fromstring(xml) for xml in re.findall(r"<network(?:\s[^>]*)?>.*?</network>", result.stdout, re.S)]
    assert len(networks) == 4
    for network in networks:
        assert network.findtext("uuid") == NETWORK_UUID != OWNER
        assert network.find("mac").attrib == {"address": NETWORK_MAC}
        assert network.find("ip/dhcp/host").attrib == initial.find("ip/dhcp/host").attrib
    # The in-memory test driver checks libvirt persistence and XML parsing; it
    # does not run dnsmasq, DHCP, macvtap, HAOS or any host network interface.
