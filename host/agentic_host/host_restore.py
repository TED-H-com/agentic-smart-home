"""Fixed Host-update restore points. No snapshot deletion, merge, or DB restore.

Call only while holding Manager.locked(). The caller must persist every journal
transition using save before this module performs its corresponding side effect.
"""
import json
import math
import re
import shutil
import time
from pathlib import Path

from .manager import NAME, HostError

GIB = 1024**3
MARGIN = 64 * 1024**2
FIELDS = "lv_uuid,vg_uuid,vg_name,lv_name,lv_path,origin_uuid,lv_attr,data_percent,lv_size,vg_free,vg_extent_size,lv_tags"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9+_.-]{0,126}\Z")
UUID = re.compile(r"[A-Za-z0-9-]{6,128}\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")


class HostRestore:
    def __init__(self, manager, *, save=None):
        self.manager = manager
        self.save = save

    def _persist(self, record, **changes):
        record["restore_point"].update(changes)
        if self.save is None:
            raise HostError("主機更新缺少持久記錄，已停止建立還原點。")
        self.save(record)

    def recover_ha(self, record):
        """Recover only the recorded guest availability, never repeat a backup."""
        journal = record.get("restore_point")
        if not isinstance(journal, dict) or journal.get("ha_original_state") == "not_created":
            return {"state": "not_needed"}
        original = journal.get("ha_original_state")
        if original not in {"running", "shut off"}:
            raise HostError("無法確認更新前的家庭系統狀態；未進行開關機。")
        if self.manager.owned() is None:
            raise HostError("無法確認原有家庭系統，尚未重新啟動。")
        current = self.manager.virsh("domstate", NAME).stdout.strip()
        if original == "running" and current != "running":
            self._persist(record, ha_recovery="restart_requested")
            self.manager.start()
            current = self.manager.virsh("domstate", NAME).stdout.strip()
        if original == "running" and current != "running":
            raise HostError("尚未確認家庭系統已重新啟動；不會強制斷電或重複建立快照。")
        # A manually started guest wins over an old 'shut off' preimage.
        result = {"state": "running_verified" if original == "running" else
                  "stopped_verified" if current == "shut off" else "owner_state_preserved", "vm": current}
        self._persist(record, ha_recovery=result["state"])
        return result

    def _lvm(self):
        source = self.manager.run("findmnt", "--noheadings", "--output", "SOURCE", "--target", "/", timeout=5).stdout.strip()
        if not source.startswith("/dev/"):
            raise HostError("這台主機尚不支援安全更新：根磁碟必須使用 LVM，尚未變更系統。")
        raw = self.manager.run("lvs", "--reportformat", "json", "--units", "b", "--nosuffix", "--options", FIELDS, timeout=10).stdout
        rows = json.loads(raw)["report"][0]["lv"]
        if not isinstance(rows, list) or len(rows) > 1024:
            raise ValueError("invalid LVM report")
        roots = [row for row in rows if Path(row.get("lv_path", "")).resolve() == Path(source).resolve()]
        if len(roots) != 1:
            raise ValueError("root LV ambiguous")
        root = roots[0]
        if (not all(UUID.fullmatch(root.get(field, "")) for field in ("lv_uuid", "vg_uuid"))
                or not all(IDENTIFIER.fullmatch(root.get(field, "")) for field in ("vg_name", "lv_name"))
                or root.get("lv_path") != "/dev/" + root["vg_name"] + "/" + root["lv_name"]
                or root.get("origin_uuid", "").strip() or root.get("lv_attr", "")[:1] not in {"-", "o"}
                or len(root.get("lv_attr", "")) < 5 or root["lv_attr"][4] != "a" or root["lv_attr"][1] != "w"):
            raise ValueError("root LV unsupported")
        for field in ("lv_size", "vg_free", "vg_extent_size"):
            value = float(root[field])
            if not math.isfinite(value) or value < (0 if field == "vg_free" else 1):
                raise ValueError("root capacity invalid")
            root[field] = int(value)
        snapshots = []
        for row in rows:
            if row.get("origin_uuid", "").strip() != root["lv_uuid"] or row.get("vg_uuid") != root["vg_uuid"]:
                continue
            attr = row.get("lv_attr", "")
            if (not attr.startswith("s") or len(attr) < 5 or attr[4] != "a"
                    or not UUID.fullmatch(row.get("lv_uuid", ""))
                    or not all(IDENTIFIER.fullmatch(row.get(field, "")) for field in ("vg_name", "lv_name"))
                    or row.get("lv_path") != "/dev/" + row["vg_name"] + "/" + row["lv_name"]):
                raise ValueError("root snapshot unknown or invalid")
            percent, size = float(row["data_percent"]), float(row["lv_size"])
            if not math.isfinite(percent) or not 0 <= percent < 100 or not math.isfinite(size) or size <= 0:
                raise ValueError("root snapshot capacity invalid")
            snapshots.append({**row, "percent": percent, "size": int(size)})
        return root, snapshots

    def _checked_lvm(self):
        try:
            return self._lvm()
        except HostError:
            raise
        except Exception as exc:
            raise HostError("無法安全確認根磁碟還原點與可用空間，尚未變更系統。") from exc

    def capacity(self, required):
        if type(required) is not int or required < 0:
            raise HostError("Invalid Host update capacity estimate")
        checked = set()
        for target in (Path("/"), self.manager.root, getattr(self.manager, "images", self.manager.root)):
            target = Path(target)
            while not target.exists():
                target = target.parent
            device = target.stat().st_dev
            if device not in checked and shutil.disk_usage(target).free < required + MARGIN:
                raise HostError("磁碟可用空間不足，已停止更新；家庭資料與原有還原點已保留。")
            checked.add(device)
        root, snapshots = self._checked_lvm()
        if any(item["percent"] >= 80 for item in snapshots):
            raise HostError("根磁碟還原點空間已不足，已停止更新；原有還原點已保留。")
        cow = getattr(self.manager, "root_cow", None)
        if cow is not None:
            cow.require_capacity(required)
        else:
            for snapshot in snapshots:
                used = snapshot["size"] * snapshot["percent"] / 100
                needed = math.ceil((used + required + MARGIN) / .6) - snapshot["size"]
                if needed <= 0:
                    continue
                current_root, current_snapshots = self._checked_lvm()
                match = next((item for item in current_snapshots if item["lv_uuid"] == snapshot["lv_uuid"]), None)
                if current_root["lv_uuid"] != root["lv_uuid"] or current_root["vg_uuid"] != root["vg_uuid"] or not match or match["lv_path"] != snapshot["lv_path"]:
                    raise HostError("根磁碟還原點已變更，已停止更新以保護資料。")
                extent = current_root["vg_extent_size"]
                count = min(math.ceil(needed / extent), current_root["vg_free"] // extent)
                if count:
                    self.manager.run("lvextend", "--yes", "--extents", "+" + str(count), "--", match["lv_path"], timeout=60)
                    _, readback = self._checked_lvm()
                    grown = next((item for item in readback if item["lv_uuid"] == match["lv_uuid"]), None)
                    if not grown or grown["size"] < match["size"] + count * extent:
                        raise HostError("無法確認還原點已增加空間，尚未切換主機版本。")
        current_root, snapshots = self._checked_lvm()
        if (current_root["lv_uuid"], current_root["vg_uuid"]) != (root["lv_uuid"], root["vg_uuid"]):
            raise HostError("根磁碟已變更，尚未切換主機版本。")
        for item in snapshots:
            if item["size"] * item["percent"] / 100 + required + MARGIN >= item["size"] * .8:
                raise HostError("根磁碟還原點空間不足，已停止更新；原有還原點已保留。")
        snapshot_bytes = max(2 * GIB, required * 2 + MARGIN)
        snapshot_bytes = min(snapshot_bytes, current_root["lv_size"])
        extent = current_root["vg_extent_size"]
        snapshot_bytes = math.ceil(snapshot_bytes / extent) * extent
        if current_root["vg_free"] < snapshot_bytes:
            raise HostError("磁碟沒有足夠空間建立主機還原點，尚未切換版本；原有還原點已保留。")
        return {"root_lv_uuid": root["lv_uuid"], "vg_uuid": root["vg_uuid"], "root_lv_path": root["lv_path"],
                "snapshot_bytes": snapshot_bytes, "required_bytes": required}

    def create(self, record):
        candidate = record.get("candidate_digest")
        if not isinstance(candidate, str) or not DIGEST.fullmatch(candidate):
            raise HostError("Invalid Host update candidate")
        if self.manager.state().get("restore_pending"):
            raise HostError("家庭系統尚有未完成的復原作業，已停止主機更新。")
        if self.manager.state().get("owner"):
            from .ha_lan import HALan
            if HALan(self.manager).record().get("pending"):
                raise HostError("家庭網路尚有未完成的作業，已停止主機更新。")
        journal = record.get("restore_point")
        if journal and journal.get("phase") == "complete":
            root, snapshots = self._checked_lvm()
            match = next((item for item in snapshots if item["lv_uuid"] == journal.get("root_snapshot_uuid")), None)
            if (root["lv_uuid"], root["vg_uuid"]) != (journal["root_lv_uuid"], journal["vg_uuid"]) or not match or match["lv_path"] != journal.get("root_snapshot_path") or match["percent"] >= 60:
                raise HostError("先前主機還原點已改變或空間不足，已停止更新。")
            if any(item["size"] * item["percent"] / 100 + record.get("restore_required_bytes", 0) + MARGIN >= item["size"] * .8 for item in snapshots):
                raise HostError("先前主機還原點空間不足，已停止更新。")
            return dict(journal)
        if journal and journal.get("phase") not in {"root_snapshot_requested", "ha_complete", "preparing"}:
            if journal.get("ha_original_state") == "running":
                try:
                    self.manager.start()
                except Exception as exc:
                    raise HostError("還原點作業中斷，且尚未確認家庭系統已重新啟動；已停止更新，不會重複建立快照。") from exc
            raise HostError("先前還原點作業結果尚未確認；已停止更新，不會重複建立快照。")
        required = record.get("restore_required_bytes", 0)
        if journal is None:
            budget = self.capacity(required)
            state = self.manager.state()
            if state.get("restore_pending"):
                raise HostError("家庭系統尚有未完成的復原作業，已停止主機更新。")
            provisioned = bool(state.get("owner"))
            original = "not_created"
            if provisioned:
                from .ha_lan import HALan
                if HALan(self.manager).record().get("pending"):
                    raise HostError("家庭網路尚有未完成的作業，已停止主機更新。")
                if self.manager.owned() is None:
                    raise HostError("無法確認原有 HA 系統，尚未建立主機還原點。")
                original = self.manager.virsh("domstate", NAME).stdout.strip()
                if original not in {"running", "shut off"}:
                    raise HostError("家庭系統目前狀態尚未確認，已停止更新。")
                allocated = sum((self.manager.images / name).stat().st_blocks * 512 for name in ("haos.qcow2", "nvram.fd"))
                self.capacity(required + allocated + allocated // 10 + MARGIN)
            record["restore_point"] = {"phase": "preparing", "ha_original_state": original, "ha_snapshot": None,
                                       "created_at": time.time(), **budget}
            self._persist(record)
            journal = record["restore_point"]
        if journal["phase"] == "preparing":
            if journal["ha_original_state"] != "not_created":
                try:
                    self._persist(record, phase="ha_shutdown_requested")
                    self.manager.shutdown()
                    self._persist(record, phase="ha_snapshot_requested")
                    receipt = self.manager.snapshot()
                    self._persist(record, phase="ha_snapshot_complete", ha_snapshot=receipt["snapshot"])
                finally:
                    if journal["ha_original_state"] == "running":
                        try:
                            self.manager.start()
                        except Exception as exc:
                            raise HostError("尚未確認家庭系統已重新啟動；已停止更新並保留現有還原點。") from exc
                self._persist(record, phase="ha_complete")
            else:
                self._persist(record, phase="ha_complete")
        if journal["phase"] == "ha_complete":
            budget = self.capacity(required)
            if (budget["root_lv_uuid"], budget["vg_uuid"]) != (journal["root_lv_uuid"], journal["vg_uuid"]):
                raise HostError("根磁碟已變更，尚未切換主機版本。")
            name = "agentic-host-" + candidate[:32]
            tag = "agentic-host-update-" + candidate
            self._persist(record, phase="root_snapshot_requested", root_snapshot_name=name, root_snapshot_tag=tag,
                          snapshot_bytes=budget["snapshot_bytes"])
            # lvcreate is never repeated after this intent is persisted.
            self.manager.run("lvcreate", "--snapshot", "--name", name, "--size", str(budget["snapshot_bytes"]) + "B",
                             "--addtag", "agentic-home-host-update", "--addtag", tag, "--", budget["root_lv_path"], timeout=120)
        root, snapshots = self._checked_lvm()
        owned = [item for item in snapshots if item["lv_name"] == journal["root_snapshot_name"]
                 and journal["root_snapshot_tag"] in item.get("lv_tags", "").split(",")
                 and "agentic-home-host-update" in item.get("lv_tags", "").split(",")]
        if (root["lv_uuid"], root["vg_uuid"]) != (journal["root_lv_uuid"], journal["vg_uuid"]) or len(owned) != 1:
            raise HostError("主機還原點建立結果尚未確認，尚未切換版本；不會重複建立快照。")
        snapshot = owned[0]
        if snapshot["percent"] >= 60 or snapshot["size"] < journal["snapshot_bytes"]:
            raise HostError("主機還原點空間不足，尚未切換版本；原有還原點已保留。")
        if any(item["size"] * item["percent"] / 100 + required + MARGIN >= item["size"] * .8 for item in snapshots):
            raise HostError("還原點建立後空間不足，尚未切換版本；原有還原點已保留。")
        self._persist(record, phase="complete", root_snapshot_uuid=snapshot["lv_uuid"], root_snapshot_path=snapshot["lv_path"],
                      scope="HAOS disk/UEFI and root LVM restore points; no automatic merge or full-system rollback")
        return dict(record["restore_point"])
