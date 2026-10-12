"""Fixed-resource libvirt lifecycle. Only this trusted broker runs as host root."""
import fcntl
import json
import lzma
import os
import re
import shutil
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path

from .artifacts import fetch, sha256

NAME = "agentic-home-haos"
NETWORK = "agentic-home"
NS = "https://agentic-home.local/managed/v1"
IP = "192.168.177.2"
MAC = "52:54:00:aa:17:02"
ET.register_namespace("agentic", NS)


class HostError(Exception):
    pass


def sync_path(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def run(*args, timeout=120, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
    if check and result.returncode:
        raise HostError(f"{args[0]}: {result.stderr.strip()[:500]}")
    return result


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".new")
    with temp.open("w") as stream:
        os.chmod(temp, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    sync_path(path.parent)


def validate_profile(profile, bases):
    required = {"id", "label", "base", "desktop", "memory_mib", "vcpus", "disk_gib", "network", "bridge",
                "appliance_lid", "minimum_host_memory_mib"}
    if not isinstance(profile, dict) or set(profile) != required:
        raise HostError("Profile fields do not match schema 1")
    if profile.get("base") not in bases["bases"]:
        raise HostError("Unknown Linux base")
    if not re.fullmatch(r"[a-z0-9-]{1,64}", profile.get("id", "")):
        raise HostError("Invalid profile id")
    if not isinstance(profile["label"], str) or not 1 <= len(profile["label"]) <= 100:
        raise HostError("Invalid profile label")
    if any(type(profile[name]) is not bool for name in ("desktop", "appliance_lid")):
        raise HostError("Invalid profile flags")
    if type(profile["minimum_host_memory_mib"]) is not int or profile["minimum_host_memory_mib"] < profile["memory_mib"] + 2048:
        raise HostError("Reserve at least 2 GiB for the outer Linux")
    for name, minimum, maximum in (("memory_mib", 2048, 65536), ("vcpus", 2, 32), ("disk_gib", 32, 2048)):
        if type(profile.get(name)) is not int or not minimum <= profile[name] <= maximum:
            raise HostError("Invalid profile " + name)
    if profile.get("network") != "nat" or profile.get("bridge") is not None:
        raise HostError("This release provisions the dedicated NAT network only")
    return profile


def domain_xml(profile, disk, nvram, owner, firmware="/usr/share/OVMF/OVMF_CODE_4M.fd"):
    domain = ET.Element("domain", {"type": "kvm"})
    ET.SubElement(domain, "name").text = NAME
    ET.SubElement(domain, "uuid").text = owner
    metadata = ET.SubElement(domain, "metadata")
    ET.SubElement(metadata, f"{{{NS}}}owner").text = owner
    ET.SubElement(domain, "memory", {"unit": "MiB"}).text = str(profile["memory_mib"])
    ET.SubElement(domain, "vcpu").text = str(profile["vcpus"])
    os_node = ET.SubElement(domain, "os")
    ET.SubElement(os_node, "type", {"arch": "x86_64", "machine": "q35"}).text = "hvm"
    ET.SubElement(os_node, "loader", {"readonly": "yes", "type": "pflash"}).text = firmware
    ET.SubElement(os_node, "nvram").text = str(nvram)
    ET.SubElement(os_node, "boot", {"dev": "hd"})
    features = ET.SubElement(domain, "features")
    ET.SubElement(features, "acpi")
    ET.SubElement(features, "apic")
    ET.SubElement(domain, "cpu", {"mode": "host-passthrough"})
    ET.SubElement(domain, "on_poweroff").text = "destroy"
    ET.SubElement(domain, "on_reboot").text = "restart"
    ET.SubElement(domain, "on_crash").text = "preserve"
    devices = ET.SubElement(domain, "devices")
    drive = ET.SubElement(devices, "disk", {"type": "file", "device": "disk"})
    ET.SubElement(drive, "driver", {"name": "qemu", "type": "qcow2", "discard": "unmap"})
    ET.SubElement(drive, "source", {"file": str(disk)})
    ET.SubElement(drive, "target", {"dev": "sda", "bus": "scsi"})
    ET.SubElement(devices, "controller", {"type": "scsi", "model": "virtio-scsi"})
    interface = ET.SubElement(devices, "interface", {"type": "network"})
    ET.SubElement(interface, "source", {"network": NETWORK})
    ET.SubElement(interface, "mac", {"address": MAC})
    ET.SubElement(interface, "model", {"type": "virtio"})
    channel = ET.SubElement(devices, "channel", {"type": "unix"})
    ET.SubElement(channel, "target", {"type": "virtio", "name": "org.qemu.guest_agent.0"})
    serial = ET.SubElement(devices, "serial", {"type": "pty"})
    ET.SubElement(serial, "target", {"port": "0"})
    console = ET.SubElement(devices, "console", {"type": "pty"})
    ET.SubElement(console, "target", {"type": "serial", "port": "0"})
    rng = ET.SubElement(devices, "rng", {"model": "virtio"})
    ET.SubElement(rng, "backend", {"model": "random"}).text = "/dev/urandom"
    return ET.tostring(domain, encoding="unicode")


def network_xml(owner):
    return (f'<network><name>{NETWORK}</name><metadata><agentic:owner xmlns:agentic="{NS}">'
            f'{owner}</agentic:owner></metadata><forward mode="nat"/><bridge name="virbr-ah" stp="on" delay="0"/>'
            f'<ip address="192.168.177.1" netmask="255.255.255.0"><dhcp><host mac="{MAC}" '
            f'name="agentic-haos" ip="{IP}"/></dhcp></ip></network>')


class Manager:
    def __init__(self, root="/var/lib/agentic-home/host", product="/opt/agentic-home",
                 images="/var/lib/libvirt/images/agentic-home", execute=run):
        self.root, self.product, self.images = Path(root), Path(product), Path(images)
        self.run = execute
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_file = self.root / "state.json"

    def state(self):
        return json.loads(self.state_file.read_text()) if self.state_file.exists() else {}

    def virsh(self, *args, **kwargs):
        return self.run("virsh", "--connect", "qemu:///system", *args, **kwargs)

    def compatibility(self):
        # GET observes only the last attempted mutation's bounded report.
        from .support import _object
        value = _object(self.root / "compatibility.json")
        return value if value and value.get("scope") == "required_fixed_host_contracts" else {
            "state": "not_checked", "action": None, "observed_at": None, "checks": [],
            "scope": "required_fixed_host_contracts", "reuse_for_mutations": False}

    def require_compatibility(self, action):
        from .compatibility import inspect
        report = inspect(self.run, action)
        atomic(self.root / "compatibility.json", report)
        if report["state"] != "supported":
            first = next(check["id"] for check in report["checks"] if check["state"] == "blocked")
            raise HostError("host_contract_" + first + "_unavailable")
        return report

    @contextmanager
    def locked(self):
        with (self.root / "operation.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise HostError("Another host operation is running") from exc
            yield

    def owned(self):
        state = self.state()
        if not state.get("owner"):
            raise HostError("HAOS has not been provisioned by this product")
        result = self.virsh("dumpxml", NAME, "--inactive", check=False)
        if result.returncode:
            return None
        xml = ET.fromstring(result.stdout)
        if xml.findtext(f"metadata/{{{NS}}}owner") != state["owner"] or xml.findtext("uuid") != state["owner"]:
            raise HostError("Existing VM is not owned by Agentic Home")
        disks = xml.findall("devices/disk/source")
        if len(disks) != 1 or disks[0].get("file") != str(self.images / "haos.qcow2"):
            raise HostError("VM disk was changed outside Agentic Home")
        return xml

    def status(self):
        from .bootstrap import InitialSetup
        from .updater import INGRESS_BRIDGE_CONTRACT

        state = self.state()
        try:
            result = self.virsh("domstate", NAME, check=False, timeout=3)
            vm = result.stdout.strip() if result.returncode == 0 else "not_created"
            if vm not in {"running", "blocked", "paused", "in shutdown", "shut off", "crashed", "pmsuspended", "not_created"}:
                vm = "unknown"
        except (OSError, HostError, subprocess.TimeoutExpired):
            vm = "unknown"
        snapshots = []
        for path in sorted((self.root / "snapshots").glob("*/manifest.json"), reverse=True):
            value = json.loads(path.read_text())
            snapshots.append({"id": path.parent.name, "created_at": value["created_at"]})
        return {"vm": vm, "profile": state.get("profile"), "ha_url": "http://localhost:8123",
                "ha_internal_url": f"http://{IP}:80", "network": "nat", "snapshots": snapshots,
                "kvm": os.access("/dev/kvm", os.R_OK | os.W_OK),
                "storage_free_bytes": shutil.disk_usage(self.root).free,
                "provisioned": bool(state.get("complete")), "restore_pending": state.get("restore_pending"),
                "bootstrap": InitialSetup(self).status(), "compatibility": self.compatibility(),
                "controller_update": {"ingress_bridge_contract": INGRESS_BRIDGE_CONTRACT}}

    def refresh_forwarding(self):
        """Restore scoped forwarding after Linux/Docker restarts, only for our network."""
        owner = self.state().get("owner")
        if not owner:
            return
        network = self.virsh("net-dumpxml", NETWORK, check=False)
        if network.returncode or ET.fromstring(network.stdout).findtext(f"metadata/{{{NS}}}owner") != owner:
            return
        if self.run("iptables", "-n", "-L", "DOCKER-USER", check=False).returncode == 0:
            for rule in (("-i", "virbr-ah", "-j", "ACCEPT"),
                         ("-o", "virbr-ah", "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT")):
                if self.run("iptables", "-C", "DOCKER-USER", *rule, check=False).returncode:
                    self.run("iptables", "-I", "DOCKER-USER", "1", *rule)

    def provision(self):
        profile = json.loads(Path("/etc/agentic-home/profile.json").read_text())
        bases = json.loads((self.product / "host/bases.lock.json").read_text())
        validate_profile(profile, bases)
        if not os.access("/dev/kvm", os.R_OK | os.W_OK):
            raise HostError("KVM unavailable; enable Intel VT-x / AMD-V in firmware")
        self.require_compatibility("provision")
        state = self.state()
        if not state:
            if self.virsh("dominfo", NAME, check=False).returncode == 0 or self.images.exists():
                raise HostError("Existing VM or image directory has no ownership record; refusing to replace")
            # No global networking changes: reserve a new, non-overlapping product subnet.
            from .compatibility import output, route_overlap, routes
            try:
                overlap = route_overlap(routes(output(self.run, "ip", "-json", "route", "show", "table", "all")))
            except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
                raise HostError("host_contract_route_json_unavailable") from None
            if overlap:
                raise HostError("192.168.177.0/24 overlaps an existing route")
            state = {"owner": str(uuid.uuid4()), "profile": profile, "complete": False}
            atomic(self.state_file, state)
        elif state["profile"] != profile:
            raise HostError("Installed profile differs from VM ownership record; no implicit replacement")
        if state.get("restore_pending"):
            raise HostError("Finish the recorded restore before starting the VM")
        self.images.mkdir(parents=True, exist_ok=True)
        artifact = fetch(bases["haos"], self.product / "assets")
        disk = self.images / "haos.qcow2"
        if not disk.exists():
            temporary = disk.with_suffix(".partial")
            try:
                with lzma.open(artifact, "rb") as source, temporary.open("wb") as target:
                    shutil.copyfileobj(source, target, 1024 * 1024)
                from .compatibility import check_image
                try:
                    check_image(self.run, temporary)
                except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
                    raise HostError("host_contract_qemu_image_unavailable") from None
                self.run("qemu-img", "check", str(temporary), timeout=300)
                self.run("qemu-img", "resize", str(temporary), str(profile["disk_gib"]) + "G")
                sync_path(temporary)
                os.replace(temporary, disk)
                sync_path(disk.parent)
            finally:
                temporary.unlink(missing_ok=True)
        nvram = self.images / "nvram.fd"
        if not nvram.exists():
            shutil.copyfile("/usr/share/OVMF/OVMF_VARS_4M.fd", nvram)
        self.run("chown", "libvirt-qemu:kvm", str(self.images), str(disk), str(nvram))
        self.run("chmod", "0750", str(self.images))
        self.run("chmod", "0600", str(disk), str(nvram))
        network = self.virsh("net-dumpxml", NETWORK, check=False)
        if network.returncode == 0:
            if ET.fromstring(network.stdout).findtext(f"metadata/{{{NS}}}owner") != state["owner"]:
                raise HostError("Existing network is not owned by Agentic Home")
        else:
            xml = self.root / "network.xml"
            xml.write_text(network_xml(state["owner"]))
            self.virsh("net-define", str(xml))
        if not re.search(r"^Active:\s+yes$", self.virsh("net-info", NETWORK).stdout, re.MULTILINE):
            self.virsh("net-start", NETWORK)
        self.virsh("net-autostart", NETWORK)
        # Docker's FORWARD policy must not block libvirt's dedicated NAT bridge.
        # Scope exceptions to this product bridge; leave all other host networking intact.
        self.refresh_forwarding()
        if self.owned() is None:
            xml = self.root / "domain.xml"
            xml.write_text(domain_xml(profile, disk, nvram, state["owner"]))
            self.virsh("define", str(xml))
        self.virsh("autostart", NAME)
        self.start()
        state["complete"] = True
        atomic(self.state_file, state)
        return self.status()

    def start(self):
        if self.state().get("restore_pending"):
            raise HostError("Interrupted restore: re-run restore with the recorded snapshot id")
        if self.owned() is None:
            raise HostError("VM definition is missing; run provision")
        self.require_compatibility("start")
        state = self.virsh("domstate", NAME).stdout.strip()
        if state == "shut off":
            self.virsh("start", NAME)
        elif state != "running":
            raise HostError("VM is " + state + "; inspect it before changing state")
        return {"vm": "running"}

    def shutdown(self):
        if self.owned() is None:
            raise HostError("VM definition is missing")
        self.require_compatibility("shutdown")
        state = self.virsh("domstate", NAME).stdout.strip()
        if state == "shut off":
            return {"vm": state}
        if state != "running":
            raise HostError("VM is " + state)
        self.virsh("shutdown", NAME, "--mode", "acpi")
        for _ in range(180):
            if self.virsh("domstate", NAME).stdout.strip() == "shut off":
                return {"vm": "shut off"}
            time.sleep(1)
        raise HostError("HAOS has not shut down; no forced power-off was performed")

    def snapshot(self):
        self.owned()
        self.require_compatibility("snapshot")
        was_running = self.virsh("domstate", NAME).stdout.strip() == "running"
        self.shutdown()
        id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
        destination = self.root / "snapshots" / id
        destination.mkdir(parents=True, mode=0o700)
        try:
            entries = {}
            for name in ("haos.qcow2", "nvram.fd"):
                self.run("cp", "--reflink=auto", "--sparse=always", "--", str(self.images / name),
                         str(destination / name), timeout=1800)
                sync_path(destination / name)
                entries[name] = sha256(destination / name)
            atomic(destination / "manifest.json", {"owner": self.state()["owner"], "files": entries,
                                                   "created_at": time.time()})
        finally:
            if was_running:
                self.start()
        return {"snapshot": id, "scope": "HAOS disk and UEFI; outer Agent data backed up separately"}

    def restore(self, id):
        if not isinstance(id, str) or not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}", id):
            raise HostError("Invalid snapshot id")
        self.owned()
        self.require_compatibility("restore")
        if self.virsh("domstate", NAME).stdout.strip() != "shut off":
            raise HostError("Shut down HAOS before restoring")
        source = self.root / "snapshots" / id
        manifest = json.loads((source / "manifest.json").read_text())
        if manifest["owner"] != self.state()["owner"] or set(manifest["files"]) != {"haos.qcow2", "nvram.fd"}:
            raise HostError("Snapshot ownership mismatch")
        for name, hash_value in manifest["files"].items():
            if sha256(source / name) != hash_value:
                raise HostError("Snapshot checksum mismatch")
        state = self.state()
        if state.get("restore_pending") not in {None, id}:
            raise HostError("A different restore must be completed first")
        state["restore_pending"] = id
        self.virsh("autostart", NAME, "--disable")
        atomic(self.state_file, state)
        for name in manifest["files"]:
            temp = self.images / (name + ".restore")
            self.run("cp", "--reflink=auto", "--sparse=always", "--", str(source / name), str(temp), timeout=1800)
            self.run("chown", "libvirt-qemu:kvm", str(temp))
            self.run("chmod", "0600", str(temp))
            sync_path(temp)
            os.replace(temp, self.images / name)
            sync_path(self.images)
        state.pop("restore_pending", None)
        atomic(self.state_file, state)
        self.virsh("autostart", NAME)
        return {"restored": id, "vm": "shut off"}

    def action(self, action, snapshot=None):
        if action not in {"provision", "start", "shutdown", "snapshot", "restore", "bootstrap_hermes"}:
            raise HostError("Unknown lifecycle operation")
        with self.locked():
            if action == "bootstrap_hermes":
                from .bootstrap import InitialSetup
                return InitialSetup(self).install()
            return self.restore(snapshot) if action == "restore" else getattr(self, action)()
