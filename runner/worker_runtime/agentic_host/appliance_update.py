"""Stable updater's fixed appliance policy adapter, with reversible preimages.

No candidate code, package manager, connection activation or live DHCP mutation
runs here. Networking and WoL are persistent next-boot policy only.
"""
import base64
import hashlib
import json
import os
import re

from . import appliance_network as network
from .appliance_power import LID_PROPERTIES, POWER_KEYS, SLEEP_TARGETS, verify_power
from .manager import HostError, atomic, sync_path

PROFILE = "/etc/agentic-home/profile.json"
LOGIND = "/etc/systemd/logind.conf.d/60-agentic-home.conf"
DCONF = "/etc/dconf/db/local.d/60-agentic-home"
LOCKS = "/etc/dconf/db/local.d/locks/60-agentic-home"
DCONF_PROFILE = "/etc/dconf/profile/user"
NETPLAN = "/etc/netplan/99-agentic-home-appliance.yaml"
POLICY = "/var/lib/agentic-home/host/appliance-network-policy.json"
LINK = "/etc/systemd/network/99-default.link.d/60-agentic-home-wol.conf"
NM = "/etc/NetworkManager/conf.d/90-agentic-home-wol.conf"
MASKS = {"/etc/systemd/system/" + name for name in SLEEP_TARGETS}
TARGETS = {LOGIND: 0o644, DCONF: 0o644, LOCKS: 0o644, DCONF_PROFILE: 0o644,
           NETPLAN: 0o600, POLICY: 0o600, LINK: 0o600, NM: 0o600, **dict.fromkeys(MASKS, 0o777)}
LIMIT = 128 * 1024


def sha(data):
    return hashlib.sha256(data).hexdigest()


def image(text, mode=0o644, kind="file"):
    data = text.encode()
    return {"kind": kind, "content": base64.b64encode(data).decode(), "sha256": sha(data), "mode": mode}


