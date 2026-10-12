"""Trusted root-only, disk-backed ext4 capacity boundary shared by fixed jobs."""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from .coding_sandbox import SandboxError, command


class DiskVolume:
    def __init__(self, root: Path, name: str, size_mib: int, *, execute=command):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{7,63}", name) or type(size_mib) is not int or not 64 <= size_mib <= 2048:
            raise SandboxError("Invalid fixed disk allocation")
        self.root = Path(root).absolute()
        self.directory = self.root / name
        self.mount = self.directory / "workspace"
        self.disk = self.directory / "workspace.ext4"
        self.size_mib, self.execute = size_mib, execute

    def create(self):
        if os.geteuid() != 0:
            raise SandboxError("Root broker required", code="root_broker_required")
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.root.resolve() != self.root:
            raise SandboxError("Unsafe volume root")
        for directory in (self.root, *self.root.parents):
            metadata = directory.stat()
            if metadata.st_uid != 0 or metadata.st_mode & 0o022:
                raise SandboxError("Unsafe volume ancestor")
        fs = self.execute("findmnt", "-n", "-o", "FSTYPE", "-T", str(self.root)).stdout.strip()
        if fs not in {"ext4", "xfs", "btrfs"}:
            raise SandboxError("Disk backing required", code="disk_backing_required")
        if shutil.disk_usage(self.root).free < self.size_mib * 1024**2 + 4 * 1024**3:
            raise SandboxError("Host disk reserve required", code="host_reserve_insufficient")
        self.directory.mkdir(mode=0o700)
        self.mount.mkdir(mode=0o700)
        fd = os.open(self.disk, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            os.posix_fallocate(fd, 0, self.size_mib * 1024**2)
        finally:
            os.close(fd)
        self.execute("mkfs.ext4", "-q", "-F", "-m", "0", "-O", "^has_journal",
                     "-E", "nodiscard,lazy_itable_init=0", str(self.disk))
        self.execute("mount", "-o", "loop,nodev,nosuid", str(self.disk), str(self.mount))
        if (not os.path.ismount(self.mount) or
            os.statvfs(self.mount).f_blocks * os.statvfs(self.mount).f_frsize > self.disk.stat().st_size):
            raise SandboxError("Bounded mount unverified", code="workspace_mount_unverified")
        self.mount.chmod(0o755)
        return self.mount

    def close(self):
        """Caller first proves every writer stopped; unmount failure preserves evidence."""
        if self.directory.is_symlink() or self.mount.is_symlink():
            raise SandboxError("Unsafe volume cleanup")
        if os.path.ismount(self.mount):
            self.execute("umount", str(self.mount))
        if os.path.ismount(self.mount):
            raise SandboxError("Workspace still mounted")
        if self.directory.exists():
            shutil.rmtree(self.directory)
