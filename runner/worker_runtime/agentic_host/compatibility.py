"""Small, read-only checks for the contracts used by fixed host mutations.

These checks report observed capabilities, not a promise about every upstream
release. New version strings and additional JSON/XML fields are tolerated.
No package installation, service restart or network change happens here.
"""
import ipaddress
import json
import platform
import re
import subprocess
import time
import xml.etree.ElementTree as ET

from .coding_sandbox import SandboxError, validate_docker_security

MAX_OUTPUT = 262144
# An image just written to a slow drive can take far longer to inspect than a capability probe;
# provisioning gives it the bound of the qemu-img check that follows.
IMAGE_TIMEOUT = 300
ACTIONS = {"provision", "start", "shutdown", "snapshot", "restore", "bootstrap_hermes"}
REQUIRES = {
    "linux_runtime": ACTIONS,
    "libvirt_connection": ACTIONS - {"bootstrap_hermes"},
    "libvirt_kvm": {"provision", "start"},
    "route_json": {"provision", "bootstrap_hermes"},
    "default_route": {"bootstrap_hermes"},
    "sparse_copy": {"snapshot", "restore"},
    "qemu_image": {"provision"},
    "docker_limits": {"bootstrap_hermes"},
    "compose_api": {"bootstrap_hermes"},
}


class ContractError(ValueError):
    pass


def output(run, *args, timeout=3):
    result = run(*args, timeout=timeout, check=False)
    if result.returncode != 0:
        raise ContractError("command_failed")
    if not isinstance(result.stdout, str) or len(result.stdout.encode()) > MAX_OUTPUT:
        raise ContractError("response_invalid")
    return result.stdout


def routes(body):
    value = json.loads(body)
    if not isinstance(value, list) or len(value) > 8192:
        raise ContractError("response_invalid")
    for route in value:
        if not isinstance(route, dict) or not isinstance(route.get("dst"), str):
            raise ContractError("response_invalid")
        if route["dst"] != "default":
            ipaddress.ip_network(route["dst"], strict=False)
        if "dev" in route and (not isinstance(route["dev"], str) or not re.fullmatch(r"[^\s/\x00]{1,15}", route["dev"])):
            raise ContractError("response_invalid")
        if not isinstance(route.get("type", "unicast"), str):
            raise ContractError("response_invalid")
        flags = route.get("flags", [])
        if not isinstance(flags, list) or not all(isinstance(flag, str) for flag in flags):
            raise ContractError("response_invalid")
        if "gateway" in route:
            if not isinstance(route["gateway"], str):
                raise ContractError("response_invalid")
            ipaddress.ip_address(route["gateway"])
    return value


def default_route(value):
    return any(route["dst"] in {"default", "0.0.0.0/0", "::/0"} and route.get("dev") and
               route.get("type", "unicast") == "unicast" and
               not {"linkdown", "dead"}.intersection(route.get("flags", [])) for route in value)


def route_overlap(value):
    subnet = ipaddress.ip_network("192.168.177.0/24")
    for route in value:
        if route["dst"] in {"default", "0.0.0.0/0", "::/0"}:
            continue
        candidate = ipaddress.ip_network(route["dst"], strict=False)
        if candidate.version == subnet.version and subnet.overlaps(candidate):
            return True
    return False


def network_status(run):
    code, ready = None, False
    try:
        value = routes(output(run, "ip", "-json", "route", "show", "default"))
        ready = bool(default_route(value))
        if not ready:
            code = "default_route_unavailable"
    except (OSError, ValueError, TypeError, AttributeError, RecursionError, subprocess.TimeoutExpired):
        code = "route_contract_unavailable"
    return {"ready": ready, "mode": "automatic", "remote_access": "deferred",
            "observation": "outer_host_default_route", "internet_verified": False, "error_code": code}


def check_kvm(body):
    root = ET.fromstring(body)
    if root.tag != "capabilities" or not any(
            guest.findtext("os_type") == "hvm" and
            any(arch.get("name") == "x86_64" and arch.find("domain[@type='kvm']") is not None
                for arch in guest.findall("arch")) for guest in root.findall("guest")):
        raise ContractError("required_capability_missing")


def check_image(run, path):
    """Inspect only a broker-selected, inactive product disk; never follow backing files."""
    value = json.loads(output(run, "qemu-img", "info", "--output=json", str(path), timeout=IMAGE_TIMEOUT))
    if (not isinstance(value, dict) or value.get("format") != "qcow2" or
            type(value.get("virtual-size")) is not int or value["virtual-size"] <= 0 or
            any(value.get(key) for key in ("backing-filename", "full-backing-filename", "encrypted"))):
        raise ContractError("image_contract_invalid")
    return True


def inspect(run, action):
    if action not in ACTIONS:
        raise ValueError("Unknown compatibility action")
    checks = []
    route_value = None
    for name, affected in REQUIRES.items():
        if action not in affected:
            continue
        code = None
        try:
            if name == "linux_runtime":
                if platform.system() != "Linux" or platform.machine() != "x86_64":
                    raise ContractError("required_capability_missing")
            elif name == "libvirt_connection":
                if output(run, "virsh", "--readonly", "--connect", "qemu:///system", "uri").strip() != "qemu:///system":
                    raise ContractError("response_invalid")
            elif name == "libvirt_kvm":
                check_kvm(output(run, "virsh", "--readonly", "--connect", "qemu:///system", "capabilities"))
            elif name == "route_json":
                route_value = routes(output(run, "ip", "-json", "route", "show", "table", "all"))
            elif name == "default_route":
                if route_value is None or not default_route(route_value):
                    raise ContractError("required_capability_missing")
            elif name == "sparse_copy":
                help_text = output(run, "cp", "--help")
                if not all(flag in help_text for flag in ("--reflink", "--sparse")):
                    raise ContractError("required_capability_missing")
            elif name == "qemu_image":
                help_text = output(run, "qemu-img", "--help")
                if not all(flag in help_text for flag in ("info", "check", "resize", "--output")):
                    raise ContractError("required_capability_missing")
            elif name == "docker_limits":
                validate_docker_security(json.loads(output(run, "docker", "info", "--format", "{{json .}}")))
            elif name == "compose_api":
                version = json.loads(output(run, "docker", "compose", "version", "--format", "json"))
                if not isinstance(version, dict) or not isinstance(version.get("version"), str) or not version["version"]:
                    raise ContractError("response_invalid")
                help_text = output(run, "docker", "compose", "up", "--help")
                if not all(flag in help_text for flag in ("--wait", "--no-deps")):
                    raise ContractError("required_capability_missing")
        except FileNotFoundError:
            code = "command_missing"
        except PermissionError:
            code = "permission_denied"
        except subprocess.TimeoutExpired:
            code = "probe_timeout"
        except ContractError as exc:
            code = str(exc) if str(exc) in {"command_failed", "response_invalid", "required_capability_missing"} else "response_invalid"
        except (OSError, ValueError, TypeError, AttributeError, RecursionError, ET.ParseError, SandboxError):
            code = "response_invalid"
        checks.append({"id": name, "state": "supported" if code is None else "blocked", "error_code": code,
                       "affected_actions": sorted(affected), "observed_from": "outer_host_fixed_probe"})
    return {"state": "supported" if all(check["state"] == "supported" for check in checks) else "blocked",
            "action": action, "observed_at": time.time(), "checks": checks,
            "scope": "required_fixed_host_contracts", "reuse_for_mutations": False}
