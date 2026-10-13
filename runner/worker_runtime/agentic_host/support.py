"""Fixed, read-only outer-host diagnostics; independent of Controller and its Web UI.

Never export raw logs, exception strings, environment, URLs, user names or paths.
Only reviewed fields and finite state values leave each collector. This is a
local Owner console tool, not a general command endpoint for coding workers.
"""
import argparse
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

SERVICES = ("agentic-home.service", "agentic-home-host.service", "agentic-home-proxy.service",
            "agentic-home-firstboot.service", "docker.service", "libvirtd.service")
PROPERTIES = "LoadState,ActiveState,SubState,Result,NRestarts"
COMMANDS = {name: ("/usr/bin/systemctl", "show", "--no-pager", "--property=" + PROPERTIES, name)
            for name in SERVICES}
COMMANDS["vm"] = ("/usr/bin/virsh", "--readonly", "--connect", "qemu:///system", "domstate",
                  "agentic-home-haos")
COMMANDS["journal"] = ("/usr/bin/journalctl", "--quiet", "--no-pager", "--output=json",
                       "--output-fields=PRIORITY", "--lines=200",
                       *(part for name in SERVICES[:4] for part in ("--unit", name)))
MAX_OUTPUT = 65536
PROBE_SECONDS = 3
STATES = {
    "LoadState": {"loaded", "not-found", "error", "bad-setting", "masked", "stub", "merged"},
    "ActiveState": {"active", "reloading", "inactive", "failed", "activating", "deactivating",
                    "maintenance", "refreshing"},
    "SubState": {"running", "exited", "dead", "failed", "waiting", "start", "start-pre", "start-post",
                 "stop", "stop-sigterm", "stop-sigkill", "stop-post", "auto-restart", "reload"},
    "Result": {"success", "resources", "timeout", "exit-code", "signal", "core-dump", "watchdog",
               "start-limit-hit", "oom-kill", "exec-condition", "protocol"},
}
VM_STATES = {"running", "blocked", "paused", "in shutdown", "shut off", "crashed", "pmsuspended"}
PROFILE_IDS = {"laptop-amd64", "server-amd64", "laptop-new-amd64"}


def _capture(argv):
    """Capture a trusted fixed probe with a wall/output bound and no inherited secrets."""
    process = None
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, cwd="/", start_new_session=True,
                                   env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
        chunks, total, deadline = [], 0, time.monotonic() + PROBE_SECONDS
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "timeout", ""
                if not selector.select(remaining):
                    return "timeout", ""
                chunk = os.read(process.stdout.fileno(), 8192)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_OUTPUT:
                    return "output_limit", ""
                chunks.append(chunk)
        code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        return ("ok", b"".join(chunks).decode("utf-8", errors="replace")) if code == 0 else ("probe_failed", "")
    except FileNotFoundError:
        return "command_missing", ""
    except PermissionError:
        return "permission_denied", ""
    except subprocess.TimeoutExpired:
        return "timeout", ""
    except OSError:
        return "probe_failed", ""
    finally:
        if process is not None:
            # Terminate the entire probe group, including any inherited pipe writers.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdout.close()


def probe(name):
    if name not in COMMANDS:
        raise ValueError("Unknown support probe")
    return _capture(COMMANDS[name])


