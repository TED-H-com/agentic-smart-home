"""Persist appliance networking without applying/restarting a live connection.

Choose the renderer before desktop packages can change netplan's default. Existing
NetworkManager installations keep their renderer and DHCP identity. New networkd
installs keep its original RFC4361/DUID identity (not a new MAC DHCP client).
Only trusted host installation/startup calls this module; Controller-only updates
cannot install it. No operation here reconnects a device or applies netplan.
"""
import configparser
import copy
import json
import os
import re
import subprocess
from pathlib import Path

from .manager import HostError, atomic, run

OVERLAY = "99-agentic-home-appliance.yaml"
POLICY = "var/lib/agentic-home/host/appliance-network-policy.json"
LINK = "[Link]\nWakeOnLan=magic\n"
NM_DEFAULT = "[connection-agentic-home-wol]\nmatch-device=type:ethernet\nethernet.wake-on-lan=magic\n"
SECTIONS = ("ethernets", "wifis", "bridges", "bonds", "vlans", "tunnels", "dummy-devices", "virtual-ethernets", "vrfs")


def _yaml():
    # Ubuntu netplan depends on python3-yaml; keep import usable by staged media.
    import yaml
    return yaml


def _merge(left, right):
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(left.get(key), dict):
            _merge(left[key], value)
        elif isinstance(value, list) and isinstance(left.get(key), list):
            left[key] += [item for item in value if item not in left[key]]
        else:
            left[key] = copy.deepcopy(value)
    return left


def netplan_config(root=Path("/"), *, include_managed=False):
    """Netplan's lexical ordering and same-name /run > /etc > /lib shadowing."""
    root = Path(root)
    sources = {}
    for directory in ("lib/netplan", "usr/lib/netplan", "etc/netplan", "run/netplan"):
        for path in (root / directory).glob("*.yaml"):
            if include_managed or path.name != OVERLAY:
                sources[path.name] = path
    merged = {}
    for name in sorted(sources):
        try:
            value = _yaml().safe_load(sources[name].read_text()) or {}
        except (_yaml().YAMLError, OSError) as exc:
            raise HostError("Invalid netplan configuration") from exc
        if not isinstance(value, dict) or not isinstance(value.get("network", {}), dict):
            raise HostError("Invalid netplan configuration")
        _merge(merged, value)
    network = merged.get("network", {})
    for section in SECTIONS:
        if not isinstance(network.get(section, {}), dict) or any(
                not isinstance(settings, dict) for settings in network.get(section, {}).values()):
            raise HostError("Invalid netplan device configuration")
    return network


def physical_ethernet(root=Path("/")):
    result = []
    for path in sorted((Path(root) / "sys/class/net").glob("*")):
        if not (path / "device").exists() or (path / "wireless").exists():
            continue
        if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,15}", path.name):
            continue
        try:
            if (path / "type").read_text().strip() != "1":
                continue
            mac = (path / "address").read_text().strip().lower()
        except OSError:
            continue
        if re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
            result.append({"name": path.name, "mac": mac})
    return result


def _active_networkd(root):
    # Read current networkd link state, not the *next boot* vendor YAML. This
    # matters on legacy firstboot after desktop apt added NM's renderer default.
    for interface in (root / "sys/class/net").glob("*"):
        if not (interface / "device").exists():
            continue
        try:
            index = (interface / "ifindex").read_text().strip()
            if not index.isdecimal():
                continue
            state = (root / "run/systemd/netif/links" / index).read_text()
        except OSError:
            continue
        values = dict(line.split("=", 1) for line in state.splitlines() if "=" in line)
        if (values.get("ADMIN_STATE") == "configured" and
                values.get("NETWORK_FILE", "").startswith("/run/systemd/network/10-netplan-")):
            return True
    return False


def _nm_identifiers(root):
    identities = {}
    for directory in ("etc/NetworkManager/system-connections", "run/NetworkManager/system-connections"):
        for path in sorted((root / directory).glob("*")):
            if not path.is_file():
                continue
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                parser.read_string(path.read_text())
                interface = parser.get("connection", "interface-name", fallback="")
                identity = parser.get("ipv4", "dhcp-client-id", fallback="")
                if interface and identity:
                    identities[interface] = identity
            except (OSError, configparser.Error, UnicodeError):
                continue
    return identities


