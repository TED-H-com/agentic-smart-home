"""Fixed declarative Host configuration reconciliation; no executable update hooks.

prepare() runs in the stable updater before the tree switch. The same stable
module performs rollback even when the old product predates this capability.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import stat
from pathlib import Path

from .manager import HostError, Manager, atomic, sync_path

DIGEST = re.compile(r"[a-f0-9]{64}\Z")
UNITS = ("agentic-home.service", "agentic-home-host.service", "agentic-home-proxy.service")
TARGETS = {"/etc/systemd/system/" + unit: ("host/systemd/" + unit, 0o644) for unit in UNITS}
TARGETS.update({"/etc/systemd/logind.conf.d/60-agentic-home.conf": ("host/managed/logind.conf", 0o644),
                "/etc/netplan/60-agentic-home.yaml": ("host/managed/netplan.yaml", 0o600)})
MAX_FILE = 128 * 1024


def fingerprint(data):
    return hashlib.sha256(data).hexdigest()


class HostReconcile:
    def __init__(self, manager, *, product=None, system_root="/"):
        self.manager = manager
        self.product = Path(product) if product is not None else manager.product
        # Injection is for confined tests only; the CLI never accepts a root/path.
        self.system_root = Path(system_root)
        self.ledger = manager.root / "managed-system-files.json"

    def _path(self, target):
        if target not in TARGETS:
            raise HostError("Host update configuration target is not allowed")
        path = self.system_root / target.lstrip("/")
        self._trusted(path, optional=True)
        return path

    @staticmethod
    def _trusted(path, *, optional=False):
        for item in (path, *path.parents):
            if item.is_symlink():
                raise HostError("Host update configuration path is untrusted")
            if not item.exists():
                if item == path and not optional:
                    raise HostError("Host update configuration file is missing")
                continue
            info = item.stat()
            if info.st_uid != 0 or info.st_mode & 0o022 or not (stat.S_ISREG(info.st_mode) if item == path else stat.S_ISDIR(info.st_mode)):
                raise HostError("Host update configuration ownership is untrusted")

    def _read(self, path, *, optional=False):
        self._trusted(path, optional=optional)
        if not path.exists():
            return None
        if path.stat().st_size > MAX_FILE:
            raise HostError("Host update configuration file exceeds limit")
        data = path.read_bytes()
        return {"content": base64.b64encode(data).decode(), "sha256": fingerprint(data),
                "mode": stat.S_IMODE(path.stat().st_mode)}

    def _journal(self, candidate):
        if not isinstance(candidate, str) or not DIGEST.fullmatch(candidate):
            raise HostError("Invalid Host update candidate")
        path = self.manager.root / "host-updates" / candidate / "reconcile.json"
        self._trusted(path, optional=True)
        return path

    def _load(self, candidate):
        path = self._journal(candidate)
        self._trusted(path)
        if path.stat().st_size > 2 * 1024**2:
            raise HostError("Host update configuration journal exceeds limit")
        record = json.loads(path.read_text())
        if (not isinstance(record, dict) or set(record) != {"format", "candidate_digest", "phase", "files"}
                or record["format"] != 1 or record["candidate_digest"] != candidate
                or record["phase"] not in {"prepared", "applying", "applied", "rolling_back", "rolled_back"}
                or not isinstance(record["files"], list) or len(record["files"]) > len(TARGETS)):
            raise HostError("Host update configuration journal is invalid")
        seen = set()
        for item in record["files"]:
            if not isinstance(item, dict) or set(item) != {"target", "before", "after", "state"} or item["target"] in seen:
                raise HostError("Host update configuration journal is invalid")
            self._path(item["target"])
            seen.add(item["target"])
            if item["state"] not in {"prepared", "write_requested", "applied", "restore_requested", "restored", "owner_modified"}:
                raise HostError("Host update configuration journal state is invalid")
            for key in ("before", "after"):
                value = item[key]
                if value is None and key == "before":
                    continue
                if not isinstance(value, dict) or set(value) != {"content", "sha256", "mode"}:
                    raise HostError("Host update configuration preimage is invalid")
                data = base64.b64decode(value["content"], validate=True)
                if (len(data) > MAX_FILE or fingerprint(data) != value["sha256"] or type(value["mode"]) is not int
                        or not 0 <= value["mode"] <= 0o777 or value["mode"] & 0o022
                        or (key == "after" and value["mode"] != TARGETS[item["target"]][1])):
                    raise HostError("Host update configuration preimage is invalid")
        return record

    def _save(self, candidate, record):
        path = self._journal(candidate)
        self._trusted(path.with_name(path.name + ".new"), optional=True)
        atomic(path, record)

    def _plan(self, product):
        manifest = product / "host/managed-system.json"
        if manifest.exists() or manifest.is_symlink():
            raw = self._read(manifest)
            value = json.loads(base64.b64decode(raw["content"]))
            if (not isinstance(value, dict) or set(value) not in ({"format", "files"}, {"format", "files", "appliance_policy"})
                    or type(value["format"]) is not int or value["format"] != 1
                    or "appliance_policy" in value and (type(value["appliance_policy"]) is not int or value["appliance_policy"] != 1)):
                raise HostError("Host managed configuration manifest is invalid")
            entries = value["files"]
        else:
            entries = [{"target": "/etc/systemd/system/" + unit, "source": "host/systemd/" + unit, "mode": 0o644}
                       for unit in UNITS if (product / "host/systemd" / unit).exists()]
        if not isinstance(entries, list) or len(entries) > len(TARGETS):
            raise HostError("Host managed configuration manifest exceeds limit")
        plan = {}
        for item in entries:
            if not isinstance(item, dict) or set(item) != {"target", "source", "mode"}:
                raise HostError("Host managed configuration entry is invalid")
            target = item["target"]
            if target not in TARGETS or target in plan or (item["source"], item["mode"]) != TARGETS[target] or type(item["mode"]) is not int:
                raise HostError("Host managed configuration target is not allowed")
            source = self._read(product / item["source"])
            plan[target] = {**source, "mode": item["mode"]}
        return plan

    def _appliance(self):
        from .appliance_update import ApplianceUpdate
        return ApplianceUpdate(self)

    def _appliance_requested(self):
        manifest = self.product / "host/managed-system.json"
        if not manifest.exists():
            return False
        value = json.loads(base64.b64decode(self._read(manifest)["content"]))
        return value.get("appliance_policy") == 1

    def prepare(self, candidate, *, previous_product=None):
        path = self._journal(candidate)
        if path.exists():
            return self._load(candidate)
        previous_product = Path(previous_product) if previous_product is not None else self.manager.product
        plan = self._plan(self.product)
        previous = self._plan(previous_product)
        self._trusted(self.ledger, optional=True)
        owned = json.loads(self.ledger.read_text()) if self.ledger.exists() else {}
        if not isinstance(owned, dict):
            raise HostError("Host managed file ownership ledger is invalid")
        items = []
        for target, after in plan.items():
            before = self._read(self._path(target), optional=True)
            expected = owned.get(target) or previous.get(target)
            if before is not None and before != after and (expected is None or before["sha256"] != expected.get("sha256") or before["mode"] != expected.get("mode")):
                raise HostError("偵測到自行修改的主機設定，已停止更新並保留原設定。")
            items.append({"target": target, "before": before, "after": after, "state": "prepared"})
        if self._appliance_requested():
            from .appliance_update import TARGETS as appliance_targets
            if set(plan) & set(appliance_targets):
                raise HostError("Appliance configuration cannot also use a static file plan")
            self._appliance().prepare(candidate)
        record = {"format": 1, "candidate_digest": candidate, "phase": "prepared", "files": items}
        self._save(candidate, record)
        return record

    @staticmethod
    def _same(left, right):
        return left == right

    def _write(self, target, image):
        path = self._path(target)
        if image is None:
            path.unlink(missing_ok=True)
            sync_path(path.parent)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self._trusted(path, optional=True)
        temporary = path.with_name(path.name + ".agentic-new")
        self._trusted(temporary, optional=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, image["mode"])
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), image["mode"])
            stream.write(base64.b64decode(image["content"], validate=True))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_path(path.parent)

    def _reload(self, record):
        if any(item["target"].startswith("/etc/systemd/system/") for item in record["files"]):
            self.manager.run("systemctl", "daemon-reload", timeout=60)
        if any(item["target"] == "/etc/systemd/logind.conf.d/60-agentic-home.conf" for item in record["files"]):
            self.manager.run("systemctl", "kill", "--kill-whom=main", "--signal=HUP", "systemd-logind.service", timeout=30)
        if any(item["target"] == "/etc/netplan/60-agentic-home.yaml" for item in record["files"]):
            # Validate generated configuration; changing live links is deliberately
            # outside this hook. The receipt explicitly requires a later reboot.
            self.manager.run("netplan", "generate", timeout=30)

    def apply(self, candidate):
        record = self._load(candidate)
        if record["phase"] not in {"prepared", "applying", "applied"}:
            raise HostError("Host configuration rollback already started")
        record["phase"] = "applying"
        self._save(candidate, record)
        for item in record["files"]:
            current = self._read(self._path(item["target"]), optional=True)
            if self._same(current, item["after"]):
                item["state"] = "applied"
                self._save(candidate, record)
                continue
            if not self._same(current, item["before"]):
                raise HostError("主機設定在更新期間已變更，已保留原設定並停止套用。")
            item["state"] = "write_requested"
            self._save(candidate, record)
            self._write(item["target"], item["after"])
            item["state"] = "applied"
            self._save(candidate, record)
        self._reload(record)
        appliance = self._appliance()
        appliance_result = appliance.apply(candidate) if appliance.journal(candidate).exists() else None
        record["phase"] = "applied"
        self._save(candidate, record)
        self._trusted(self.ledger, optional=True)
        self._trusted(self.ledger.with_name(self.ledger.name + ".new"), optional=True)
        owned = json.loads(self.ledger.read_text()) if self.ledger.exists() else {}
        for item in record["files"]:
            owned[item["target"]] = {key: item["after"][key] for key in ("sha256", "mode")}
        atomic(self.ledger, owned)
        return {"state": "applied", "reboot_required": bool(appliance_result) or any(item["target"].startswith("/etc/netplan/") for item in record["files"])}

    def rollback(self, candidate):
        record = self._load(candidate)
        record["phase"] = "rolling_back"
        self._save(candidate, record)
        preserved = []
        appliance = self._appliance()
        if appliance.journal(candidate).exists():
            preserved.extend(appliance.rollback(candidate))
        for item in reversed(record["files"]):
            current = self._read(self._path(item["target"]), optional=True)
            if self._same(current, item["before"]):
                item["state"] = "restored"
            elif not self._same(current, item["after"]):
                item["state"] = "owner_modified"
                preserved.append(item["target"])
            else:
                item["state"] = "restore_requested"
                self._save(candidate, record)
                self._write(item["target"], item["before"])
                item["state"] = "restored"
            self._save(candidate, record)
        self._reload(record)
        record["phase"] = "rolled_back"
        self._save(candidate, record)
        self._trusted(self.ledger, optional=True)
        owned = json.loads(self.ledger.read_text()) if self.ledger.exists() else {}
        for item in record["files"]:
            if item["state"] == "restored" and item["before"] is not None:
                owned[item["target"]] = {key: item["before"][key] for key in ("sha256", "mode")}
            else:
                owned.pop(item["target"], None)
        self._trusted(self.ledger.with_name(self.ledger.name + ".new"), optional=True)
        atomic(self.ledger, owned)
        return {"state": "rolled_back", "owner_modified_preserved": preserved}

    def health(self, candidate):
        record = self._load(candidate)
        if record["phase"] != "applied":
            raise HostError("Host configuration has not completed")
        units = []
        for item in record["files"]:
            if self._read(self._path(item["target"]), optional=True) != item["after"]:
                raise HostError("Host configuration readback mismatch")
            if item["target"].startswith("/etc/systemd/system/"):
                unit = Path(item["target"]).name
                fragment = self.manager.run("systemctl", "show", "--property=FragmentPath", "--value", unit, timeout=10).stdout.strip()
                if fragment != item["target"]:
                    raise HostError("Host systemd unit readback mismatch")
                self.manager.run("systemctl", "is-active", "--quiet", unit, timeout=10)
                units.append(unit)
        appliance = self._appliance()
        if appliance.journal(candidate).exists():
            appliance.health(candidate)
        return {"state": "healthy", "units": units, "configuration_files_verified": len(record["files"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    for action in ("apply", "rollback", "health"):
        actions.add_argument("--" + action, action="store_true")
    parser.add_argument("--candidate", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Host configuration reconciliation requires the fixed root updater")
    manager = Manager()
    reconciler = HostReconcile(manager)
    action = "apply" if args.apply else "rollback" if args.rollback else "health"
    print(json.dumps(getattr(reconciler, action)(args.candidate), ensure_ascii=False))


if __name__ == "__main__":
    main()
