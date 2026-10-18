"""Full Linux installation rehearsal. Every disk/credential is temporary and isolated."""
import base64
import contextlib
import hashlib
import importlib.util
import inspect
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path

import pytest
from installed_coding_probe import CLEANUP_WAIT_SECONDS, JOB_WAIT_SECONDS
from installed_probe import HTTP_TIMEOUT_SECONDS
from installed_sandbox_probe import CLEANUP_ALARM_SECONDS, PROBE_ALARM_SECONDS
from installed_update_probe import COMMAND_TIMEOUT_SECONDS as UPDATE_COMMAND_TIMEOUT_SECONDS
from installed_update_probe import RUNTIME_MAX_SECONDS

ROOT = Path(__file__).parents[1]
# Each SSH bound must outlast the probe's own bounds; a slow test disk stretches every step.
AUTH_PROBE_TIMEOUT_SECONDS = HTTP_TIMEOUT_SECONDS * 4
SANDBOX_PROBE_TIMEOUT_SECONDS = PROBE_ALARM_SECONDS + CLEANUP_ALARM_SECONDS + 120
CODING_PROBE_TIMEOUT_SECONDS = JOB_WAIT_SECONDS + CLEANUP_WAIT_SECONDS + 240
FAILURE_DIAGNOSTICS_TIMEOUT_SECONDS = UPDATE_COMMAND_TIMEOUT_SECONDS + 60
FAILURE_DIAGNOSTICS_ATTEMPTS = 3
FAILURE_DIAGNOSTIC_PROBE_SECONDS = 15
FIRSTBOOT_CONNECT_SECONDS = 20
FIRSTBOOT_PROBE_SECONDS = 60
FIRSTBOOT_MAX_OBSERVATIONS = 721
UPDATE_INVOKE_TIMEOUT_SECONDS = UPDATE_COMMAND_TIMEOUT_SECONDS + 60
UPDATE_DEADLINE_SECONDS = RUNTIME_MAX_SECONDS + 300
INSTALL_CLEANUP_COMMAND_SECONDS = 30
INSTALL_CONSOLE_BYTES = 65536
INSTALL_DISPOSABLE_IMAGES = ("linux.qcow2", "test.iso", "nvram.fd")
LAN_LAB_IMAGE = "agentic-home-lan-lab:0.11.0"
INSTALL_GUEST_MEMORY_MIB = 10240
# The nested fixture needs page-cache/QEMU headroom above the 10 GiB guest.
# The previous 12 GiB cgroup swapped QEMU despite spare physical host memory.
# These are test-container limits, not a changed guest or product requirement.
INSTALL_CONTAINER_MEMORY_GIB = 16
INSTALL_CONTAINER_MEMORY_SWAP_GIB = 24
REBOOT_OBSERVATION_SECONDS = 15
REBOOT_BASELINE_SECONDS = 60
REBOOT_REQUEST_SECONDS = 30
REBOOT_ERRORS = {"outer_reboot_baseline_unavailable", "outer_reboot_request_failed",
                 "outer_reboot_new_boot_not_ready"}
OUTER_BOOT_OBSERVATION_SCRIPT = """import json,subprocess
from pathlib import Path
try:
    path=Path('/proc/sys/kernel/random/boot_id')
    before=path.read_text().strip()
    active=subprocess.run(['systemctl','is-active','agentic-home'],
                          stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5).returncode == 0
    after=path.read_text().strip()
    if before != after:
        raise ValueError('boot_changed_during_observation')
    print(json.dumps({'boot_id':after,'service_active':active}),flush=True)
except Exception:
    raise SystemExit(1) from None
"""


def provider_policy_seed():
    """Set the fixture's administration policy before any installed service starts.

    The runtime file survives firstboot, reboots and Controller updates. Product
    payloads and image IDs remain exact; the excluded provider is never tested.
    """
    return [
        "install -d -m 0700 /target/var/lib/agentic-home/host",
        "printf 'AGENTIC_DISABLED_PROVIDERS=grok\\n' > /target/var/lib/agentic-home/host/runtime.env",
        "chmod 0600 /target/var/lib/agentic-home/host/runtime.env",
    ]


