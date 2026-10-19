"""Bounded host observations; no network or power configuration effects."""
import fcntl
import ipaddress
import json
import os
import re
import tempfile
import time
from pathlib import Path

MDNS_HOSTNAME = "castle-agentic-home.local"
ROUTER_HINT = "在路由器的「DHCP 位址保留」中，選這台主機並保留目前位址。"


def _command(run, *args, empty_exit=False):
    try:
        result = run(*args, timeout=3, check=False)
        if result.returncode == 0 or (empty_exit and result.returncode == 1 and
                                      not result.stdout and not getattr(result, "stderr", "")):
            return result.stdout
        return None
    except Exception:
        # A missing utility or broker command failure is an unavailable observation.
        return None


def _physical(interface, sysfs):
    if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,64}", interface):
        return False
    path = sysfs / interface
    return (path / "device").exists() or any((member / "device").exists()
                                                          for member in path.glob("lower_*"))


def _network(run, sysfs):
    try:
        routes = json.loads(_command(run, "ip", "-json", "route", "show", "default") or "null")
        addresses = json.loads(_command(run, "ip", "-json", "address", "show") or "null")
        if not isinstance(routes, list) or not isinstance(addresses, list):
            return None
        # Ignore container, VPN and HAOS addresses. A bridge is accepted only when
        # its lower interface is physical; its address/MAC is the DHCP endpoint.
        for route in sorted(routes, key=lambda value: value.get("metric", 0)):
            interface = route.get("dev", "")
            if not _physical(interface, sysfs):
                continue
            for item in addresses:
                if item.get("ifname") != interface:
                    continue
                candidates = []
                for address in item.get("addr_info", []):
                    if address.get("scope") != "global":
                        continue
                    ip = ipaddress.ip_address(address.get("local", ""))
                    if not (ip.is_loopback or ip.is_unspecified or ip.is_multicast or ip.is_link_local):
                        candidates.append(ip)
                if candidates:
                    candidates.sort(key=lambda value: (value.version, str(value)))
                    return {"interface": interface, "address": str(candidates[0]),
                            "mac": item.get("address")}
    except (ValueError, TypeError, AttributeError):
        pass
    return None


def _save(path, value):
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def observe_network(root, run, *, sysfs=Path("/sys/class/net")):
    """Record a baseline on startup and retain an address-change notice on polls.

    The dedicated flock covers observation and atomic replacement, so concurrent
    polls cannot overwrite a newer measurement or race on a shared .new file.
    Unavailable observations never replace the last successfully observed IP.
    """
    root, sysfs = Path(root), Path(sysfs)
    value = {"state": "unavailable", "address": None, "address_changed": False,
             "mdns_hostname": None, "router_reservation_hint": ROUTER_HINT,
             "source": "ip route/address; sysfs physical interfaces", "observed_at": time.time(),
             "limitations": "目前預設路由上的實體網路位址；不代表其他裝置能連入管理介面。"}
    hostname = (_command(run, "hostnamectl", "--static") or "").strip()
    if re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", hostname):
        value["mdns_hostname"] = hostname.lower() + ".local"
    try:
        root.mkdir(parents=True, exist_ok=True)
        with (root / "appliance-network-observation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = _network(run, sysfs)
            if current is None:
                return value
            path = root / "appliance-network-observation.json"
            try:
                previous = json.loads(path.read_text())
                if not isinstance(previous, dict):
                    previous = {}
            except (OSError, ValueError):
                previous = {}
            changed = previous.get("address") not in (None, current["address"])
            record = {**current, "previous_address": previous.get("address") if changed else
                      previous.get("previous_address"), "changed_at": time.time() if changed else
                      previous.get("changed_at")}
            if record != previous:
                _save(path, record)
            value.update(state="observed", **record, address_changed=bool(record["previous_address"]))
    except OSError:
        value["persistence_unavailable"] = True
        # Do not show a cached IP as a current measurement when persistence fails.
    return value


def _suspend(run):
    output = _command(run, "journalctl", "--no-pager", "--since=-14days", "--lines=200",
                      "--output=json",
                      "--grep=The system will suspend|Suspending system|Entering sleep state|"
                      r"Performing sleep operation|System is suspending|PM: suspend entry|Suspending\.\.\.", empty_exit=True)
    if output is None:
        return {"state": "unavailable", "warning": None}
    found = False
    try:
        for line in output.splitlines():
            event = json.loads(line)
            message = event.get("MESSAGE", "")
            if isinstance(message, str) and re.search(
                    r"The system will suspend|Suspending system|Entering sleep state|"
                    r"Performing sleep operation|System is suspending|PM: suspend entry|Suspending\.\.\.", message):
                found = True
    except (ValueError, AttributeError):
        return {"state": "unavailable", "warning": None}
    return {"state": "observed", "past_suspend": found,
            "warning": "最近曾進入睡眠，期間家庭自動化可能中斷；請確認目前的合蓋設定。" if found else None,
            "lookback_days": 14, "limitations": "只查最近 14 天最多 200 筆符合紀錄；未發現不代表從未睡眠。"}


def appliance_status(root, profile, run):
    """Safe additive Host status for old installations and missing utilities."""
    if profile is None:
        try:
            profile = json.loads(Path("/etc/agentic-home/profile.json").read_text())
        except (OSError, ValueError):
            profile = {}
    lid = {"state": "unavailable", "message": "尚未確認合蓋後會繼續運作。"}
    if isinstance(profile, dict) and profile.get("appliance_lid") is False:
        lid = {"state": "not_applicable", "message": "這台主機不使用筆電合蓋設定。"}
    elif isinstance(profile, dict) and profile.get("appliance_lid") is True:
        try:
            from .appliance_power import verify_power
            result = verify_power(run)
            lid = {"state": "verified" if result["verified"] else "unverified",
                   "message": "蓋上蓋子也會繼續運作" if result["verified"] else
                              "尚未確認合蓋後會繼續運作，請先保持上蓋開啟。",
                   "checks": result["checks"]}
        except Exception:
            pass
    return {"network": observe_network(root, run), "lid": lid,
            "suspend": _suspend(run), "observed_at": time.time(),
            "source": "installed Host broker; live system utilities", "target": "outer Linux host"}
