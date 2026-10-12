"""Kernel identity proof for a quarantined fixed CLI process namespace."""
import os
from pathlib import Path


def runtime_identity():
    return {"boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "pid_namespace": os.stat("/proc/self/ns/pid").st_ino,
            "init_start": int(Path("/proc/1/stat").read_text().rsplit(") ", 1)[1].split()[19])}


def provider_processes_absent(uids):
    for path in Path("/proc").iterdir():
        if not path.name.isdecimal():
            continue
        try:
            if path.stat().st_uid in uids:
                fields = (path / "stat").read_text().rsplit(") ", 1)[1].split()
                if fields[0] not in {"Z", "X"}:
                    return False
        except FileNotFoundError:
            continue
    return True


def local_proof(previous, current, uids):
    if not isinstance(previous, dict) or not all(k in previous for k in ("boot_id", "pid_namespace", "init_start")):
        return False
    if previous["boot_id"] != current["boot_id"]:
        return True  # An earlier boot cannot retain a process on this kernel.
    if any(previous[k] != current[k] for k in ("pid_namespace", "init_start")):
        return False  # Another container with the same volume might still be alive.
    return provider_processes_absent(uids)
