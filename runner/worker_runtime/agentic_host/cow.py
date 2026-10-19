"""Protect classic LVM snapshots of the mounted root, without deleting user restore points.

Only the root LV observed by findmnt can be enrolled. Its UUID and VG UUID are
pinned before any mutation; names and paths supplied by callers are never used.
"""
import json
import math
import re
import threading
from pathlib import Path

from .manager import HostError, atomic

FIELDS = "lv_uuid,vg_uuid,vg_name,lv_name,lv_path,origin_uuid,lv_attr,data_percent,lv_size,vg_free,vg_extent_size"
NAME = re.compile(r"[A-Za-z0-9+_.-]{1,127}\Z")
UUID = re.compile(r"[A-Za-z0-9-]{6,128}\Z")
WARN = 60
CRITICAL = 80
MARGIN = 64 * 1024**2


class RootCow:
    def __init__(self, manager):
        self.manager = manager
        self.path = manager.root / "root-lvm.json"
        self.lock = threading.RLock()
        self.last_action = None
        self.quiesced = set()

    def _observe(self):
        source = self.manager.run("findmnt", "--noheadings", "--output", "SOURCE", "--target", "/",
                                  timeout=3).stdout.strip()
        if not source.startswith("/dev/"):
            return None, []
        report = self.manager.run("lvs", "--reportformat", "json", "--units", "b", "--nosuffix",
                                  "--options", FIELDS, timeout=5).stdout
        rows = json.loads(report)["report"][0]["lv"]
        if not isinstance(rows, list):
            raise ValueError("invalid LVM report")
        root = [row for row in rows if Path(row.get("lv_path", "")).resolve() == Path(source).resolve()]
        if len(root) != 1:
            if not root and (source.startswith("/dev/mapper/") or Path(source).resolve().name.startswith("dm-")):
                raise ValueError("root LV not identified")
            return None, []
        root = root[0]
        for field in ("lv_uuid", "vg_uuid"):
            if not UUID.fullmatch(root.get(field, "")):
                raise ValueError("root identity missing")
        if self.path.exists():
            marker = json.loads(self.path.read_text())
            if marker != self._identity(root):
                raise ValueError("root identity changed")
        snapshots = []
        for row in rows:
            if row.get("origin_uuid", "").strip() != root["lv_uuid"] or row.get("vg_uuid") != root["vg_uuid"]:
                continue
            if not row.get("lv_attr", "").startswith(("s", "S")):
                continue  # Thin snapshots and unrelated LVs have a different capacity model.
            if not UUID.fullmatch(row.get("lv_uuid", "")):
                raise ValueError("snapshot identity missing")
            if not all(NAME.fullmatch(row.get(name, "")) for name in ("vg_name", "lv_name")):
                raise ValueError("snapshot name invalid")
            path = "/dev/" + row["vg_name"] + "/" + row["lv_name"]
            if row.get("lv_path") != path:
                raise ValueError("snapshot path invalid")
            attr = row["lv_attr"]
            if attr.startswith("S"):
                raise ValueError("snapshot merge in progress")
            invalid = len(attr) < 5 or attr[4] in {"I", "S", "m", "M"}
            try:
                percent = float(row["data_percent"])
                size, free, extent = (int(float(row[name])) for name in ("lv_size", "vg_free", "vg_extent_size"))
                if not math.isfinite(percent) or not 0 <= percent <= 100 or size <= 0 or free < 0 or extent <= 0:
                    raise ValueError("snapshot capacity invalid")
            except (ValueError, KeyError):
                if not invalid:
                    raise
                percent, size, free, extent = 100, 0, 0, 1
            invalid = invalid or percent >= 100
            snapshots.append({"id": row["lv_uuid"], "path": path, "percent": percent,
                              "size": size, "free": free, "extent": extent, "invalid": invalid})
        return root, snapshots

    @staticmethod
    def _identity(root):
        return {"schema": 1, "root_lv_uuid": root["lv_uuid"], "vg_uuid": root["vg_uuid"]}

    def _status(self, root, snapshots):
        maximum = max((item["percent"] for item in snapshots), default=0)
        if root is None or not snapshots:
            state, message = "not_applicable", "目前沒有根磁碟 LVM 還原點需要監控。"
        elif any(item["invalid"] for item in snapshots):
            state, message = "invalid", "磁碟還原點已失效，不能用來復原；已保留原檔，請聯絡支援協助復原。"
        elif maximum >= CRITICAL:
            state, message = "critical", "磁碟還原點空間不足；正在保護資料，已暫停新增備份。原有還原點不會自動刪除。"
        elif maximum >= WARN:
            state, message = "warning", f"磁碟還原點已使用 {maximum:.0f}%；系統會嘗試自動增加空間，不會刪除原有還原點。"
        else:
            state, message = "healthy", f"磁碟還原點空間正常（已使用 {maximum:.0f}%）。"
        return {"state": state, "message": message, "used_percent": maximum,
                "snapshot_count": len(snapshots), "warning_percent": WARN, "critical_percent": CRITICAL,
                "enrolled": self.path.exists(), "last_action": self.last_action,
                "limitations": ["持續的系統寫入仍可能用盡空間；空間不足時會停止新增備份並嘗試正常關閉 HA。"] if snapshots else []}

    def status(self):
        try:
            with self.lock:
                return self._status(*self._observe())
        except Exception:
            return {"state": "unavailable", "message": "暫時無法確認磁碟還原點空間；已停止新增備份以保護資料。",
                    "enrolled": self.path.exists(), "last_action": self.last_action}

    def _extend(self, snapshot, required=0):
        if snapshot["invalid"]:
            return
        used = snapshot["size"] * snapshot["percent"] / 100
        # Return below the warning threshold, including the anticipated copy.
        wanted = math.ceil((used + required) / 0.50) - snapshot["size"]
        if wanted <= 0:
            return
        extents = min(math.ceil(wanted / snapshot["extent"]), snapshot["free"] // snapshot["extent"])
        if extents <= 0:
            return
        # Re-identify the root and snapshot immediately before changing metadata.
        _, current = self._observe()
        match = next((item for item in current if item["id"] == snapshot["id"] and item["path"] == snapshot["path"]), None)
        if not match or match["invalid"]:
            raise HostError("磁碟還原點狀態已改變；已停止操作以保護資料。")
        extents = min(extents, match["free"] // match["extent"])
        if extents <= 0:
            return
        self.manager.run("lvextend", "--yes", "--extents", "+" + str(extents), "--", match["path"], timeout=30)
        _, observed = self._observe()
        grown = next((item for item in observed if item["id"] == match["id"]), None)
        if not grown or grown["invalid"] or grown["size"] < match["size"] + extents * match["extent"]:
            raise HostError("無法確認磁碟還原點已增加空間；已停止新增備份。")
        self.last_action = "已自動增加磁碟還原點空間。"

    def reconcile(self, required=0):
        with self.lock:
            root, snapshots = self._observe()
            if root is not None and not self.path.exists():
                atomic(self.path, self._identity(root))
            for snapshot in snapshots:
                if snapshot["percent"] >= WARN or required:
                    self._extend(snapshot, required)
            # A successful command alone never proves the snapshot gained space.
            return self._status(*self._observe())

    def require_capacity(self, required):
        if type(required) is not int or required < 0:
            raise HostError("Invalid snapshot capacity estimate")
        with self.lock:
            try:
                self.reconcile(required)
                _, snapshots = self._observe()
                for item in snapshots:
                    used = item["size"] * item["percent"] / 100
                    if item["invalid"] or (used + required) >= item["size"] * CRITICAL / 100:
                        raise ValueError("insufficient space")
            except Exception as exc:
                raise HostError("磁碟還原點空間不足或無法確認；已停止新增備份，原有還原點已保留。") from exc

    def require_copy_capacity(self, paths):
        # Sparse files consume allocated blocks; add metadata and concurrent-write headroom.
        allocated = sum(Path(path).stat().st_blocks * 512 for path in paths)
        self.require_capacity(allocated + allocated // 10 + MARGIN)

    def monitor(self, stop=None):
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                try:
                    self.reconcile()
                except Exception:
                    self.last_action = "無法自動增加磁碟還原點空間，正在檢查是否需要保護 HA。"
                _, snapshots = self._observe()
                critical = {item["id"] for item in snapshots if item["invalid"] or item["percent"] >= CRITICAL}
                if critical - self.quiesced:
                    # Fixed owned HA only, graceful ACPI shutdown; never force off or freeze root.
                    # Never interrupt a lifecycle transaction. locked() is nonblocking;
                    # the next observation retries protection after that job completes.
                    with self.manager.locked():
                        self.manager.shutdown()
                    self.quiesced.update(critical)
                    self.last_action = "磁碟還原點空間不足，HA 已正常關閉以減少寫入；請聯絡支援增加磁碟空間。"
            except Exception:
                self.last_action = "磁碟保護尚未完成；無法確認空間或正常關閉 HA，請聯絡支援。"
            stop.wait(5)