def network_overlay(config, renderer, identities=None):
    """Only overlay policy keys, leaving addresses, Wi-Fi secrets and routes intact."""
    if renderer not in {"networkd", "NetworkManager"}:
        raise HostError("Unsupported appliance network renderer")
    identities = identities or {}
    result = {"version": 2, "renderer": renderer}
    for section in SECTIONS:
        for name, settings in config.get(section, {}).items():
            if not isinstance(settings, dict):
                raise HostError("Invalid netplan device configuration")
            device = {"renderer": renderer}
            if section == "ethernets":
                device["wakeonlan"] = True
                if renderer == "NetworkManager":
                    # Netplan 1.0 translates wakeonlan:true to NM DEFAULT (1),
                    # which may still disable WoL. Pin magic (64) explicitly.
                    device["networkmanager"] = {"passthrough": {"ethernet.wake-on-lan": "64"}}
            if renderer == "networkd" and settings.get("dhcp4"):
                identity = settings.get("dhcp-identifier", "duid")
                if identity not in {"mac", "duid"}:
                    raise HostError("Unsupported DHCP identifier")
                device["dhcp-identifier"] = identity
            elif renderer == "NetworkManager" and settings.get("dhcp4"):
                passthrough = settings.get("networkmanager", {}).get("passthrough", {})
                interface = settings.get("set-name", name)
                # Preserve explicitly configured arbitrary client IDs. Netplan NM's
                # original default is MAC; pin it rather than inheriting new defaults.
                identity = passthrough.get("ipv4.dhcp-client-id", identities.get(interface, "mac"))
                device.setdefault("networkmanager", {}).setdefault("passthrough", {})["ipv4.dhcp-client-id"] = identity
            result.setdefault(section, {})[name] = device
    return {"network": result}


