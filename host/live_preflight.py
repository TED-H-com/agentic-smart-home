#!/usr/bin/env python3
"""Check live-installer hardware before Subiquity reaches disk configuration.

This standalone, standard-library-only file also runs on Ubuntu's live Python.
It never selects, opens or writes an installation disk. Loading one fixed KVM
kernel module is the only persistent-in-this-boot preparation it may perform.
"""
import argparse
import fcntl
import os
import platform
import re
import stat
import subprocess
import sys
import time
from pathlib import Path


class PreflightError(Exception):
    pass


def read_system_file(path, limit):
    with Path(path).open("rb") as stream:
        return stream.read(limit).decode("ascii", errors="replace")


def kvm_ready():
    """Check the real API and permission to create a VM; retain no VM or disk."""
    descriptor = None
    try:
        descriptor = os.open("/dev/kvm", os.O_RDWR | os.O_CLOEXEC)
        if not stat.S_ISCHR(os.fstat(descriptor).st_mode):
            return False
        if fcntl.ioctl(descriptor, 0xAE00, 0) != 12:  # KVM_GET_API_VERSION
            return False
        vm = fcntl.ioctl(descriptor, 0xAE01, 0)  # KVM_CREATE_VM, no vCPU or memory
        os.close(vm)
        return True
    except OSError:
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def prepare_kvm():
    if kvm_ready():
        return True
    # The live kernel need not have loaded KVM yet. CPU flags choose only a
    # fixed vendor module; no command, module name or path comes from the user.
    try:
        cpu = read_system_file("/proc/cpuinfo", 1048576)
    except OSError:
        return False
    module = ("kvm_intel" if re.search(r"(?m)^flags\s*:.*\bvmx\b", cpu) else
              "kvm_amd" if re.search(r"(?m)^flags\s*:.*\bsvm\b", cpu) else None)
    if module is None:
        return False
    executable = next((path for path in ("/usr/sbin/modprobe", "/sbin/modprobe")
                       if os.access(path, os.X_OK)), None)
    if executable is None:
        return False
    try:
        result = subprocess.run([executable, "--", module], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode:
        return False
    deadline = time.monotonic() + 2
    while True:
        if kvm_ready():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def check(minimum_memory_mib):
    if platform.machine() != "x86_64":
        raise PreflightError("This USB installer requires an Intel/AMD x86-64 computer; ARM is not supported.")
    try:
        memory = re.search(r"(?m)^MemTotal:\s+(\d+)\s+kB$", read_system_file("/proc/meminfo", 65536))
        if memory is None:
            raise ValueError("missing_memory")
        memory_mib = int(memory[1]) // 1024
    except (OSError, ValueError):
        raise PreflightError("Cannot read installed RAM. Restart using this USB's normal installation entry.") from None
    if memory_mib < minimum_memory_mib:
        raise PreflightError(f"Available system RAM is {memory_mib} MiB; this profile requires at least "
                             f"{minimum_memory_mib} MiB. Use more RAM; 16 GB is recommended.")
    if not prepare_kvm():
        raise PreflightError("Hardware virtualization is unavailable. Enable Intel VT-x / AMD-V in UEFI/BIOS "
                             "and restart this USB. If testing in a VM, enable nested virtualization.")
    return memory_mib


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minimum-memory-mib", type=int, required=True)
    args = parser.parse_args(argv)
    if not 2048 <= args.minimum_memory_mib <= 1048576:
        parser.error("minimum-memory-mib is outside the supported range")
    try:
        memory_mib = check(args.minimum_memory_mib)
    except PreflightError as error:
        print("Agentic Home hardware check failed before installation disk configuration.\n"
              "No installation disk has been selected or written by this check.\n"
              + str(error) + "\nCorrect the issue, then restart the USB installer.", file=sys.stderr)
        return 1
    print(f"Agentic Home hardware check passed: x86-64, {memory_mib} MiB RAM, working KVM.\n"
          "Continue with network, installation disk and Linux account selection.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
