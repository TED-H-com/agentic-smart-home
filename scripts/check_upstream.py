#!/usr/bin/env python3
"""Build and test an isolated Hermes candidate; never activate or change locks.

This is a developer/CI command, not an installed-product upgrade command. The
candidate keeps our pinned dependency lock and adapter. All model/OAuth/MCP
traffic during the gate is local synthetic transport inside a network-none
container. The report is evidence of those interfaces, not real-account login.
"""
import argparse
import hashlib
import json
import os
import re
import resource
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "host"))
from agentic_host.bootstrap import SetupError, latest_source  # noqa: E402


class CompatibilityFailure(Exception):
    pass


def limit_output():
    # Docker's client writes the captured output, so this limits the disk files
    # without granting any extra capability to the candidate container.
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))


def validate_gate(result):
    if result.returncode != 0:
        raise CompatibilityFailure("candidate_probe_failed")
    if len(result.stdout.encode()) > 65536:
        raise CompatibilityFailure("candidate_probe_invalid")
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise CompatibilityFailure("candidate_probe_invalid") from None
    if (not isinstance(value, dict) or value.get("compatible") is not True
            or type(value.get("adapter_contract")) is not int or value["adapter_contract"] != 1
            or value.get("observed_from") != "runtime_container"
            or value.get("cloud_account_verified") is not False):
        raise CompatibilityFailure("candidate_contract_mismatch")
    return value


def check(selection, *, run=subprocess.run, resolver=latest_source, root=ROOT, scratch=None):
    if selection not in {"locked", "latest"}:
        raise ValueError("unsupported candidate selector")
    scratch = scratch or Path.home() / "agent-work/tmp/compatibility"
    scratch.mkdir(parents=True, exist_ok=True)
    identifier = "agentic-upstream-check-" + uuid.uuid4().hex
    image, volume = identifier + ":candidate", identifier + "-data"
    report = {"adapter_contract": 1, "component": "hermes", "selection": selection,
              "state": "failed", "activation": "never", "cloud_account": "not_used",
              "model_transport": "loopback_fixture", "observed_from": "isolated_candidate_container",
              "limitations": ["Not a Linux or HAOS upgrade test", "No real OAuth consent or cloud inference",
                             "Compatibility evidence covers tested adapter surfaces, not arbitrary future upstream changes"]}
    try:
        with tempfile.TemporaryDirectory(prefix=identifier + "-", dir=scratch) as temporary:
            work = Path(temporary)
            context = work / "hermes"
            shutil.copytree(root / "hermes", context, symlinks=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
            source_files = [p for p in context.rglob("*") if p.is_file()]
            if any(p.is_symlink() for p in context.rglob("*")):
                raise CompatibilityFailure("candidate_context_invalid")
            report["source_hashes"] = {"hermes/" + str(p.relative_to(context)): hashlib.sha256(p.read_bytes()).hexdigest()
                                       for p in sorted(source_files)}
            lock_path = context / "hermes.lock.json"
            lock = json.loads(lock_path.read_text())
            if selection == "latest":
                lock.update(resolver(work))
                lock_path.write_text(json.dumps(lock, indent=2) + "\n")
            # Preserve both checked-out input provenance and the exact generated
            # candidate context; latest changes only this copied upstream lock.
            report["candidate_context_hashes"] = {
                "hermes/" + str(p.relative_to(context)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(source_files)}
            report["upstream"] = {key: lock[key] for key in ("repository", "commit", "sha256")}
            with (work / "build.log").open("w") as log:
                built = run(["docker", "build", "--quiet", "-t", image, str(context)],
                            stdout=log, stderr=subprocess.STDOUT, timeout=900, check=False, preexec_fn=limit_output)
            if built.returncode:
                raise CompatibilityFailure("candidate_build_failed")
            inspected = run(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                            capture_output=True, text=True, timeout=20, check=True)
            digest = inspected.stdout.strip()
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
                raise CompatibilityFailure("candidate_image_invalid")
            report["candidate_image"] = digest
            args = ["docker", "run", "--rm", "--name", identifier, "--network=none", "--read-only",
                          "--cap-drop=ALL", "--security-opt=no-new-privileges", "--memory=512m", "--cpus=1",
                          "--pids-limit=64", "--user=10003:10003", "--log-driver=none",
                          "--mount", "type=volume,src=" + volume + ",dst=/data",
                          "-e", "HOME=/data", "-e", "HERMES_HOME=/data", "-e", "TMPDIR=/data",
                          "-e", "PYTHONPATH=/app:/opt/hermes", "--entrypoint=python", digest,
                          "/app/compatibility_probe.py"]
            with (work / "stdout").open("w") as out, (work / "stderr").open("w") as err:
                process = run(args, stdout=out, stderr=err, timeout=180, check=False, preexec_fn=limit_output)
            stdout = (work / "stdout").read_bytes()[:65537].decode("utf-8", errors="replace")
            stderr = (work / "stderr").read_bytes()[:4096].decode("utf-8", errors="replace")
            report["probe"] = {"returncode": process.returncode,
                               "stdout_bytes": (work / "stdout").stat().st_size,
                               "stderr_bytes": (work / "stderr").stat().st_size,
                               "import_error": "ImportError" in stderr or "ModuleNotFoundError" in stderr,
                               "timeout": "TimeoutError" in stderr, "assertion_failed": "AssertionError" in stderr}
            result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
            gate = validate_gate(result)
            report.update(state="passed", gate={key: gate[key] for key in
                ("compatible", "adapter_contract", "observed_from", "cloud_account_verified")})
    except CompatibilityFailure as exc:
        report["error_code"] = str(exc)
    except (OSError, ValueError, KeyError, AttributeError, SetupError, tarfile.TarError, subprocess.SubprocessError):
        report["error_code"] = "candidate_check_unavailable"
    finally:
        cleaned = True
        # Only generated names created by this invocation are removed. Even a
        # timeout must try all cleanup actions; no running test container is kept.
        for args in (["docker", "rm", "-fv", identifier], ["docker", "volume", "rm", volume],
                     ["docker", "image", "rm", "-f", image]):
            try:
                result = run(args, capture_output=True, text=True, timeout=30, check=False)
                if result.returncode and not any(message in result.stderr.lower() for message in
                    ("no such container", "no such volume", "no such image")):
                    cleaned = False
            except (OSError, subprocess.SubprocessError):
                cleaned = False
        report["cleanup_completed"] = cleaned
        if not cleaned:
            report.update(state="failed", error_code="candidate_cleanup_unconfirmed")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes", choices=("locked", "latest"), default="locked")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    report = check(args.hermes)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: report[key] for key in ("state", "component", "selection", "activation", "cleanup_completed")}))
    raise SystemExit(report["state"] != "passed")


if __name__ == "__main__":
    main()