def _write(path, value, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_text() == value and (path.stat().st_mode & 0o777) == mode:
        return False
    temporary = path.with_name(path.name + ".new")
    with temporary.open("w") as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return True


def _safe_run(runner, *args):
    try:
        return runner(*args, check=False, timeout=10)
    except (OSError, HostError, subprocess.SubprocessError):
        return None


def reconcile_network(root=Path("/"), runner=run, runtime=True):
    """Idempotent host policy; runtime=False only stages files beneath root.

    A pending renderer change waits for reboot. Runtime changes are limited to
    enabling magic-packet WoL and saving that property on active NM connections.
    No netplan apply, device reapply, service restart or connection activation.
    """
    root = Path(root)
    config = netplan_config(root)
    policy_path = root / POLICY
    try:
        previous = json.loads(policy_path.read_text()) if policy_path.exists() else {}
    except (ValueError, OSError) as exc:
        raise HostError("Invalid persisted appliance network policy") from exc
    if not isinstance(previous, dict) or (previous and
            (previous.get("version") != 1 or previous.get("renderer") not in {"networkd", "NetworkManager"} or
             not isinstance(previous.get("overlay"), dict))):
        raise HostError("Invalid persisted appliance network policy")
    boot_path = root / "proc/sys/kernel/random/boot_id"
    boot_id = boot_path.read_text().strip() if boot_path.exists() else None
    renderer = previous.get("renderer") or ("networkd" if _active_networkd(root) else config.get("renderer"))
    if not renderer:
        renderers = {settings.get("renderer") for section in SECTIONS
                     for settings in config.get(section, {}).values() if settings.get("renderer")}
        if len(renderers) > 1:
            raise HostError("Mixed network renderers need explicit migration review")
        renderer = next(iter(renderers), "networkd")
    identities = _nm_identifiers(root)
    overlay = network_overlay(config, renderer, identities)
    # Reuse pinned identities on restart even if NM-generated files disappear.
    if previous.get("overlay"):
        for section, devices in previous["overlay"].get("network", {}).items():
            if section in SECTIONS:
                for name, settings in devices.items():
                    if name in overlay["network"].get(section, {}):
                        device = overlay["network"][section][name]
                        if "dhcp-identifier" in settings:
                            device["dhcp-identifier"] = settings["dhcp-identifier"]
                        client_id = settings.get("networkmanager", {}).get("passthrough", {}).get("ipv4.dhcp-client-id")
                        if client_id is not None:
                            device.setdefault("networkmanager", {}).setdefault("passthrough", {})["ipv4.dhcp-client-id"] = client_id
    policy = {"version": 1, "renderer": renderer, "overlay": overlay}
    managed_paths = [root / "etc/netplan" / OVERLAY,
                     root / "etc/systemd/network/99-default.link.d/60-agentic-home-wol.conf",
                     root / "etc/NetworkManager/conf.d/90-agentic-home-wol.conf", policy_path]
    originals = {path: (path.read_text(), path.stat().st_mode & 0o777) if path.exists() else None
                 for path in managed_paths}
    changed = _write(root / "etc/netplan" / OVERLAY,
                     "# Managed appliance policy; does not change addresses or routes.\n" +
                     _yaml().safe_dump(overlay, sort_keys=False))
    changed |= _write(root / "etc/systemd/network/99-default.link.d/60-agentic-home-wol.conf", LINK)
    changed |= _write(root / "etc/NetworkManager/conf.d/90-agentic-home-wol.conf", NM_DEFAULT)
    policy["configured_boot_id"] = previous.get("configured_boot_id")
    if changed or any(previous.get(key) != policy[key] for key in ("version", "renderer", "overlay")):
        policy["configured_boot_id"] = boot_id
    elif policy["configured_boot_id"] is None and boot_id:
        # An offline-staged policy gets a conservative first-boot marker.
        policy["configured_boot_id"] = boot_id
    if previous != policy:
        atomic(policy_path, policy)
        changed = True
    pending = policy["configured_boot_id"] is None or policy["configured_boot_id"] == boot_id
    report = {"renderer": renderer, "changed": changed, "reboot_required": pending,
              "interfaces": [], "warnings": [], "verified": False}
    if not runtime:
        return report
    command = ["netplan", "generate"]
    if root != Path("/"):
        command += ["--root-dir", str(root)]
    generated = _safe_run(runner, *command)
    effective = netplan_config(root, include_managed=True)
    expected = overlay["network"]
    def contains(actual, expected):
        if isinstance(expected, dict):
            return isinstance(actual, dict) and all(contains(actual.get(key), value)
                                                    for key, value in expected.items())
        return actual == expected

    matches = contains(effective, expected)
    if generated is None or generated.returncode or not matches:
        # Leave the next boot's inputs intact when generation rejects the policy.
        for path, original in originals.items():
            if original is None:
                path.unlink(missing_ok=True)
            else:
                _write(path, *original)
        report["changed"] = False
        report["reboot_required"] = False
        report["warnings"].append("network_config_verification_failed")
        return report
    for interface in physical_ethernet(root):
        name = interface["name"]
        before = _safe_run(runner, "ethtool", name)
        details = before.stdout if before is not None and before.returncode == 0 else ""
        supports = re.search(r"Supports Wake-on:\s*(\S+)", details)
        enabled = re.search(r"(?m)^\s*Wake-on:\s*(\S+)", details)
        capable = bool(supports and "g" in supports.group(1))
        active = bool(enabled and "g" in enabled.group(1))
        if capable and not active:
            _safe_run(runner, "ethtool", "-s", name, "wol", "g")
            after = _safe_run(runner, "ethtool", name)
            enabled = re.search(r"(?m)^\s*Wake-on:\s*(\S+)", after.stdout if after is not None else "")
            active = bool(after is not None and after.returncode == 0 and enabled and "g" in enabled.group(1))
        if renderer == "NetworkManager":
            connection = _safe_run(runner, "nmcli", "-g", "GENERAL.CON-UUID", "device", "show", name)
            uuid = connection.stdout.strip() if connection is not None and connection.returncode == 0 else ""
            if re.fullmatch(r"[0-9a-fA-F-]{36}", uuid):
                client = _safe_run(runner, "nmcli", "-g", "ipv4.dhcp-client-id", "connection", "show", uuid)
                client_id = client.stdout.strip() if client is not None and client.returncode == 0 else ""
                if not client_id:
                    lease = _safe_run(runner, "nmcli", "-g", "DHCP4.OPTION", "device", "show", name)
                    option = re.search(r"(?m)^dhcp_client_identifier\s*=\s*([^\n]+)$",
                                       lease.stdout if lease is not None and lease.returncode == 0 else "")
                    client_id = option.group(1).strip() if option else "mac"
                    saved = _safe_run(runner, "nmcli", "connection", "modify", "uuid", uuid,
                                      "ipv4.dhcp-client-id", client_id)
                    readback = _safe_run(runner, "nmcli", "-g", "ipv4.dhcp-client-id", "connection", "show", uuid)
                    if (saved is None or saved.returncode or readback is None or readback.returncode or
                            readback.stdout.strip() != client_id):
                        report["warnings"].append("dhcp_identity_persistence_failed:" + name)
                if capable:
                    setting = _safe_run(runner, "nmcli", "-g", "802-3-ethernet.wake-on-lan", "connection", "show", uuid)
                    if setting is None or setting.returncode or setting.stdout.strip() not in {"64", "magic", "64 (magic)"}:
                        saved = _safe_run(runner, "nmcli", "connection", "modify", "uuid", uuid,
                                          "802-3-ethernet.wake-on-lan", "magic")
                        readback = _safe_run(runner, "nmcli", "-g", "802-3-ethernet.wake-on-lan",
                                             "connection", "show", uuid)
                        if (saved is None or saved.returncode or readback is None or readback.returncode or
                                readback.stdout.strip() not in {"64", "magic", "64 (magic)"}):
                            report["warnings"].append("wol_connection_persistence_failed:" + name)
        interface.update(wol_supported=capable, wol_enabled=active)
        report["interfaces"].append(interface)
        if not active:
            report["warnings"].append(("wol_readback_failed:" if capable else "wol_not_supported:") + name)
    if not report["interfaces"]:
        report["warnings"].append("no_physical_ethernet_for_wol_verification")
    # Unsupported hardware is a truthful warning, not an installation failure.
    report["verified"] = not any(warning.startswith(("wol_readback_failed:",
                                                     "wol_connection_persistence_failed:",
                                                     "dhcp_identity_persistence_failed:"))
                                 for warning in report["warnings"])
    return report