class ApplianceUpdate:
    def __init__(self, reconciler):
        self.owner, self.manager = reconciler, reconciler.manager
        self.root = reconciler.system_root
        self.marker = self.manager.root / "appliance-update-managed.json"
        self.ledger = self.manager.root / "appliance-update-files.json"

    def path(self, target):
        if target not in TARGETS and target != PROFILE:
            raise HostError("Appliance update target is not allowed")
        return self.root / target.lstrip("/")

    def read(self, target):
        path = self.path(target)
        if target in MASKS and path.is_symlink():
            self.owner._trusted(path.parent / ".agentic-check", optional=True)
            if path.lstat().st_uid != 0 or os.readlink(path) != "/dev/null":
                raise HostError("Appliance sleep target is owned by another policy")
            return image("/dev/null", 0o777, "mask")
        value = self.owner._read(path, optional=True)
        return {"kind": "file", **value} if value is not None else None

    def journal(self, candidate):
        return self.owner._journal(candidate).with_name("appliance.json")

    def save(self, candidate, value):
        path = self.journal(candidate)
        self.owner._trusted(path, optional=True)
        self.owner._trusted(path.with_name(path.name + ".new"), optional=True)
        atomic(path, value)

    def load(self, candidate):
        path = self.journal(candidate)
        self.owner._trusted(path)
        if path.stat().st_size > 6 * 1024**2:
            raise HostError("Appliance update journal exceeds limit")
        value = json.loads(path.read_text())
        if (not isinstance(value, dict) or set(value) != {"format", "candidate_digest", "phase", "profile", "power", "desktop", "files", "inputs"}
                or type(value["format"]) is not int or value["format"] != 1 or value["candidate_digest"] != candidate
                or value["phase"] not in {"prepared", "applying", "applied", "rolling_back", "rolled_back"}
                or type(value["power"]) is not bool or type(value["desktop"]) is not bool
                or not isinstance(value["profile"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["profile"])
                or not isinstance(value["inputs"], dict)
                or not isinstance(value["files"], list) or len(value["files"]) > len(TARGETS)):
            raise HostError("Appliance update journal is invalid")
        seen = set()
        for item in value["files"]:
            if (not isinstance(item, dict) or set(item) != {"target", "before", "after", "state"}
                    or item["target"] not in TARGETS or item["target"] in seen
                    or item["state"] not in {"prepared", "write_requested", "applied", "restore_requested", "restored", "owner_modified"}):
                raise HostError("Appliance update file journal is invalid")
            seen.add(item["target"])
            for key in ("before", "after"):
                entry = item[key]
                if entry is None and key == "before":
                    continue
                if not isinstance(entry, dict) or set(entry) != {"kind", "content", "sha256", "mode"}:
                    raise HostError("Appliance update preimage is invalid")
                data = base64.b64decode(entry["content"], validate=True)
                if (len(data) > LIMIT or sha(data) != entry["sha256"] or type(entry["mode"]) is not int
                        or entry["kind"] not in {"file", "mask"}
                        or (entry["kind"] == "mask" and (item["target"] not in MASKS or entry != image("/dev/null", 0o777, "mask")))
                        or (entry["kind"] == "file" and (not 0 <= entry["mode"] <= 0o777 or entry["mode"] & 0o022))
                        or (key == "after" and (entry["mode"] != TARGETS[item["target"]]
                                                or (entry["kind"] == "mask") != (item["target"] in MASKS)))):
                    raise HostError("Appliance update preimage is invalid")
        expected = {NETPLAN, POLICY, LINK, NM}
        if value["power"]:
            expected |= {LOGIND, *MASKS}
            if value["desktop"]:
                expected |= {DCONF, LOCKS, DCONF_PROFILE}
        if seen != expected:
            raise HostError("Appliance update file set is incomplete")
        return value

    def inputs(self):
        result = {}
        for directory in ("lib/netplan", "usr/lib/netplan", "etc/netplan", "run/netplan",
                          "etc/NetworkManager/system-connections", "run/NetworkManager/system-connections"):
            for path in (self.root / directory).glob("*.yaml" if "netplan" in directory else "*"):
                if path.name == network.OVERLAY:
                    continue
                if path.is_symlink():
                    raise HostError("Appliance network input is untrusted")
                entry = self.owner._read(path.resolve())
                result[str(path.relative_to(self.root))] = {"sha256": entry["sha256"], "mode": entry["mode"]}
                if len(result) > 256:
                    raise HostError("Appliance network input count exceeds limit")
        return result

    def _dependencies(self, profile):
        network._yaml()  # Trusted OS python3-yaml, never downloaded by an update.
        commands = [("netplan", "info")]
        if profile["appliance_lid"]:
            commands += [("systemctl", "--version"), ("busctl", "--version")]
            if profile["desktop"]:
                commands.append(("dconf", "help", "update"))
        for command in commands:
            self.manager.run(*command, timeout=15)

    def plan(self):
        raw = self.owner._read(self.path(PROFILE))
        profile = json.loads(base64.b64decode(raw["content"]))
        if (not isinstance(profile, dict) or any(type(profile.get(key)) is not bool for key in ("appliance_lid", "desktop"))):
            raise HostError("Installed appliance profile is invalid")
        self._dependencies(profile)
        # Only root-owned bounded regular configuration inputs may influence a
        # privileged generated policy; vendor /lib resolves to trusted /usr/lib.
        self.inputs()
        config = network.netplan_config(self.root)
        previous_image = self.read(POLICY)
        previous = json.loads(base64.b64decode(previous_image["content"])) if previous_image else {}
        if (not isinstance(previous, dict) or previous and (previous.get("version") != 1
                or previous.get("renderer") not in {"networkd", "NetworkManager"} or not isinstance(previous.get("overlay"), dict))):
            raise HostError("Installed appliance network baseline is invalid")
        renderer = previous.get("renderer") or ("networkd" if network._active_networkd(self.root) else config.get("renderer"))
        if not renderer:
            renderers = {value.get("renderer") for section in network.SECTIONS for value in config.get(section, {}).values() if value.get("renderer")}
            if len(renderers) > 1:
                raise HostError("Mixed network renderers need explicit migration review")
            renderer = next(iter(renderers), "networkd")
        identities = network._nm_identifiers(self.root)
        if renderer == "NetworkManager":
            active, warnings = network._active_nm_identities(network.physical_ethernet(self.root), self.manager.run)
            if warnings:
                raise HostError("無法確認目前 DHCP 身分，尚未變更網路設定。")
            identities.update({name: item["identity"] for name, item in active.items()})
            identities.update({"mac:" + item["mac"]: active[item["name"]]["identity"]
                               for item in network.physical_ethernet(self.root) if item["name"] in active})
        overlay = network.network_overlay(config, renderer, identities)
        # Retain prior explicit pins for disconnected devices exactly as #122.
        for section in network.SECTIONS:
            for name, saved in previous.get("overlay", {}).get("network", {}).get(section, {}).items():
                current = overlay["network"].get(section, {}).get(name)
                if current is not None and "dhcp-identifier" in saved:
                    current["dhcp-identifier"] = saved["dhcp-identifier"]
                saved_id = saved.get("networkmanager", {}).get("passthrough", {}).get("ipv4.dhcp-client-id")
                if current is not None and saved_id is not None and network._identity_for_device(name, config.get(section, {}).get(name, {}), identities) is None:
                    current.setdefault("networkmanager", {}).setdefault("passthrough", {})["ipv4.dhcp-client-id"] = saved_id
        boot = self.root / "proc/sys/kernel/random/boot_id"
        boot_id = boot.read_text().strip() if boot.exists() else None
        policy = {"version": 1, "renderer": renderer, "overlay": overlay,
                  "configured_boot_id": previous.get("configured_boot_id") if previous.get("overlay") == overlay else boot_id}
        plan = {NETPLAN: image("# Managed appliance policy; does not change addresses or routes.\n" + network._yaml().safe_dump(overlay, sort_keys=False), 0o600),
                LINK: image(network.LINK, 0o600), NM: image(network.NM_DEFAULT, 0o600),
                POLICY: image(json.dumps(policy, ensure_ascii=False, indent=2), 0o600)}
        if profile["appliance_lid"]:
            plan[LOGIND] = image("[Login]\n" + "".join(key + "=ignore\n" for key in LID_PROPERTIES))
            plan.update({target: image("/dev/null", 0o777, "mask") for target in sorted(MASKS)})
            if profile["desktop"]:
                plan[DCONF] = image("[org/gnome/settings-daemon/plugins/power]\n" + "".join(key + "=" + value + "\n" for key, value in POWER_KEYS.items()))
                plan[LOCKS] = image("".join("/org/gnome/settings-daemon/plugins/power/" + key + "\n" for key in POWER_KEYS))
                before = self.read(DCONF_PROFILE)
                text = base64.b64decode(before["content"]).decode() if before else "user-db:user\n"
                plan[DCONF_PROFILE] = image(text if "system-db:local" in text.splitlines() else text.rstrip() + "\nsystem-db:local\n")
        return profile, raw["sha256"], plan

    def prepare(self, candidate):
        if self.journal(candidate).exists():
            value = self.load(candidate)
            self.mark(candidate)
            return value
        profile, profile_hash, plan = self.plan()
        self.owner._trusted(self.ledger, optional=True)
        owned = json.loads(self.ledger.read_text()) if self.ledger.exists() else {}
        if not isinstance(owned, dict):
            raise HostError("Appliance update ownership ledger is invalid")
        files = []
        legacy_dconf = image("[org/gnome/settings-daemon/plugins/power]\nsleep-inactive-ac-type='nothing'\nsleep-inactive-ac-timeout=0\n")
        for target, after in plan.items():
            before = self.read(target)
            # Adopt only exact known product inputs, or append to the shared
            # dconf profile without replacing its existing owner entries.
            known = target == DCONF_PROFILE or target == DCONF and before == legacy_dconf
            if before is not None and before != after and before != owned.get(target) and not known:
                raise HostError("偵測到自行修改的家電設定，已停止更新並保留原設定。")
            files.append({"target": target, "before": before, "after": after, "state": "prepared"})
        value = {"format": 1, "candidate_digest": candidate, "phase": "prepared", "profile": profile_hash,
                 "power": profile["appliance_lid"], "desktop": profile["desktop"], "files": files, "inputs": self.inputs()}
        self.save(candidate, value)
        self.mark(candidate)
        return value

    def mark(self, candidate):
        self.owner._trusted(self.marker, optional=True)
        self.owner._trusted(self.marker.with_name(self.marker.name + ".new"), optional=True)
        atomic(self.marker, {"format": 1, "candidate_digest": candidate, "authority": "host_update"})

    def write(self, target, entry):
        path = self.path(target)
        if target in MASKS:
            self.read(target)  # Validate exact existing mask or safe regular path.
            path.parent.mkdir(parents=True, exist_ok=True)
            if entry is None:
                path.unlink(missing_ok=True)
            elif entry["kind"] == "mask":
                temporary = path.with_name(path.name + ".agentic-new")
                if temporary.is_symlink():
                    self.owner._trusted(temporary.parent / ".agentic-check", optional=True)
                    if temporary.lstat().st_uid != 0 or os.readlink(temporary) != "/dev/null":
                        raise HostError("Appliance temporary mask is untrusted")
                else:
                    self.owner._trusted(temporary, optional=True)
                    temporary.symlink_to("/dev/null")
                os.replace(temporary, path)
            else:
                raise HostError("Appliance sleep target preimage is invalid")
            sync_path(path.parent)
        else:
            # Use the same trusted fixed-file writer through a narrow adapter.
            self.owner._trusted(path, optional=True)
            if entry is None:
                path.unlink(missing_ok=True)
                sync_path(path.parent)
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".agentic-new")
            self.owner._trusted(temporary, optional=True)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, entry["mode"])
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), entry["mode"])
                stream.write(base64.b64decode(entry["content"], validate=True))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            sync_path(path.parent)

    def reload(self, value):
        self.manager.run("netplan", "generate", timeout=30)
        if value["power"]:
            self.manager.run("systemctl", "daemon-reload", timeout=30)
            self.manager.run("systemctl", "kill", "--kill-whom=main", "--signal=HUP", "systemd-logind.service", timeout=30)
            if value["desktop"]:
                self.manager.run("dconf", "update", timeout=30)

    def apply(self, candidate):
        value = self.load(candidate)
        if value["phase"] not in {"prepared", "applying", "applied"}:
            raise HostError("Appliance rollback already started")
        if self.read(PROFILE)["sha256"] != value["profile"]:
            raise HostError("家電設定檔已變更，尚未套用更新。")
        if self.inputs() != value["inputs"]:
            raise HostError("網路設定在更新期間已變更，已保留原設定。")
        self._dependencies({"appliance_lid": value["power"], "desktop": value["desktop"]})
        value["phase"] = "applying"
        self.save(candidate, value)
        for item in value["files"]:
            current = self.read(item["target"])
            if current != item["after"]:
                if current != item["before"]:
                    raise HostError("家電設定在更新期間已變更，已保留原設定。")
                item["state"] = "write_requested"
                self.save(candidate, value)
                self.write(item["target"], item["after"])
            item["state"] = "applied"
            self.save(candidate, value)
        self.reload(value)
        value["phase"] = "applied"
        self.save(candidate, value)
        self._ledger(value, after=True)
        return {"state": "applied", "reboot_required": True, "network": "persistent_policy_pending_reboot"}

    def _ledger(self, value, *, after):
        self.owner._trusted(self.ledger, optional=True)
        owned = json.loads(self.ledger.read_text()) if self.ledger.exists() else {}
        for item in value["files"]:
            entry = item["after"] if after else item["before"] if item["state"] == "restored" else None
            if entry is not None:
                owned[item["target"]] = entry
            else:
                owned.pop(item["target"], None)
        self.owner._trusted(self.ledger.with_name(self.ledger.name + ".new"), optional=True)
        atomic(self.ledger, owned)

    def rollback(self, candidate):
        value = self.load(candidate)
        value["phase"] = "rolling_back"
        self.save(candidate, value)
        preserved = []
        for item in reversed(value["files"]):
            try:
                current = self.read(item["target"])
            except HostError:
                # An owner may replace a managed file or mask with a symlink or
                # change ownership. Do not follow it or undo that later choice.
                item["state"] = "owner_modified"
                preserved.append(item["target"])
                self.save(candidate, value)
                continue
            if current == item["before"]:
                item["state"] = "restored"
            elif current != item["after"]:
                item["state"] = "owner_modified"
                preserved.append(item["target"])
            else:
                item["state"] = "restore_requested"
                self.save(candidate, value)
                self.write(item["target"], item["before"])
                item["state"] = "restored"
            self.save(candidate, value)
        self.reload(value)
        value["phase"] = "rolled_back"
        self.save(candidate, value)
        self._ledger(value, after=False)
        return preserved

    def health(self, candidate):
        value = self.load(candidate)
        if value["phase"] != "applied" or any(self.read(item["target"]) != item["after"] for item in value["files"]):
            raise HostError("Appliance policy readback mismatch")
        if value["power"] and not verify_power(self.manager.run)["verified"]:
            raise HostError("蓋上蓋子後繼續運作的設定尚未確認，已停止更新。")
        expected = json.loads(base64.b64decode(next(item["after"]["content"] for item in value["files"] if item["target"] == POLICY)))["overlay"]["network"]
        effective = network.netplan_config(self.root, include_managed=True)
        def contains(actual, desired):
            if isinstance(desired, dict):
                return isinstance(actual, dict) and all(contains(actual.get(key), entry) for key, entry in desired.items())
            return actual == desired
        if not contains(effective, expected):
            raise HostError("持續性網路設定被其他設定覆蓋，已停止更新。")
        return {"state": "healthy", "reboot_required": True, "network": "persistent_policy_pending_reboot"}