def cleanup_installer(name, temporary, console_path, *, run=None):
    """Confirm owned VM absence; retain console capture as a separate typed step."""
    run = run or subprocess.run
    result = {"status": "failed", "steps": {}}

    def step(label, args, seconds=INSTALL_CLEANUP_COMMAND_SECONDS, **options):
        try:
            completed = run(args, timeout=seconds, stderr=subprocess.DEVNULL,
                            stdout=options.pop("stdout", subprocess.DEVNULL), **options)
            code = completed.returncode
            result["steps"][label] = {"status": "passed" if type(code) is int and code == 0 else "failed",
                "exit_code": code if type(code) is int and -128 <= code <= 255 else None}
            return completed
        except Exception as error:
            result["steps"][label] = {"status": "timeout" if isinstance(error, subprocess.TimeoutExpired) else "failed",
                "error_type": type(error).__name__ if type(error).__name__ in {
                    "TimeoutExpired", "PermissionError", "FileNotFoundError", "OSError"} else "OtherError"}
            return None

    step("stop_vm", ["docker", "stop", "-t", "10", name])
    try:
        serial = temporary / "serial.log"
        if serial.is_symlink():
            raise OSError("console is not a regular owned file")
        if serial.is_file():
            descriptor = os.open(console_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                if not stat.S_ISREG(os.fstat(output.fileno()).st_mode):
                    raise OSError("console destination is not a regular file")
                os.fchmod(output.fileno(), 0o600)
                step("console_tail", ["tail", "-c", str(INSTALL_CONSOLE_BYTES), "--", str(serial)], 10, stdout=output)
        else:
            result["steps"]["console_tail"] = {"status": "not_present"}
    except Exception:
        result["steps"]["console_tail"] = {"status": "failed", "error_type": "OtherError"}
    step("remove_vm", ["docker", "rm", "-fv", name])
    observed = step("vm_absent", ["docker", "ps", "-a", "--filter", "name=^/" + name + "$",
                                 "--format", "{{.Names}}"], stdout=subprocess.PIPE)
    if observed is not None and type(observed.returncode) is int and observed.returncode == 0:
        result["steps"]["vm_absent"]["status"] = "passed" if (
            isinstance(observed.stdout, bytes) and not observed.stdout.strip()) else "failed"
    # A failed stop/remove may still end in proven absence. Console capture is
    # diagnostic retention, not a condition for declaring the resource absent.
    if result["steps"]["vm_absent"]["status"] == "passed":
        result["status"] = "passed"
        # Fixed direct entries only: no recursion, globbing or symlink following.
        # Root-owned images can be unlinked from the user-owned directory.
        artifacts = {}
        for filename in INSTALL_DISPOSABLE_IMAGES:
            try:
                (temporary / filename).unlink()
                artifacts[filename] = "removed"
            except FileNotFoundError:
                artifacts[filename] = "not_present"
            except OSError:
                artifacts[filename] = "failed"
        result["steps"]["remove_images"] = {"status": (
            "failed" if "failed" in artifacts.values() else "passed"), "artifacts": artifacts}
    else:
        result["steps"]["remove_images"] = {"status": "not_run"}
    return result


def finish_installer_cleanup(name, temporary, console_path, report, *, primary_error=None, run=None):
    """Receipt failure cannot skip cleanup or replace an active test exception."""
    result = cleanup_installer(name, temporary, console_path, run=run)
    try:
        report.write_text(json.dumps(result, indent=2) + "\n")
    except Exception:
        result["receipt"] = "write_failed"
        result["status"] = "failed"
    if result["status"] != "passed":
        if primary_error is not None:
            with contextlib.suppress(Exception):
                primary_error.add_note("Disposable installer cleanup incomplete; bounded cleanup was attempted.")
        else:
            raise AssertionError("Disposable installer cleanup incomplete")
    return result


def bootstrap_job_diagnostics(output):
    """Keep only fixed job status and known codes, never CLI text or paths."""
    from installed_update_probe import lifecycle_error

    result = {"state": "unknown", "error_code": "unclassified_host_error"}
    try:
        if len(output.encode()) > 65536:
            return result
        value = json.loads(output[output.index("{"):])
        if value.get("state") in {"running", "succeeded", "failed", "needs_reconcile"}:
            result["state"] = value["state"]
        if code := lifecycle_error(value.get("message")):
            result["error_code"] = code
        elif result["state"] == "succeeded":
            result["error_code"] = None
    except (ValueError, TypeError, AttributeError):
        pass
    return result


def bootstrap_record_diagnostics(raw):
    """Project fixed private setup diagnostics; reject arbitrary nested values."""
    import json

    invalid = {"observation": "invalid"}
    if not isinstance(raw, (bytes, str)) or len(raw) > 16384:
        return invalid
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if len(raw.encode("utf-8")) > 16384:
            return invalid

        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate_key")
                value[key] = item
            return value

        value = json.loads(raw, object_pairs_hook=unique)
    except (UnicodeError, ValueError, TypeError, RecursionError):
        return invalid
    if not isinstance(value, dict):
        return invalid
    names = {"diagnostic", "fallback_diagnostic", "probe_diagnostic"}
    if set(value) == {"observation"}:
        return value if isinstance(value["observation"], str) and value["observation"] in {
            "absent", "invalid", "unavailable", "timeout"} else invalid
    if not names.intersection(value):
        return {"observation": "absent"}
    phases = {"workspace_preparation", "image_inspection", "probe_preparation", "offline_probe",
              "runtime_stop", "account_recheck", "environment_activation", "runtime_activation",
              "state_commit", "temporary_cleanup", "state_readback"}
    reasons = {"timeout", "command_failed", "invalid_contract", "state_conflict", "filesystem_error", "unexpected_error"}
    exits = {"unavailable", "success", "rejected", "docker_error", "command_not_executable",
             "command_not_found", "killed", "terminated", "nonzero", "signal"}
    stages = {"consumed_signatures", "chat_tool_roundtrip", "continued_history", "provider_401",
              "native_device_oauth", "native_responses_tool_roundtrip", "fixture_finalization"}
    kinds = {"child_timeout", "child_exit", "invalid_output", "contract_failure", "fixture_protocol", "unexpected"}
    result = {"observation": "ok"}
    for name in sorted(names.intersection(value)):
        item = value[name]
        if not isinstance(item, dict) or type(item.get("schema")) is not int or item["schema"] != 1:
            return invalid
        fields = ({"stage": stages, "kind": kinds} if name == "probe_diagnostic" else
                  {"phase": {"fallback_environment", "fallback_activation"} if name == "fallback_diagnostic" else phases,
                   "reason": reasons, "exit_category": exits})
        if set(item) != {"schema", *fields} or any(
                not isinstance(item[key], str) or item[key] not in choices for key, choices in fields.items()):
            return invalid
        result[name] = item
    return result


def observe_bootstrap_diagnostics(ssh, *, run=None):
    """One bounded read before VM cleanup; never capture the private record itself."""
    run = run or subprocess.run
    script = "import json\nfrom pathlib import Path\n" + inspect.getsource(bootstrap_record_diagnostics) + '''
try:
    path = Path('/var/lib/agentic-home/host/bootstrap-hermes.json')
    if path.is_symlink():
        result = {"observation": "invalid"}
    else:
        with path.open('rb') as stream:
            result = bootstrap_record_diagnostics(stream.read(16385))
except OSError:
    result = {"observation": "unavailable"}
print(json.dumps(result))
'''
    try:
        completed = run([*ssh, "sudo python3 -"], input=script, text=True, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        return {"observation": "timeout"}
    except UnicodeError:
        return {"observation": "invalid"}
    except OSError:
        return {"observation": "unavailable"}
    if completed.returncode != 0:
        return {"observation": "unavailable"}
    return bootstrap_record_diagnostics(completed.stdout)


def retain_bootstrap_result(ssh, latest_result, iso_hash, report):
    """Persist safe failure details before a subsequent status transport can fail."""
    receipt = {"runtime_location": "disposable_outer_linux_vm", "iso_sha256": iso_hash,
               "cli_exit": latest_result.returncode, "job": bootstrap_job_diagnostics(latest_result.stdout),
               "diagnostics": observe_bootstrap_diagnostics(ssh), "cloud_login": "not_run"}
    report.write_text(json.dumps(receipt, indent=2) + "\n")
    observed = json.loads(command(*ssh, "agentic-home status"))["bootstrap"]["hermes"]
    receipt["observed"] = observed
    report.write_text(json.dumps(receipt, indent=2) + "\n")
    return observed


def command(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs).stdout.strip()


def lan_probe_diagnostics(output, expected_phase):
    """Accept only the fixed probe's bounded failure contract, never its streams."""
    from installed_ha_lan_probe import validate_failure

    if isinstance(output, bytes):
        if len(output) > 8192:
            return None
        try:
            output = output.decode("utf-8")
        except UnicodeError:
            return None
    if not isinstance(output, str) or len(output) > 8192:
        return None
    try:
        if len(output.encode()) > 8192:
            return None
    except UnicodeError:
        return None

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result

    try:
        value = validate_failure(json.loads(output, object_pairs_hook=unique_object))
    except (ValueError, TypeError, RecursionError):
        return None
    if value is None or value["phase"] != expected_phase:
        return None
    return value


def observe_outer_boot(ssh, timeout, *, run=None):
    """One fixed read-only probe; transport loss and malformed output are not ready."""
    run = run or subprocess.run
    try:
        result = run([*ssh, "python3 -"], input=OUTER_BOOT_OBSERVATION_SCRIPT,
                     text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0 or not isinstance(result.stdout, str) or len(result.stdout) > 512:
        return None
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError):
        return None
    if (not isinstance(value, dict) or set(value) != {"boot_id", "service_active"}
            or not isinstance(value["boot_id"], str)
            or not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", value["boot_id"])
            or value["boot_id"] == "00000000-0000-0000-0000-000000000000"
            or type(value["service_active"]) is not bool):
        return None
    return value


def reboot_outer(ssh, seconds, *, run=None, clock=time.monotonic, pause=time.sleep):
    """Issue one reboot, then prove a different boot and active service together.

    The shared wall-clock budget includes baseline, the single write and every
    observation. A timed-out/disconnected write may have executed: never retry it.
    No boot identifiers or raw SSH output enter the returned public evidence.
    """
    if type(seconds) not in (int, float) or not 0 < seconds <= 3600:
        raise ValueError("invalid_reboot_budget")
    run = run or subprocess.run
    started = clock()
    deadline = started + seconds
    evidence = {"boot_id_changed": False, "service_active_in_new_boot": False,
                "reboot_requests": 0, "observations": 0, "unavailable_observations": 0,
                "same_boot_observations": 0, "new_boot_not_ready_observations": 0}

    def observe(until):
        remaining = until - clock()
        if remaining <= 0:
            return None
        evidence["observations"] += 1
        value = observe_outer_boot(ssh, min(REBOOT_OBSERVATION_SECONDS, remaining), run=run)
        if value is None:
            evidence["unavailable_observations"] += 1
        return value if clock() < until else None

    def wait(until):
        pause(min(5, max(0, until - clock())))

    baseline_deadline = min(deadline, started + REBOOT_BASELINE_SECONDS)
    before = None
    while clock() < baseline_deadline:
        before = observe(baseline_deadline)
        if before is not None:
            break
        wait(baseline_deadline)
    if before is None:
        raise AssertionError("outer_reboot_baseline_unavailable")

    remaining = deadline - clock()
    if remaining <= 0:
        raise AssertionError("outer_reboot_new_boot_not_ready")
    evidence["reboot_requests"] = 1
    try:
        requested = run([*ssh, "sudo systemctl reboot"], capture_output=True, text=True,
                        timeout=min(REBOOT_REQUEST_SECONDS, remaining))
    except subprocess.TimeoutExpired:
        evidence["request_result"] = "timeout"
    else:
        if requested.returncode not in (0, 255):
            raise AssertionError("outer_reboot_request_failed")
        evidence["request_result"] = "accepted" if requested.returncode == 0 else "transport_disconnected"

    while clock() < deadline:
        observed = observe(deadline)
        if observed is not None:
            if observed["boot_id"] == before["boot_id"]:
                evidence["same_boot_observations"] += 1
            elif observed["service_active"]:
                evidence.update(boot_id_changed=True, service_active_in_new_boot=True,
                                duration_seconds=round(clock() - started))
                return evidence
            else:
                evidence["new_boot_not_ready_observations"] += 1
        wait(deadline)
    raise AssertionError("outer_reboot_new_boot_not_ready")


def native_api_probe_script(base, timeout):
    return ("import urllib.request,urllib.error\ntry:\n urllib.request.urlopen(" + repr(base + "/api/")
            + ",timeout=" + str(timeout) + ")\nexcept urllib.error.HTTPError as e:\n assert e.code==401\n"
            "else:\n raise AssertionError('native_auth_api_expected')\n")


def lan_callback_probe_script(base):
    """Observe the unchanged single request; retain its original exception."""
    return "import errno,json,urllib.error\ndef check():\n" + textwrap.indent(
        native_api_probe_script(base, 10), " ") + '''
try:
 check()
except Exception as error:
 reason = error.reason if isinstance(error, urllib.error.URLError) else error
 if isinstance(reason, TimeoutError):
  code = 'socket_timeout'
 elif isinstance(reason, OSError) and reason.errno == errno.ECONNREFUSED:
  code = 'connection_refused'
 elif isinstance(reason, OSError) and reason.errno in {errno.ENETUNREACH, errno.EHOSTUNREACH}:
  code = 'network_unreachable'
 elif isinstance(error, AssertionError):
  code = 'unexpected_http_status'
 else:
  code = 'unexpected_error'
 print(json.dumps({'schema':1,'kind':'installed_ha_lan_callback','status':'failed','error_code':code}),flush=True)
 raise
else:
 print(json.dumps({'schema':1,'kind':'installed_ha_lan_callback','status':'passed','error_code':None}),flush=True)
'''


def lan_callback_diagnostics(raw, *, succeeded):
    """Only a flat finite receipt is accepted, never HTTP output or URLs."""
    if not isinstance(raw, (str, bytes)) or len(raw) > 2048:
        return None
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result
    def reject_constant(value):
        raise ValueError("nonfinite_json")
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if len(raw.encode("utf-8")) > 2048:
            return None
        value = json.loads(raw, object_pairs_hook=unique, parse_constant=reject_constant)
    except (UnicodeError, ValueError, TypeError, RecursionError):
        return None
    if (not isinstance(value, dict) or set(value) != {"schema", "kind", "status", "error_code"}
            or type(value["schema"]) is not int or value["schema"] != 1
            or value["kind"] != "installed_ha_lan_callback"
            or value["status"] != ("passed" if succeeded else "failed")):
        return None
    code = value["error_code"]
    if ((succeeded and code is not None) or (not succeeded and (not isinstance(code, str)
            or code not in {"socket_timeout", "connection_refused", "network_unreachable",
                            "unexpected_http_status", "unexpected_error"}))):
        return None
    return {"status": value["status"], "error_code": code}


def lan_partial_path(request, profile):
    """Keep incomplete observations outside all distributable source inputs."""
    assert profile in {"laptop-amd64", "server-amd64"}
    xml = getattr(getattr(request.config, "option", None), "xmlpath", None)
    directory = Path(xml).expanduser().resolve().parent if xml else ROOT / "build"
    if directory.is_relative_to(ROOT.resolve()):
        directory = ROOT / "build"
    return directory / ("installed-ha-lan-partial-" + profile + ".json")


def retain_lan_partial(path, value):
    """Atomic private checkpoint; a failed diagnostic write never replaces cause."""
    temporary = path.with_name(path.name + ".partial-" + uuid.uuid4().hex)
    try:
        raw = json.dumps(value, indent=2).encode() + b"\n"
        assert len(raw) <= 8192
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return True
    except Exception:
        return False
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)


def installed_lan(ssh, container, phase, image_ids, *, partial_path, profile, iso_hash):
    """Actual second physical-from-guest NIC, native discovery, reboot, removal."""
    report = {"kind": "real_installed_haos_wired_lan", "result": "running", "images": image_ids,
              "network": "synthetic_tap_inside_owned_docker_namespace", "physical_household": "not_run",
              "model_provider": "not_run", "fixture_protocol": "homekit_mdns_advertisement_only"}
    source = (ROOT / "tests/installed_ha_lan_probe.py").read_text()
    compose = "sudo docker compose -f /opt/agentic-home/host/compose.yaml"
    assert profile in {"laptop-amd64", "server-amd64"} and re.fullmatch(r"[a-f0-9]{64}", iso_hash)
    partial = {"schema": 1, "kind": "installed_ha_lan_partial", "status": "incomplete", "complete": False,
               "profile": profile, "iso_sha256": iso_hash, "stages": {}}
    retain_lan_partial(partial_path, partial)

    def probe(stage):
        result = command(*ssh, compose + " exec -T -e AGENTIC_LAN_PROBE_PHASE=" + stage + " controller python -",
                         input=source, timeout=900)
        return json.loads(result)

    def wait_native(stage):
        # Onboarding routes disappear after completed onboarding and a restart.
        # A 401 from the Core API proves the native authenticated API is alive;
        # the following owner probe establishes the actual network postconditions.
        phase(stage)
        script = native_api_probe_script("http://localhost:8123", 3)
        def ready():
            try:
                result = subprocess.run([*ssh, "python3 -"], input=script, text=True, capture_output=True, timeout=15)
                return result.returncode == 0
            except subprocess.TimeoutExpired:
                return False
        assert wait_until(ready, 900), "native_core_api_unavailable"

    def callback(observation, stage):
        native = {name: observation.get(name) for name in (
            "management_reachable", "dhcp_ipv4", "ipv6_ra", "lan_default_gateway", "management_gateway_absent",
            "native_discovery_adapter_auto", "native_mdns_flow_observed", "product_observation_ready")}
        assert all(value is True for value in native.values())
        partial["stages"][stage] = {"native": native, "callback": {"status": "pending", "error_code": None}}
        retain_lan_partial(partial_path, partial)
        url = observation["callback_url"]
        # Only the synthetic peer namespace contacts this observed fixture URL.
        from urllib.parse import urlsplit

        from installed_ha_lan_probe import callback_address_matches

        parsed = urlsplit(url)
        assert (re.fullmatch(r"198\.18\.42\.(?:1[0-4][0-9]|150)", parsed.hostname or "")
                and callback_address_matches(url, [parsed.hostname]))
        script = lan_callback_probe_script(url)
        try:
            output = command("docker", "exec", "-i", container, "python3", "-", input=script, timeout=20)
            diagnostic = lan_callback_diagnostics(output, succeeded=True)
            assert diagnostic is not None, "lan_callback_diagnostic_invalid"
        except Exception as error:
            diagnostic = None
            if isinstance(error, subprocess.CalledProcessError):
                diagnostic = lan_callback_diagnostics(error.stdout, succeeded=False)
            code = "peer_process_timeout" if isinstance(error, subprocess.TimeoutExpired) else "peer_process_failed"
            partial["stages"][stage]["callback"] = diagnostic or {"status": "failed", "error_code": code}
            if not retain_lan_partial(partial_path, partial):
                with contextlib.suppress(Exception):
                    error.add_note("Private LAN partial observation could not be retained.")
            raise
        partial["stages"][stage]["callback"] = diagnostic
        retain_lan_partial(partial_path, partial)
        return {**native, "native_callback_reachable_from_lan_peer": True}

    phase("wired_lan_native_owner_fixture")
    report["owner"] = probe("onboard")
    phase("wired_lan_select_and_start")
    command(*ssh, "agentic-home shutdown", timeout=240)
    status = json.loads(command(*ssh, "agentic-home ha-lan", timeout=30))
    choices = [row for row in status["interfaces"] if row["address"] == "198.18.42.10"]
    assert len(choices) == 1 and re.fullmatch(r"[a-f0-9]{64}", choices[0]["id"])
    command(*ssh, "agentic-home configure_lan --interface-id " + choices[0]["id"], timeout=180)
    command(*ssh, "agentic-home start", timeout=120)
    wait_native("wired_lan_guest_boot")
    phase("wired_lan_native_checks")
    initial = probe("wired")
    phase("wired_lan_initial_callback")
    report["initial"] = callback(initial, "initial")
    phase("wired_lan_outer_reboot")
    report["outer_reboot_observation"] = reboot_outer(ssh, 900)
    partial["outer_reboot_observation"] = {name: report["outer_reboot_observation"].get(name) is True
                                          for name in ("boot_id_changed", "service_active_in_new_boot")}
    retain_lan_partial(partial_path, partial)
    wait_native("wired_lan_after_outer_reboot")
    phase("wired_lan_after_reboot_native_checks")
    after_reboot = probe("wired")
    phase("wired_lan_after_reboot_callback")
    report["after_outer_reboot"] = callback(after_reboot, "after_outer_reboot")
    phase("wired_lan_remove_and_restore_nat")
    command(*ssh, "agentic-home shutdown", timeout=240)
    command(*ssh, "agentic-home configure_lan --disable-lan", timeout=180)
    command(*ssh, "agentic-home start", timeout=120)
    wait_native("nat_after_wired_lan_removal")
    phase("nat_native_checks")
    report["removed"] = probe("nat")
    report["result"] = "passed"
    return report


def wait_until(ready, seconds, *, clock=time.monotonic, pause=time.sleep):
    """Wall-clock bound. A slow test disk stretches every phase and every probe."""
    deadline = clock() + seconds
    while clock() < deadline:
        if ready():
            return True
        if (remaining := deadline - clock()) > 0:
            pause(min(5, remaining))
    return False


def remote_ok(ssh, script, *, run=subprocess.run, timeout=FIRSTBOOT_PROBE_SECONDS,
              observations=None, clock=time.monotonic, checkpoint=lambda: None):
    """Read-only firstboot probe; retain bounded classifications, never SSH text."""
    from installed_update_probe import process_diagnostics

    started = clock()
    record = {"timeout_seconds": round(timeout, 3), "status": "not_ready"}
    try:
        result = run([*ssh, script], capture_output=True, timeout=timeout)
        ready = result.returncode == 0
        record.update(status="ready" if ready else "not_ready", process=process_diagnostics(result))
    except (subprocess.TimeoutExpired, OSError) as error:
        ready = False
        record.update(status="timeout" if isinstance(error, subprocess.TimeoutExpired) else "unavailable",
            error_type=type(error).__name__ if type(error).__name__ in {
                "TimeoutExpired", "PermissionError", "FileNotFoundError", "OSError"} else "OSError",
            process=process_diagnostics(subprocess.CompletedProcess([], None,
                getattr(error, "stdout", None), getattr(error, "stderr", None))))
    record["elapsed_seconds"] = round(clock() - started, 3)
    if observations is not None and len(observations) < FIRSTBOOT_MAX_OBSERVATIONS:
        observations.append({"attempt": len(observations) + 1, **record})
        try:
            checkpoint()
        except OSError:
            record["checkpoint_failed"] = True
            observations[-1]["checkpoint_failed"] = True
    return ready


def probe_json(result, diagnostic_path):
    from installed_update_probe import process_diagnostics

    diagnostic_path.write_text(json.dumps(process_diagnostics(result), indent=2) + "\n")
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        # Preserve the original parser error while allowing the outer failure
        # collector to retain the actual process/transport evidence.
        exc.stdout, exc.stderr, exc.returncode, exc.cmd = result.stdout, result.stderr, result.returncode, result.args
        raise


def failure_observation_public(value):
    from installed_update_probe import (
        HOST_VM_STATES,
        PRODUCT_UNITS,
        compatibility_receipt,
        lifecycle_receipt,
        process_diagnostics,
        product_unit_public,
    )

    if not isinstance(value, dict):
        raise ValueError("invalid_failure_observation")
    flags = process_diagnostics(subprocess.CompletedProcess([], None, "", ""))["stderr_signatures"]
    result = {"selection": value.get("selection") if value.get("selection") in {"exact_id", "recent_action_unbound"} else "unknown",
              "jobs": []}
    if isinstance(value.get("jobs"), list):
        result["jobs"] = [receipt for item in value["jobs"][:5] if (receipt := lifecycle_receipt(item)) is not None]
    if isinstance(value.get("filesystem"), dict):
        result["filesystem"] = {key: number for key in ("total_bytes", "used_bytes", "free_bytes")
                                if type(number := value["filesystem"].get(key)) is int and 0 <= number <= 2**63}
    if value.get("filesystem_observation") == "probe_failed":
        result["filesystem_observation"] = "probe_failed"
    if isinstance(value.get("product_units"), dict):
        result["product_units"] = {name: product_unit_public(value["product_units"][name])
                                   for name in PRODUCT_UNITS if name in value["product_units"]}
    result["compatibility"] = compatibility_receipt(value.get("compatibility"))
    state = value.get("provision_state")
    if isinstance(state, dict):
        result["provision_state"] = {**{key: state.get(key) is True for key in ("record_readable", "owner_reference_valid")},
            **{key: state.get(key) if type(state.get(key)) is bool else None for key in ("complete", "restore_pending")}, "files": {}}
        files = state.get("files") if isinstance(state.get("files"), dict) else {}
        for name in ("disk", "partial_disk", "nvram"):
            item = files.get(name) if isinstance(files.get(name), dict) else {}
            kind = item.get("kind")
            result["provision_state"]["files"][name] = {
                "kind": kind if isinstance(kind, str) and kind in {"absent", "unreadable", "regular", "directory", "symlink", "other"} else "unknown"}
            if kind == "regular" and type(item.get("bytes")) is int and 0 <= item["bytes"] <= 2**63:
                result["provision_state"]["files"][name]["bytes"] = item["bytes"]
    for name in ("domain", "network"):
        item = value.get(name)
        if isinstance(item, dict):
            status = item.get("observation")
            result[name] = {"observation": status if isinstance(status, str) and status in {
                "ok", "timeout", "output_limit", "probe_failed", "command_missing", "permission_denied"} else "unknown",
                "xml_valid": item.get("xml_valid") is True,
                **{key: item.get(key) if type(item.get(key)) is bool else None for key in ("name_matches", "ownership_matches")}}
            if name == "domain" and "disk_matches" in item:
                result[name]["disk_matches"] = item["disk_matches"] if type(item["disk_matches"]) is bool else None
    for name in ("broker_unit", "vm", "journal", "kernel"):
        item = value.get(name, {})
        if not isinstance(item, dict):
            continue
        signatures = item.get("signatures", {})
        if not isinstance(signatures, dict):
            signatures = {}
        result[name] = {"observation": item.get("observation") if item.get("observation") in {
            "ok", "timeout", "output_limit", "probe_failed", "command_missing", "permission_denied"} else "unknown",
            "signatures": {key: signatures.get(key) is True for key in flags}}
        if name == "vm":
            result[name]["state"] = item.get("state") if isinstance(item.get("state"), str) and item["state"] in HOST_VM_STATES else "unknown"
        if name == "broker_unit":
            result[name]["active"] = item.get("active") if item.get("active") in {"active", "inactive", "failed", "activating", "deactivating"} else "unknown"
            result[name]["oom_killed"] = item.get("oom_killed") is True
    return result


def record_install_failure(exc, phase, report, ssh, *, run=subprocess.run,
                           clock=time.monotonic, pause=time.sleep):
    """Never retry effects; retain safe original diagnostics before VM teardown."""
    from installed_update_probe import LIFECYCLE_ACTIONS, lifecycle_receipt, process_diagnostics

    stdout, stderr = getattr(exc, "stdout", "") or "", getattr(exc, "stderr", "") or ""
    # Keep the original exception intact, including malformed private streams.
    process = subprocess.CompletedProcess([], getattr(exc, "returncode", None),
        stdout.encode(errors="replace") if isinstance(stdout, str) else stdout,
        stderr.encode(errors="replace") if isinstance(stderr, str) else stderr)
    result = {"phase": phase, "error_type": type(exc).__name__ if type(exc).__name__ in {
        "CalledProcessError", "TimeoutExpired", "AssertionError", "JSONDecodeError", "TypeError"} else "OtherError",
        "process": process_diagnostics(process), "remote": "not_attempted"}
    if (isinstance(exc, AssertionError) and len(exc.args) == 1
            and isinstance(exc.args[0], str) and exc.args[0] in REBOOT_ERRORS):
        result["reboot_error_code"] = exc.args[0]
    if isinstance(exc, subprocess.TimeoutExpired) and type(exc.timeout) in {int, float} and 0 <= exc.timeout <= 3600:
        result["timeout_seconds"] = exc.timeout
    args = getattr(exc, "cmd", ())
    remote_command = args[-1] if isinstance(args, (tuple, list)) and args and isinstance(args[-1], str) else ""
    lan_probe = re.fullmatch(
        r"sudo docker compose -f /opt/agentic-home/host/compose\.yaml exec -T "
        r"-e AGENTIC_LAN_PROBE_PHASE=(onboard|wired|nat) controller python -", remote_command)
    if lan_probe and (native := lan_probe_diagnostics(stdout, lan_probe[1])) is not None:
        result["native_lan"] = native
    action = next((item for item in LIFECYCLE_ACTIONS if (item != "configure_lan" and remote_command == "agentic-home " + item) or
                   (item == "restore" and remote_command.startswith("agentic-home restore --snapshot ")) or
                   (item == "configure_lan" and re.fullmatch(
                       r"agentic-home configure_lan (?:--interface-id [a-f0-9]{64}|--disable-lan)", remote_command))), None)
    result["lifecycle_action"] = action
    output = stdout.decode(errors="replace") if isinstance(stdout, bytes) else stdout
    receipt = None
    if not lan_probe and isinstance(output, str) and len(output.encode()) <= 65536:
        try:
            receipt = lifecycle_receipt(json.loads(output[output.index("{"):]))
        except (ValueError, TypeError, AttributeError):
            pass
        if receipt is None and (match := re.search(r"(?m)^Operation ([a-f0-9]{32})$", output)):
            receipt = lifecycle_receipt({"id": match[1], "action": action, "state": "unknown"})
    result["lifecycle_receipt"] = receipt
    report.write_text(json.dumps(result, indent=2) + "\n")
    if ssh is None:
        return result
    identifier = receipt["id"] if receipt else None
    source = (ROOT / "tests/installed_update_probe.py").read_text()
    encoded = base64.b64encode(source.encode()).decode()
    script = ("import base64,json,sys\ns=base64.b64decode('" + encoded + "').decode()\n"
              "ns={'__name__':'installed_failure_diagnostics'}\nexec(compile(s,'installed_failure_diagnostics','exec'),ns)\n"
              "sys.path.insert(0,'/opt/agentic-home/host')\nfrom agentic_host import support\n"
              f"support.PROBE_SECONDS={FAILURE_DIAGNOSTIC_PROBE_SECONDS}\n"
              f"print(json.dumps(ns['failure_observation']({identifier!r},{action!r})),flush=True)\n")
    # Only this disposable diagnostic process gets longer capture bounds; no
    # installed service configuration or product timeout changes. Three attempts
    # share one deadline and never repeat the failed lifecycle effect.
    attempts = FAILURE_DIAGNOSTICS_ATTEMPTS if phase == "installed_firstboot" else 1
    deadline = clock() + attempts * FAILURE_DIAGNOSTICS_TIMEOUT_SECONDS
    result["diagnostic_attempts"] = []
    for attempt in range(1, attempts + 1):
        remaining = deadline - clock()
        if remaining <= 0:
            break
        started = clock()
        observation = {"attempt": attempt}
        try:
            timeout = (FAILURE_DIAGNOSTICS_TIMEOUT_SECONDS if attempts == 1
                       else min(FAILURE_DIAGNOSTICS_TIMEOUT_SECONDS, remaining))
            observed = run([*ssh, "sudo env AGENTIC_DISPOSABLE_VM=1 python3 -"], capture_output=True,
                           text=True, input=script, timeout=timeout)
            observation["process"] = result["diagnostic_process"] = process_diagnostics(observed)
            if observed.returncode != 0 or len(observed.stdout.encode()) > 65536:
                raise ValueError("diagnostic_unavailable")
            result["remote"] = failure_observation_public(json.loads(observed.stdout))
            result.pop("diagnostic_error_type", None)
            journal = result["remote"].get("journal", {})
            observation["status"] = {"timeout": "journal_timeout", "probe_failed": "journal_unavailable"}.get(
                journal.get("observation"), "observed")
        except Exception as diagnostic_error:
            if not isinstance(result["remote"], dict):
                result["remote"] = "unavailable"
            observation.update(status="unavailable", error_type=type(diagnostic_error).__name__ if isinstance(
                diagnostic_error, (subprocess.TimeoutExpired, ValueError, OSError)) else "OtherError")
            result["diagnostic_error_type"] = observation["error_type"]
        observation["elapsed_seconds"] = round(clock() - started, 3)
        result["diagnostic_attempts"].append(observation)
        report.write_text(json.dumps(result, indent=2) + "\n")
        if observation["status"] == "observed":
            break
        if attempt < attempts and (remaining := deadline - clock()) > 0:
            pause(min(5, remaining))
    return result


def decode_bridge_contract_receipt(value, supporting_image):
    """Reject partial/ambiguous proof; publish only fixed mechanism and digests."""
    checks = ("revoked_history_marker_copied", "supporting_fixed_probe_passed", "missing_contract_fixed_probe_rejected",
              "missing_contract_import_error", "missing_contract_without_history_passed", "revoked_database_unchanged",
              "pending_maintenance_rejected", "pending_maintenance_readonly")
    cleanup = ("probe_containers_absent", "quota_volumes_absent", "probe_journal_absent", "fixture_image_absent", "scratch_database_absent")
    fixed = {"contract": 1, "observed_from": "installed_host_fixed_probe", "schema": 3,
             "database_fixture": "isolated_structure_only_revoked_bridge", "fixed_probe_executions": 3,
             "synthetic_missing_contract_image": True, "real_historical_release": False}
    if (not isinstance(value, dict) or any(type(value.get(key)) is not type(expected) or value[key] != expected for key, expected in fixed.items()) or
            value.get("supporting_image") != supporting_image or
            not isinstance(value.get("missing_contract_image"), str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", value["missing_contract_image"]) or
            value["missing_contract_image"] == supporting_image or
            not isinstance(value.get("fixed_script_sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", value["fixed_script_sha256"]) or
            any(not isinstance(value.get(group), dict) or any(value[group].get(key) is not True for key in keys)
                for group, keys in (("checks", checks), ("cleanup", cleanup)))):
        raise ValueError("probe_bridge_contract_receipt_invalid")
    return {**fixed, **{key: value[key] for key in ("supporting_image", "missing_contract_image", "fixed_script_sha256")},
            "checks": dict.fromkeys(checks, True), "cleanup": dict.fromkeys(cleanup, True)}


def decode_update_observation(output):
    """Validate/project private probe receipts; never publish arbitrary remote text."""
    from installed_update_probe import ERROR_TYPES, SAFE_ERRORS, STAGES, UPDATE_PHASES, process_diagnostics

    if not output or len(output.encode()) > 65536:
        raise ValueError("probe_output_missing_or_oversized")
    value = json.loads(output)
    if not isinstance(value, dict) or set(value) != {"checkpoint", "result", "unit", "process"}:
        raise ValueError("probe_observation_invalid")
    checkpoint = value["checkpoint"]
    if checkpoint is not None and (not isinstance(checkpoint, dict) or checkpoint.get("state") != "running" or checkpoint.get("stage") not in STAGES):
        raise ValueError("probe_checkpoint_invalid")
    clean = {"checkpoint": None if checkpoint is None else {"state": "running", "stage": checkpoint["stage"]}}
    unit = value["unit"]
    if not isinstance(unit, dict):
        raise ValueError("probe_unit_invalid")
    clean["unit"] = {
        "ActiveState": unit.get("ActiveState") if unit.get("ActiveState") in {"active", "activating", "deactivating", "inactive", "failed"} else "unknown",
        "SubState": unit.get("SubState") if unit.get("SubState") in {"running", "exited", "failed", "dead", "start", "stop", "stop-sigterm", "stop-sigkill"} else "unknown",
        "Result": unit.get("Result") if unit.get("Result") in {"success", "exit-code", "signal", "core-dump", "timeout", "oom-kill", "resources"} else "unknown",
        **{key: unit.get(key) if isinstance(unit.get(key), str) and re.fullmatch(r"[0-9]{1,3}", unit[key]) else None
           for key in ("ExecMainCode", "ExecMainStatus")},
    }
    process = value["process"]
    if not isinstance(process, dict):
        raise ValueError("probe_process_invalid")
    clean["process"] = {k: process[k] for k in ("stdout_bytes", "stderr_bytes") if type(process.get(k)) is int and process[k] >= 0}
    clean["process"]["stderr_truncated"] = process.get("stderr_truncated") is True
    flags = process.get("stderr_signatures", {})
    clean["process"]["stderr_signatures"] = {k: flags.get(k) is True for k in (
        "traceback", "import_error", "syntax_error", "permission_denied", "ssh_transport", "process_killed", "sudo_error")}
    result = value["result"]
    if result is None:
        clean["result"] = None
        return clean
    if not isinstance(result, dict) or result.get("state") not in {"passed", "failed"}:
        raise ValueError("probe_result_invalid")
    diagnostic = result.get("diagnostics", {})
    if not isinstance(diagnostic, dict):
        raise ValueError("probe_diagnostics_invalid")
    projected = {"state": result["state"], "diagnostics": {
        k: diagnostic.get(k) is True for k in ("cleanup_started", "cleanup_completed")}}
    for key in ("cleanup_checkpoint_failed", "result_persist_failed"):
        if key in diagnostic:
            projected["diagnostics"][key] = diagnostic[key] is True
    if "cleanup_error_type" in diagnostic:
        projected["diagnostics"]["cleanup_error_type"] = diagnostic["cleanup_error_type"] if diagnostic["cleanup_error_type"] in ERROR_TYPES else "OtherError"
        error = diagnostic.get("cleanup_error_code")
        projected["diagnostics"]["cleanup_error_code"] = error if error in SAFE_ERRORS else "unclassified_update_error"
    if diagnostic.get("updater_state") in {"failed", "needs_reconcile", "rolled_back"}:
        projected["diagnostics"]["updater_state"] = diagnostic["updater_state"]
        projected["diagnostics"]["updater_phase"] = diagnostic.get("updater_phase") if diagnostic.get("updater_phase") in UPDATE_PHASES else "unknown"
        error = diagnostic.get("updater_error_code")
        projected["diagnostics"]["updater_error_code"] = error if error in SAFE_ERRORS else "unclassified_update_error"
    signatures = process_diagnostics(subprocess.CompletedProcess([], None, "", ""))["stderr_signatures"]
    probe_error = diagnostic.get("probe_failure")
    if isinstance(probe_error, dict):
        projected["diagnostics"]["probe_failure"] = {
            "error_code": probe_error.get("error_code") if probe_error.get("error_code") in SAFE_ERRORS else "unclassified_update_error",
            "error_type": probe_error.get("error_type") if probe_error.get("error_type") in ERROR_TYPES else "OtherError",
            "signatures": {key: probe_error.get("signatures", {}).get(key) is True for key in signatures},
        }
        if probe_error.get("operation") in {"umount", "mount", "mkfs.ext4", "findmnt", "docker.run", "docker.inspect", "docker.info", "docker.rm", "docker.kill"}:
            projected["diagnostics"]["probe_failure"]["operation"] = probe_error["operation"]
        if type(probe_error.get("command_exit_code")) is int and -128 <= probe_error["command_exit_code"] <= 255:
            projected["diagnostics"]["probe_failure"]["command_exit_code"] = probe_error["command_exit_code"]
    command = diagnostic.get("command_failure")
    if isinstance(command, dict):
        projected["diagnostics"]["command_failure"] = {
            "returncode": command.get("returncode") if type(command.get("returncode")) is int and -255 <= command["returncode"] <= 255 else None,
            "stderr_signatures": {key: command.get("stderr_signatures", {}).get(key) is True for key in signatures},
            **{key: command[key] for key in ("stdout_bytes", "stderr_bytes") if type(command.get(key)) is int and command[key] >= 0},
            "stderr_truncated": command.get("stderr_truncated") is True,
        }
    resources = diagnostic.get("probe_resources")
    if isinstance(resources, dict):
        projected["diagnostics"]["probe_resources"] = {key: resources[key] for key in ("free_disk_bytes", "available_memory_bytes")
                                                      if type(resources.get(key)) is int and resources[key] >= 0}
    if result["state"] == "failed":
        projected.update(error_code=result.get("error_code") if result.get("error_code") in SAFE_ERRORS else "unclassified_update_error",
                         error_type=result.get("error_type") if result.get("error_type") in ERROR_TYPES else "OtherError",
                         stage=result.get("stage") if result.get("stage") in STAGES else "unknown")
    else:
        actual = ["ed25519_signature_verification", "bounded_candidate_probe", "compose_activation", "authenticated_owner_smoke",
                  "consistent_snapshot", "schema3", "maintenance_gate", "same_database_rollback", "post_deployment_row_preserved",
                  "revoked_bridge_fixed_probe_enforced", "pending_bridge_operation_blocked", "isolated_probe_cleanup_verified",
                  "coding_source_moved_with_image_and_rolled_back"]
        fixtures = ["ephemeral_publisher", "preloaded_registry_delivery", "candidate_changes_label_and_source_bundle", "synthetic_post_deployment_record",
                    "synthetic_missing_bridge_contract_image", "isolated_structure_only_revoked_bridge"]
        if (result.get("actual") != actual or result.get("fixtures") != fixtures or result.get("final_schema") != 3 or
                result.get("real_cloud") is not False or result.get("real_publisher_release") is not False or
                result.get("rollback_restored_backup") is not False or result.get("registry_delivery_calls") != 1 or
                not all(projected["diagnostics"].get(k) for k in ("cleanup_started", "cleanup_completed")) or
                any(not isinstance(result.get(k), str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", result[k]) for k in ("original_image", "candidate_image")) or
                result["original_image"] == result["candidate_image"]):
            raise ValueError("probe_success_receipt_invalid")
        projected.update(observed_from="disposable_installed_linux_vm_root", actual=actual, fixtures=fixtures,
                         final_schema=3, real_cloud=False, real_publisher_release=False, rollback_restored_backup=False,
                         registry_delivery_calls=1, original_image=result["original_image"], candidate_image=result["candidate_image"],
                         bridge_update_contract=decode_bridge_contract_receipt(result.get("bridge_update_contract"), result["original_image"]))
    clean["result"] = projected
    return clean


def installed_update(ssh, source, report, *, run=subprocess.run, clock=time.monotonic, pause=time.sleep):
    """Launch once, then recover only the same fixed private receipt over fresh SSH."""
    from installed_update_probe import STATE_ROOT, process_diagnostics

    evidence = {"state": "failed", "error_code": "probe_result_unconfirmed", "transport": {}}
    def save():
        report.write_text(json.dumps(evidence, indent=2) + "\n")
    def invoke(command, **kwargs):
        try:
            return run([*ssh, command], capture_output=True, text=True, timeout=UPDATE_INVOKE_TIMEOUT_SECONDS, **kwargs)
        except subprocess.TimeoutExpired as exc:
            return subprocess.CompletedProcess([], None, exc.stdout or "", exc.stderr or "")
    encoded = base64.b64encode(source.encode()).decode()
    launcher = ("import base64,json\ns=base64.b64decode('" + encoded + "').decode()\n"
                "ns={'__name__':'installed_probe_launcher'}\nexec(compile(s,'installed_update_probe','exec'),ns)\n"
                "print(json.dumps(ns['launch'](s)),flush=True)\n")
    started = invoke("sudo env AGENTIC_DISPOSABLE_VM=1 python3 -", input=launcher)
    evidence["transport"]["launch"] = process_diagnostics(started)
    save()  # Preserve return code/flags BEFORE attempting to parse anything.
    deadline = clock() + UPDATE_DEADLINE_SECONDS
    attempts = 0
    misses = 0
    while clock() < deadline:
        response = invoke("sudo env AGENTIC_DISPOSABLE_VM=1 python3 " + str(STATE_ROOT / "probe.py") + " --observe")
        attempts += 1
        evidence["transport"].update(observation_attempts=attempts, observation=process_diagnostics(response))
        save()
        try:
            if response.returncode != 0:
                raise ValueError("probe_observation_transport")
            observed = decode_update_observation(response.stdout)
            evidence["observation"] = observed
        except (ValueError, TypeError, AttributeError):
            misses += 1
            evidence["error_code"] = "probe_observation_unavailable"
            save()
            if misses >= 3:
                return evidence
            pause(2)
            continue
        misses = 0
        unit = observed["unit"]
        if unit["ActiveState"] in {"inactive", "failed", "unknown"} or unit["SubState"] == "exited":
            result = observed["result"]
            if result is not None:
                evidence.update(result)
            if not (result is not None and result["state"] == "passed" and unit["ActiveState"] == "active" and unit["SubState"] == "exited" and
                    unit["Result"] == "success" and unit["ExecMainCode"] == "1" and unit["ExecMainStatus"] == "0"):
                evidence["state"] = "failed"
                evidence.setdefault("error_code", "probe_completion_unconfirmed")
            else:
                evidence.pop("error_code", None)
            save()
            return evidence
        save()
        pause(2)
    evidence["error_code"] = "probe_observation_deadline"
    save()
    return evidence


def test_full_linux_install_with_nested_haos(tmp_path, request):
    if not request.config.getoption("--linux-install"):
        pytest.skip("Use --linux-install to install onto a disposable virtual disk")
    assert str(tmp_path).startswith(str(Path.home() / "agent-work/tmp/pytest"))
    profile = os.environ.get("AGENTIC_INSTALL_TEST_PROFILE", "laptop-amd64")
    assert profile in {"laptop-amd64", "server-amd64"}
    iso = ROOT / "dist" / ("agentic-home-0.11.0-" + profile + ".iso")
    assert iso.exists()
    with iso.open("rb") as stream:
        iso_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    spec = importlib.util.spec_from_file_location("builder", ROOT / "scripts/build_host.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    command("ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "test-key"))
    config = build.autoinstall({"id": profile})
    autoinstall = config["autoinstall"]
    autoinstall.pop("interactive-sections")
    autoinstall["identity"] = {"hostname": "agentic-disposable", "username": "tester", "password": "!"}
    autoinstall["ssh"] = {"install-server": True, "allow-pw": False,
                          "authorized-keys": [(tmp_path / "test-key.pub").read_text().strip()]}
    autoinstall["storage"] = {"layout": {"name": "direct", "match": {"serial": "AGENTIC_TEST_ONLY"}}}
    autoinstall["shutdown"] = "poweroff"
    # Capture the exact shipped hardware gate's output while retaining its exit
    # status and original position before storage. This wrapper is fixture-only.
    gate = autoinstall["early-commands"][0]
    autoinstall["early-commands"][0] = ["bash", "-o", "pipefail", "-c",
        shlex.join(gate) + " | tee /run/agentic-preflight-receipt.log"]
    autoinstall["late-commands"] += [
        "printf 'tester ALL=(root) NOPASSWD: ALL\\n' > /target/etc/sudoers.d/90-agentic-test",
        "chmod 440 /target/etc/sudoers.d/90-agentic-test"]
    autoinstall["late-commands"] += provider_policy_seed()
    autoinstall["late-commands"] += ["cp /run/agentic-preflight-receipt.log /target/var/lib/agentic-home/host/fixture-preflight.log"]
    (tmp_path / "user-data").write_text("#cloud-config\n" + json.dumps(config, indent=2))
    (tmp_path / "ha-lan-peer.py").write_text((ROOT / "tests/ha_lan_peer.py").read_text())
    # Connect from QEMU's loopback: Docker's outer 172.17/16 otherwise overlaps
    # Docker inside the guest after firstboot and breaks the return route.
    (tmp_path / "ssh-proxy.py").write_text('''import socket,sys,threading
s=socket.create_connection(('127.0.0.1',2222))
def forward():
 while data:=sys.stdin.buffer.read1(65536): s.sendall(data)
 s.shutdown(socket.SHUT_WR)
threading.Thread(target=forward,daemon=True).start()
while data:=s.recv(65536):
 sys.stdout.buffer.write(data);sys.stdout.buffer.flush()
''')
    name = "agentic-linux-install-test-" + uuid.uuid4().hex[:10]
    started = time.time()
    progress = {"runtime_location": "disposable_outer_linux_vm", "iso_sha256": iso_hash,
                "result": "running", "cloud_login": "not_run",
                "fixture_resources": {"guest_memory_mib": INSTALL_GUEST_MEMORY_MIB,
                    "container_memory_gib": INSTALL_CONTAINER_MEMORY_GIB,
                    "container_memory_and_swap_gib": INSTALL_CONTAINER_MEMORY_SWAP_GIB},
                "excluded_cli_probes": {"grok": "not_run: owner instruction"}}
    progress_path = ROOT / "evidence" / ("installed-progress-" + profile + ".json")

    def phase(name):
        progress.update(phase=name, duration_seconds=round(time.time() - started))
        progress_path.write_text(json.dumps(progress, indent=2) + "\n")

    try:
        phase("prepare_isolated_lan_test_image")
        command("docker", "build", "-f", str(ROOT / "tests/ha_lan_lab.Dockerfile"), "-t", LAN_LAB_IMAGE, str(ROOT), timeout=600)
        lan_images = {"builder": command("docker", "image", "inspect", "--format", "{{.Id}}", "agentic-home-builder:0.2.0"),
                      "lan_lab": command("docker", "image", "inspect", "--format", "{{.Id}}", LAN_LAB_IMAGE)}
        phase("prepare_disposable_media")
        command("docker", "run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}", "-v", f"{tmp_path}:/lab",
                "-v", f"{iso}:/source.iso:ro", "agentic-home-builder:0.2.0", "xorriso",
                "-indev", "/source.iso", "-outdev", "/lab/test.iso", "-boot_image", "any", "replay",
                "-map", "/lab/user-data", "/agentic-seed/user-data", "-commit")
        command("docker", "run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}", "-v", f"{tmp_path}:/lab",
                "agentic-home-builder:0.2.0", "qemu-img", "create", "-f", "qcow2", "/lab/linux.qcow2", "120G")
        qemu = ["qemu-system-x86_64", "-enable-kvm", "-cpu", "host", "-machine", "q35", "-m", str(INSTALL_GUEST_MEMORY_MIB), "-smp", "4",
                "-drive", "if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd",
                "-drive", "if=pflash,format=raw,file=/lab/nvram.fd",
                "-drive", "file=/lab/linux.qcow2,format=qcow2,if=none,id=disk,discard=unmap,detect-zeroes=unmap",
                "-device", "virtio-blk-pci,drive=disk,serial=AGENTIC_TEST_ONLY",
                "-netdev", "user,id=net,hostfwd=tcp:0.0.0.0:2222-:22", "-device", "virtio-net-pci,netdev=net",
                "-netdev", "tap,id=lan,ifname=ahltap,script=no,downscript=no",
                "-device", "virtio-net-pci,netdev=lan,mac=52:54:00:aa:42:01",
                "-display", "none", "-serial", "file:/lab/serial.log", "-monitor", "unix:/lab/monitor.sock,server,nowait"]
        command("docker", "run", "--rm", "-v", f"{tmp_path}:/lab", "agentic-home-builder:0.2.0",
                "cp", "/usr/share/OVMF/OVMF_VARS_4M.fd", "/lab/nvram.fd")
        common = ["docker", "run", "-d", "--name", name, "--device", "/dev/kvm", "--memory", f"{INSTALL_CONTAINER_MEMORY_GIB}g",
                  "--memory-swap", f"{INSTALL_CONTAINER_MEMORY_SWAP_GIB}g", "--cpus", "4",
                  "--cap-add", "NET_ADMIN", "--device", "/dev/net/tun", "--sysctl", "net.ipv4.ip_forward=1",
                  "--sysctl", "net.ipv6.conf.all.disable_ipv6=0", "-e", "AGENTIC_DISPOSABLE_LAN_LAB=1",
                  "-p", "127.0.0.1::2222", "-v", f"{tmp_path}:/lab", lan_images["lan_lab"], "python3", "/lab/ha-lan-peer.py", "launch"]
        phase("ubuntu_installation")
        command(*common, *qemu, "-cdrom", "/lab/test.iso", "-boot", "d")
        def powered_off():
            state = json.loads(command("docker", "inspect", name))[0]["State"]
            if state["Running"]:
                return False
            assert state["ExitCode"] == 0 and not state["OOMKilled"]
            return True
        if not wait_until(powered_off, 1800):
            raise AssertionError("Linux installer did not power off within 30 minutes")
        command("docker", "rm", "-fv", name)
        # Subsequent boots use only the installed drive, never the installer CD.
        with contextlib.suppress(OSError):
            (tmp_path / "test.iso").unlink()
        phase("installed_firstboot")
        command(*common, *qemu)
        ssh = ["ssh", "-i", str(tmp_path / "test-key"), "-o", "BatchMode=yes",
               "-o", f"ProxyCommand=docker exec -i {name} python3 /lab/ssh-proxy.py",
               "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", f"ConnectTimeout={FIRSTBOOT_CONNECT_SECONDS}",
               "tester@127.0.0.1"]
        # The product service can keep working; this acceptance stage is bounded to one hour.
        progress["firstboot_probes"] = []
        firstboot_deadline = time.monotonic() + 3600
        def firstboot_ready():
            remaining = firstboot_deadline - time.monotonic()
            return remaining > 0 and remote_ok(ssh, "test -f /var/lib/agentic-home/installed.json",
                timeout=min(FIRSTBOOT_PROBE_SECONDS, remaining), observations=progress["firstboot_probes"],
                checkpoint=lambda: phase("installed_firstboot"))
        if not wait_until(firstboot_ready, 3600):
            raise AssertionError("Firstboot did not complete within 60 minutes")
        phase("installed_identity_and_images")
        gate_log = command(*ssh, "sudo cat /var/lib/agentic-home/host/fixture-preflight.log", timeout=30)
        assert re.search(r"Agentic Home hardware check passed: x86-64, [0-9]+ MiB RAM, working KVM\.", gate_log)
        host = json.loads(command(*ssh, "agentic-home status"))
        assert not host["provisioned"] and host["kvm"]
        installed = json.loads(command(*ssh, "sudo cat /var/lib/agentic-home/installed.json"))
        assert installed["version"] == "0.11.0" and installed["profile"] == profile
        image_lock = json.loads(command(*ssh, "cat /opt/agentic-home/images.lock.json"))
        expected_tags = {f"agentic-home-{service}:0.11.0" for service in ("controller", "runner", "linejs", "hermes", "preview")}
        assert set(image_lock) == expected_tags
        for tag, digest in image_lock.items():
            assert command(*ssh, "sudo docker image inspect --format '{{.Id}}' " + tag) == digest
        source_lock = json.loads(command(*ssh, "sudo cat /var/lib/agentic-home/coding/source.json"))
        assert source_lock == json.loads(command(*ssh, "cat /opt/agentic-home/assets/coding-source.json"))
        assert command(*ssh, "sudo git -C /var/lib/agentic-home/coding/source rev-parse HEAD") == source_lock["commit"]
        assert command(*ssh, "tailscale version").splitlines()[0] == "1.104.1"
        assert json.loads(command(*ssh, "sudo tailscale status --json"))["BackendState"] == "NeedsLogin"
        assert command(*ssh, "systemctl is-active agentic-home agentic-home-host agentic-home-proxy") == "active\nactive\nactive"
        assert command(*ssh, "curl -fsS http://localhost:8099/api/deployment")
        phase("installed_owner_and_role_auth")
        auth_result = subprocess.run([*ssh, "python3 -"], capture_output=True, text=True,
                                     timeout=AUTH_PROBE_TIMEOUT_SECONDS,
                                     input=(ROOT / "tests/installed_probe.py").read_text())
        auth_probe = probe_json(auth_result, ROOT / "evidence" / ("installed-process-auth-" + profile + ".json"))
        (ROOT / "evidence" / ("installed-auth-" + profile + ".json")).write_text(
            json.dumps(auth_probe, indent=2) + "\n")
        assert auth_result.returncode == 0 and auth_probe.get("result") != "failed", auth_probe
        assert auth_probe["excluded_cli_probes"] == {"grok": "not_run: owner instruction"}
        # Exercise independent console recovery before adding any household VM.
        phase("controller_stopped_console_recovery")
        diagnostics = json.loads(command(*ssh, "agentic-home diagnostics"))
        assert diagnostics["observations"]
        assert all(item["version_refs"]["installed_application"] == installed["version"]
                   for item in diagnostics["observations"])
        installation = next(item for item in diagnostics["observations"] if item["target_component"] == "installation")
        assert installation["status"] == "collected" and installation["data"]["profile"] == profile
        compose = ("sudo docker compose --env-file /var/lib/agentic-home/host/runtime.env "
                   "-f /opt/agentic-home/host/compose.yaml")
        command(*ssh, compose + " stop controller")
        try:
            unavailable = subprocess.run([*ssh, "curl --max-time 3 -fsS http://localhost:8099/health"], capture_output=True)
            assert unavailable.returncode != 0
            rescue = json.loads(command(*ssh, "agentic-home diagnostics"))
            assert rescue["observations"]
            assert all(item["version_refs"]["installed_application"] == installed["version"]
                       for item in rescue["observations"])
            command(*ssh, "agentic-home support-bundle --output /home/tester/support-probe.json")
            support = json.loads(command(*ssh, "cat /home/tester/support-probe.json"))
            assert support["format"] == "agentic-support-bundle-v1"
            (ROOT / "evidence" / ("installed-rescue-" + profile + ".json")).write_text(json.dumps({
                "runtime_location": "disposable_outer_linux_vm", "controller_stopped": True,
                "diagnostics": rescue, "support_bundle": "created", "cloud_login": "not_run"}, indent=2) + "\n")
        finally:
            command(*ssh, compose + " up -d --wait controller")
        phase("installed_sandbox_quota_and_fencing")
        sandbox_result = subprocess.run([*ssh, "sudo python3 -"], capture_output=True, text=True,
                                       input=(ROOT / "tests/installed_sandbox_probe.py").read_text(),
                                       timeout=SANDBOX_PROBE_TIMEOUT_SECONDS)
        # Preserve the probe's allowlisted diagnostics before asserting a failure;
        # never publish arbitrary SSH stderr or workspace contents.
        sandbox_probe = probe_json(sandbox_result, ROOT / "evidence" / ("installed-process-sandbox-" + profile + ".json"))
        (ROOT / "evidence" / ("installed-sandbox-" + profile + ".json")).write_text(
            json.dumps(sandbox_probe, indent=2) + "\n")
        assert sandbox_result.returncode == 0 and sandbox_probe["result"] == "passed", sandbox_probe
        phase("installed_coding_and_independent_verifier")
        coding_result = subprocess.run([*ssh, "sudo python3 -"], capture_output=True, text=True,
                                      input=(ROOT / "tests/installed_coding_probe.py").read_text(),
                                      timeout=CODING_PROBE_TIMEOUT_SECONDS)
        coding_probe = probe_json(coding_result, ROOT / "evidence" / ("installed-process-coding-" + profile + ".json"))
        coding_probe["installed_source"] = source_lock["commit"]
        (ROOT / "evidence" / ("installed-coding-" + profile + ".json")).write_text(json.dumps(coding_probe, indent=2) + "\n")
        assert coding_result.returncode == 0 and coding_probe["status"] == "passed", coding_probe
        # Exercise the real first-install shipped-image probe/activation
        # on this disposable OS, without logging into any model account.
        phase("release_hermes_probe_and_activation")
        latest_result = subprocess.run([*ssh, "agentic-home bootstrap_hermes"], capture_output=True,
                                       text=True, timeout=1500)
        bootstrap_hermes = retain_bootstrap_result(ssh, latest_result, iso_hash,
            ROOT / "evidence" / ("installed-hermes-" + profile + ".json"))
        assert latest_result.returncode == 0 and bootstrap_hermes["state"] == "ready", bootstrap_hermes
        assert bootstrap_hermes["source"] == "release"
        phase("release_hermes_runtime_revision_readback")
        def hermes_image():
            identifier = command(*ssh, compose + " images -q hermes")
            assert re.fullmatch(r"(?:sha256:)?[a-f0-9]{12,64}", identifier)
            return command(*ssh, "sudo docker image inspect --format '{{.Id}}' " + identifier)
        candidate_id = hermes_image()
        runtime_revision = json.loads(command(*ssh, "python3 -", input='''
import http.cookiejar,json,urllib.request
from pathlib import Path
base='http://localhost:8099'
o=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
r=urllib.request.Request(base+'/api/host/login',data=json.dumps({'key':Path('/var/lib/agentic-home/controller/access-key').read_text().strip()}).encode(),headers={'Origin':base,'Content-Type':'application/json'})
o.open(r).close()
print(json.dumps(json.load(o.open(base+'/api/hermes'))['runtime']['upstream_commit']))
'''))
        assert runtime_revision == bootstrap_hermes["commit"]
        phase("nested_haos_provision")
        command(*ssh, "agentic-home provision")
        host = json.loads(command(*ssh, "agentic-home status"))
        assert host["vm"] == "running" and host["provisioned"]
        def ha_ready():
            response = subprocess.run([*ssh, "curl --max-time 3 -fsS http://localhost:8123/api/onboarding"],
                                      capture_output=True)
            try:
                return response.returncode == 0 and isinstance(json.loads(response.stdout), list)
            except ValueError:
                return False
        def wait_ha(stage):
            phase(stage)
            if not wait_until(ha_ready, 1800):
                raise AssertionError("Nested HAOS HTTP did not become ready")

        wait_ha("nested_haos_first_boot")
        phase("installed_controller_update_rollback")
        update_probe = installed_update(ssh, (ROOT / "tests/installed_update_probe.py").read_text(),
                                        ROOT / "evidence" / ("installed-update-" + profile + ".json"))
        assert update_probe["state"] == "passed", update_probe
        phase("nested_haos_shutdown_snapshot_restore")
        command(*ssh, "agentic-home shutdown")
        assert json.loads(command(*ssh, "agentic-home status"))["vm"] == "shut off"
        assert command(*ssh, "curl -fsS http://localhost:8099/api/deployment")
        command(*ssh, "agentic-home snapshot")
        snapshot = json.loads(command(*ssh, "agentic-home status"))["snapshots"][0]["id"]
        command(*ssh, "agentic-home restore --snapshot " + snapshot)
        command(*ssh, "agentic-home start")
        wait_ha("nested_haos_restored_boot")
        phase("outer_reboot_and_haos_autostart")
        outer_reboot_observation = reboot_outer(ssh, 1800)
        assert json.loads(command(*ssh, "agentic-home status"))["vm"] == "running"
        assert hermes_image() == candidate_id
        # The same Runner policy must remain effective after all update/restore
        # paths and a real outer reboot; reading /status must not execute Grok.
        assert "AGENTIC_DISABLED_PROVIDERS=grok" in command(*ssh,
            "sudo cat /var/lib/agentic-home/host/runtime.env").splitlines()
        policy_result = subprocess.run([*ssh, "python3 - --provisioned"], capture_output=True, text=True,
                                      timeout=AUTH_PROBE_TIMEOUT_SECONDS,
                                      input=(ROOT / "tests/installed_probe.py").read_text())
        # The initial probe also expects bootstrap idle / unpaired Home runtime;
        # both remain true in this explicit fixture-driven provisioning flow.
        policy_probe = probe_json(policy_result, ROOT / "evidence" / ("installed-process-auth-reboot-" + profile + ".json"))
        assert policy_result.returncode == 0 and policy_probe.get("excluded_cli_probes") == {
            "grok": "not_run: owner instruction"}, policy_probe
        wait_ha("nested_haos_after_outer_reboot")
        lan_report = installed_lan(ssh, name, phase, lan_images,
                                   partial_path=lan_partial_path(request, profile), profile=profile, iso_hash=iso_hash)
        lan_report["iso_sha256"] = iso_hash
        (ROOT / "evidence" / ("installed-ha-lan-" + profile + ".json")).write_text(json.dumps(lan_report, indent=2) + "\n")
        evidence = {"kind": "real_linux_install_nested_kvm", "profile": profile, "base": "Ubuntu 24.04.5",
                    "iso_sha256": iso_hash,
                    "installed_version": installed["version"], "images": image_lock,
                    "source_commit": source_lock["commit"],
                    "host_coding": coding_probe["status"], "controller_update_rollback": update_probe["state"],
                    "tailscale_installed_without_account": "1.104.1",
                    "runtime_location": "disposable_outer_linux_vm", "model_backend": "coding_transport_fixture",
                    "ha_backend": "nested_official_haos", "device_kind": "none",
                    "installation": "passed", "firstboot": "passed", "haos_vm_running": True,
                    "live_hardware_preflight_executed": True, "wired_lan_qualification": lan_report["result"],
                    "isolated_lan_test_images": lan_images,
                    "outer_ui_with_haos_off": True, "cold_snapshot_restore": "passed",
                    "firstboot_without_haos": True, "hermes_service_without_account": "needs_auth",
                    "console_rescue_with_controller_stopped": "passed",
                    "desktop_single_use_login": "passed", "bootstrap_get_has_no_side_effect": "passed",
                    "release_hermes": {**bootstrap_hermes, "image_id": candidate_id,
                                      "runtime_revision_readback": "passed", "persists_after_reboot": True},
                    "outer_reboot_haos_autostart": "passed",
                    "outer_reboot_observation": outer_reboot_observation,
                    "fixture_resources": progress["fixture_resources"],
                    "ha_readiness_check": "real Core onboarding API JSON list after startup, restore and outer reboot",
                    "duration_seconds": round(time.time() - started),
                    "installer_inputs": "Disposable disk serial + ephemeral SSH key supplied by fixture",
                    "physical_laptop": "not_run", "cloud_login": "not_run",
                    "excluded_cli_probes": {"grok": "not_run: owner instruction"}}
        report_name = "linux-install-server.json" if profile == "server-amd64" else "linux-install.json"
        (ROOT / "evidence" / report_name).write_text(json.dumps(evidence, indent=2) + "\n")
        progress["result"] = "passed"
        phase("completed")
    except Exception as exc:
        progress.update(result="failed", error_type=type(exc).__name__)
        try:
            phase(progress["phase"])
        except Exception:
            pass
        try:
            record_install_failure(exc, progress["phase"],
                                   ROOT / "evidence" / ("installed-failure-" + profile + ".json"), locals().get("ssh"))
        except Exception:
            # Diagnostic failure cannot replace the original exception or bypass cleanup.
            progress["failure_diagnostics"] = "unavailable"
        raise
    finally:
        finish_installer_cleanup(name, tmp_path, ROOT / "build/linux-install-console.log",
                                 ROOT / "evidence" / ("installed-cleanup-" + profile + ".json"),
                                 primary_error=sys.exc_info()[1])