def _read(path, limit=32768):
    """Do not follow a replaced file into credentials or wait on a FIFO/device."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                return None
            data = stream.read(limit + 1)
            return data.decode("utf-8") if len(data) <= limit else None
    except (OSError, UnicodeError):
        return None


def _object(path):
    data = _read(path)
    try:
        value = json.loads(data) if data is not None else None
        return value if isinstance(value, dict) else None
    except (ValueError, RecursionError):
        return None


def _version(value):
    # Numerical product/OS versions only; never forward arbitrary manifest strings.
    return value if isinstance(value, str) and re.fullmatch(r"[0-9]{1,4}(?:\.[0-9]{1,4}){1,3}", value) else None


def _service(output):
    fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    if not all(name in fields for name in ("LoadState", "ActiveState")):
        return None
    result = {name: fields.get(name) if fields.get(name) in values else "unknown"
              for name, values in STATES.items()}
    restarts = fields.get("NRestarts", "")
    result["NRestarts"] = int(restarts) if re.fullmatch(r"[0-9]{1,9}", restarts) else None
    return result


def _journal(output):
    counts = dict.fromkeys(("critical", "error", "warning", "notice", "info", "debug", "unknown"), 0)
    names = {"0": "critical", "1": "critical", "2": "critical", "3": "error", "4": "warning",
             "5": "notice", "6": "info", "7": "debug"}
    for line in output.splitlines()[:200]:
        try:
            value = json.loads(line)
        except (ValueError, RecursionError):
            return None
        priority = value.get("PRIORITY") if isinstance(value, dict) else None
        counts[names.get(priority, "unknown") if isinstance(priority, str) else "unknown"] += 1
    return {"sampled_entries": sum(counts.values()), "limit": 200, "severity_counts": counts}


def collect(*, root=Path("/var/lib/agentic-home"), os_release=Path("/usr/lib/os-release"), execute=probe):
    root = Path(root)
    observed_at = datetime.now(UTC).isoformat()
    installed = _object(root / "installed.json")
    installed_version = _version((installed or {}).get("version"))
    version_refs = {"support_format": 1, "installed_application": installed_version}
    observations = []

    def add(target, method, data, reason="ok", limitations=()):
        content = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(content).hexdigest()
        observations.append({"evidence_id": "support-" + str(len(observations) + 1),
                             "observed_from": "outer_host_console", "target_component": target,
                             "observed_at": observed_at, "version_refs": version_refs,
                             "collection_method": method, "status": "collected" if reason == "ok" else "not_available",
                             "reason": reason, "content_digest": digest, "data": data,
                             "redaction": "reviewed_fields_only_no_raw_logs",
                             "limitations": list(limitations)})

    profile = (installed or {}).get("profile")
    add("installation", "fixed_installed_manifest_fields",
        {"version": installed_version, "profile": profile if isinstance(profile, str) and profile in PROFILE_IDS else None},
        "ok" if installed is not None else "manifest_unavailable",
        ["Manifest is local installation metadata, not proof of running service health or publisher authenticity."])
    os_fields = dict(line.split("=", 1) for line in (_read(os_release) or "").splitlines() if "=" in line)
    os_id = os_fields.get("ID", "").strip('"')
    add("host_os", "fixed_os_release_fields",
        {"id": os_id if os_id in {"ubuntu", "debian"} else "unknown",
         "version": _version(os_fields.get("VERSION_ID", "").strip('"'))},
        "ok" if os_fields else "os_release_unavailable")
    try:
        usage = shutil.disk_usage(root)
        disk = {"total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free}
    except OSError:
        disk = None
    add("product_storage", "statvfs_product_data_filesystem", disk,
        "ok" if disk is not None else "storage_unavailable",
        ["Filesystem capacity only; no disk serial, user paths or partition inventory are exported."])
    for name in SERVICES:
        reason, raw = execute(name)
        data = _service(raw) if reason == "ok" else None
        add(name, "systemctl_fixed_state_properties", data,
            reason if reason != "ok" else ("ok" if data is not None else "invalid_output"),
            ["Systemd unit state is not a Controller, Hermes, container or model health check."])
    state = _object(root / "host/state.json") or {}
    reason, raw = execute("vm")
    vm = raw.strip() if reason == "ok" and raw.strip() in VM_STATES else None
    add("managed_haos_vm", "libvirt_readonly_fixed_domain_state",
        {"state": vm, "provisioned": state.get("complete") is True,
         "restore_pending": bool(state.get("restore_pending"))},
        reason if reason != "ok" else ("ok" if vm else "invalid_output"),
        ["Observed from outer libvirt; does not establish Core, Supervisor or HAOS App health."])
    reason, raw = execute("journal")
    data = _journal(raw) if reason == "ok" else None
    add("product_service_log_severity", "journal_priority_only_last_200", data,
        reason if reason != "ok" else ("ok" if data is not None else "invalid_output"),
        ["Aggregate severity from accessible product units only; messages, URLs and private logs are omitted.",
         "Zero entries does not prove no errors; journal access and retention can limit the sample."])
    return {"format": "agentic-support-v1", "collected_at": observed_at,
            "runtime_location": "outer_host_console", "model_backend": "none", "ha_backend": "none",
            "device_kind": "none", "live_effects": [], "observations": observations,
            "limitations": ["No Controller Web, model account, HA API or network probes are used.",
                            "This report does not verify the hardware pilot or any E0/E1/E2 acceptance case."]}


def bundle(destination):
    report = collect()
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    document = {"format": "agentic-support-bundle-v1", "payload_sha256": hashlib.sha256(payload).hexdigest(),
                "report": report}
    # Exclusive creation also rejects symlinks; do not overwrite an Owner's files.
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(document, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return document["payload_sha256"]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only local diagnostics; no Web or model required")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("bundle").add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            print(json.dumps(collect(), ensure_ascii=False, indent=2))
        else:
            print(json.dumps({"state": "written", "payload_sha256": bundle(args.destination)}))
    except OSError:
        print("Support output could not be created; use a new file in an accessible directory.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
