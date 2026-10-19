"""Fixed labelled USB data, authenticated by a small installed root manifest.

VFAT ownership is deliberately not trusted. No media code is executed; bounded
regular files are hashed and copied into root-private storage before parsing.
"""
import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path

from .host_update import MAX_ARCHIVE, read_json, secure
from .manager import HostError, sync_path

LABEL = "AH-UPDATE"
DIRECTORY = "agentic-home-update"
FILES = {"host-bundle.tar": MAX_ARCHIVE, "controller-images.tar": 16 * 1024**3}
INSERT_USB = "請插入官方更新 USB（AH-UPDATE），系統會自動尋找並核對更新檔案。"
DEVICE = re.compile(r"/dev/(?:sd[a-z]+[0-9]+|nvme[0-9]+n[0-9]+p[0-9]+|mmcblk[0-9]+p[0-9]+)\Z")
SMALL_LIMIT = 16 * 1024**2
PREINST = '''#!/usr/bin/python3 -I
import json, math, os, pathlib, subprocess, sys
try:
    required = 80 * 1024**2
    disk = os.statvfs("/")
    if disk.f_bavail * disk.f_frsize < required:
        raise ValueError()
    def command(*args):
        return subprocess.run(args, check=True, capture_output=True, text=True, timeout=10).stdout
    source = pathlib.Path(command("/usr/bin/findmnt", "-n", "-o", "SOURCE", "-T", "/").strip()).resolve()
    rows = json.loads(command("/usr/sbin/lvs", "--reportformat", "json", "--units", "b", "--nosuffix", "-o",
        "lv_uuid,vg_uuid,lv_path,origin_uuid,lv_attr,data_percent,lv_size"))["report"][0]["lv"]
    roots = [row for row in rows if pathlib.Path(row.get("lv_path", "")).resolve() == source]
    if len(roots) != 1 or not roots[0].get("lv_uuid") or not roots[0].get("vg_uuid"):
        raise ValueError()
    root = roots[0]
    for row in rows:
        if row.get("origin_uuid") != root["lv_uuid"] or row.get("vg_uuid") != root["vg_uuid"]:
            continue
        attr = row.get("lv_attr", "")
        percent, size = float(row["data_percent"]), float(row["lv_size"])
        if (not attr.startswith("s") or len(attr) < 5 or attr[4] != "a" or not math.isfinite(percent)
                or not math.isfinite(size) or size <= 0 or not 0 <= percent < 80
                or size * (80 - percent) / 100 < required):
            raise ValueError()
except Exception:
    sys.exit("主機磁碟或還原點空間無法安全確認，尚未安裝更新工具；請聯絡支援。")
'''


def validate_manifest(value):
    if (not isinstance(value, dict) or set(value) != {"format", "label", "directory", "files"}
            or type(value["format"]) is not int or value["format"] != 1
            or value["label"] != LABEL or value["directory"] != DIRECTORY or not isinstance(value["files"], dict)
            or set(value["files"]) != set(FILES)):
        raise HostError("更新 USB 的可信檔案清單無效，尚未變更主機。")
    for name, maximum in FILES.items():
        item = value["files"][name]
        if (not isinstance(item, dict) or set(item) != {"size", "sha256"} or type(item["size"]) is not int
                or not 0 < item["size"] <= maximum or not isinstance(item["sha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])):
            raise HostError("更新 USB 的可信檔案清單無效，尚未變更主機。")
    return value


class BootstrapMedia:
    def __init__(self, manager, manifest):
        self.manager = manager
        self.manifest = validate_manifest(read_json(manifest, 16384))

    def locate(self):
        raw = self.manager.run("/usr/bin/lsblk", "--json", "--paths", "--output", "NAME,LABEL,TYPE,RM,TRAN", timeout=10).stdout
        if len(raw) > 131072:
            raise HostError(INSERT_USB)
        rows = json.loads(raw).get("blockdevices")
        candidates, count = [], 0
        def visit(items, removable=False):
            nonlocal count
            if not isinstance(items, list):
                raise HostError(INSERT_USB)
            for item in items:
                count += 1
                if not isinstance(item, dict) or count > 256:
                    raise HostError(INSERT_USB)
                external = removable or item.get("rm") in {True, "1"} or item.get("tran") == "usb"
                if item.get("label") == LABEL:
                    if not external or item.get("type") != "part" or not DEVICE.fullmatch(item.get("name", "")):
                        raise HostError(INSERT_USB)
                    candidates.append(item["name"])
                if item.get("children"):
                    visit(item["children"], external)
        visit(rows)
        if len(candidates) != 1:
            raise HostError(INSERT_USB)
        device = candidates[0]
        def mounted():
            result = self.manager.run("/usr/bin/findmnt", "--json", "--source", device, "--output", "SOURCE,TARGET", check=False, timeout=10)
            if result.returncode:
                return None
            if len(result.stdout) > 16384:
                raise HostError(INSERT_USB)
            entries = json.loads(result.stdout).get("filesystems")
            if not isinstance(entries, list) or len(entries) != 1 or entries[0].get("source") != device:
                raise HostError(INSERT_USB)
            target = Path(entries[0].get("target", ""))
            if (not target.is_absolute() or ".." in target.parts
                    or not any(target.is_relative_to(base) for base in ("/media", "/run/media"))):
                raise HostError(INSERT_USB)
            return target
        root = mounted()
        if root is None:
            self.manager.run("/usr/bin/udisksctl", "mount", "--block-device", device,
                             "--options", "ro,nodev,nosuid,noexec", "--no-user-interaction", timeout=30)
            root = mounted()
        if root is None:
            raise HostError(INSERT_USB)
        return root

    @contextmanager
    def _source(self, root, name):
        # Resolve every component with nofollow dirfds: an owner-writable mount
        # path or VFAT file can never redirect privileged reads into host files.
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in (*Path(root).parts[1:], DIRECTORY):
                next_descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
            with os.fdopen(source, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size != self.manifest["files"][name]["size"]:
                    raise HostError("更新 USB 檔案類型或大小不符，尚未變更主機。")
                yield stream
        finally:
            os.close(descriptor)

    def _read(self, root, name, output=None):
        expected = self.manifest["files"][name]
        total, value = 0, hashlib.sha256()
        with self._source(root, name) as stream:
            while chunk := stream.read(min(1024 * 1024, expected["size"] + 1 - total)):
                total += len(chunk)
                if total > expected["size"]:
                    raise HostError("更新 USB 檔案大小已變更，已停止處理。")
                value.update(chunk)
                if output is not None:
                    output.write(chunk)
        if total != expected["size"] or value.hexdigest() != expected["sha256"]:
            raise HostError("更新 USB 檔案未通過可信清單核對，尚未解析或套用。")

    def inspect(self, *, verify=True):
        root = self.locate()
        for name in FILES:
            if verify:
                self._read(root, name)
            else:
                with self._source(root, name):
                    pass
        return self.manifest

    def copy(self, destination):
        destination = Path(destination)
        secure(destination, optional=True)
        destination.mkdir(parents=True, mode=0o700, exist_ok=True)
        secure(destination)
        destination.chmod(0o700)
        root = self.locate()
        result = {}
        for name in FILES:
            target = destination / name
            secure(target, optional=True)
            temporary = target.with_name(target.name + ".partial")
            secure(temporary, optional=True)
            if target.exists() or temporary.exists():
                raise HostError("先前更新檔案複製結果尚未確認，不會覆寫；請聯絡支援。")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                self._read(root, name, output)
                output.flush()
                os.fsync(output.fileno())
            os.rename(temporary, target)
            sync_path(destination)
            result[name] = target
        return result
