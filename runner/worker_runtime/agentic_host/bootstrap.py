"""First-install Hermes preparation, owned by the fixed host broker.

No model output, URL, command, image name or filesystem path is accepted here.
Production activates the digest shipped with the release, after an offline
adapter check. Upstream discovery is reserved for maintainer candidate tooling.
This is deliberately not a general installed-system updater.
"""

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .manager import HostError, atomic, sync_path

REPOSITORY = "https://github.com/NousResearch/hermes-agent"
HEAD_URL = "https://api.github.com/repos/NousResearch/hermes-agent/commits/HEAD"
ARCHIVE_BASE = "https://codeload.github.com/NousResearch/hermes-agent/tar.gz/"
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_SOURCE = 512 * 1024 * 1024
HA_URL = "http://192.168.177.2:80/api/onboarding"

INSTALL_DIAGNOSTIC_PHASES = frozenset({
    "workspace_preparation", "image_inspection", "probe_preparation", "offline_probe",
    "runtime_stop", "account_recheck", "environment_activation", "runtime_activation",
    "state_commit", "temporary_cleanup", "state_readback", "fallback_environment", "fallback_activation",
})


def install_diagnostic(phase, error, returncode=None):
    """Private fixed categories only; never persist exception text or process streams."""
    if phase not in INSTALL_DIAGNOSTIC_PHASES:
        raise ValueError("invalid_install_diagnostic_phase")
    exit_category = "unavailable"
    if type(returncode) is int:
        exit_category = {
            0: "success", 1: "rejected", 125: "docker_error", 126: "command_not_executable",
            127: "command_not_found", 137: "killed", 143: "terminated", -9: "killed", -15: "terminated",
        }.get(returncode, "signal" if returncode < 0 else "nonzero")
    if isinstance(error, subprocess.TimeoutExpired):
        reason = "timeout"
    elif isinstance(error, SetupError):
        reason = ("state_conflict" if str(error) in {
            "existing_hermes_account", "existing_hermes_state", "existing_hermes_sessions"}
            else "command_failed" if type(returncode) is int and returncode != 0 else "invalid_contract")
    elif isinstance(error, HostError):
        reason = "command_failed"
    elif isinstance(error, OSError):
        reason = "filesystem_error"
    else:
        reason = "unexpected_error"
    return {"schema": 1, "phase": phase, "reason": reason, "exit_category": exit_category}


def probe_failure_diagnostic(result, returncode):
    """Only a failed shipped probe's exact finite diagnostic can cross this boundary."""
    if (type(returncode) is not int or returncode == 0 or not isinstance(result, dict)
            or set(result) != {"compatible", "adapter_contract", "error", "cloud_account_verified", "diagnostic"}
            or result["compatible"] is not False or type(result["adapter_contract"]) is not int
            or result["adapter_contract"] != 1 or result["error"] != "upstream_contract_incompatible"
            or result["cloud_account_verified"] is not False):
        return None
    value = result["diagnostic"]
    if (not isinstance(value, dict) or set(value) != {"schema", "stage", "kind"}
            or type(value["schema"]) is not int or value["schema"] != 1
            or not isinstance(value["stage"], str) or value["stage"] not in {
                "consumed_signatures", "chat_tool_roundtrip", "continued_history", "provider_401",
                "native_device_oauth", "native_responses_tool_roundtrip", "fixture_finalization"}
            or not isinstance(value["kind"], str) or value["kind"] not in {
                "child_timeout", "child_exit", "invalid_output", "contract_failure", "fixture_protocol", "unexpected"}):
        return None
    return {"schema": 1, "stage": value["stage"], "kind": value["kind"]}


def probe_json(output):
    """Bound private probe input before consuming any success or failure fields."""
    if not isinstance(output, str) or len(output) > 65536 or len(output.encode("utf-8")) > 65536:
        raise ValueError("invalid_probe_output")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("invalid_probe_output")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("invalid_probe_output")

    result = json.loads(output, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    pending = [(result, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 32:
            raise ValueError("invalid_probe_output")
        if isinstance(value, str):
            value.encode("utf-8")  # Reject lone surrogates, including escaped JSON strings.
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError("invalid_probe_output")
        elif isinstance(value, dict):
            pending.extend((item, depth + 1) for pair in value.items() for item in pair)
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
    return result


class SetupError(HostError):
    """Only fixed error codes cross the root broker boundary."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SetupError("upstream_redirect_refused")


def open_public(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc not in {"api.github.com", "codeload.github.com"}:
        raise SetupError("upstream_origin_refused")
    request = urllib.request.Request(url, headers={"User-Agent": "Agentic-Home-bootstrap", "Accept": "application/vnd.github+json"})
    # No environment proxy and no redirects into a private address/origin.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(request, timeout=30)


def latest_source(directory, opener=open_public):
    with opener(HEAD_URL) as response:
        body = response.read(65537)
    if len(body) > 65536:
        raise SetupError("upstream_metadata_invalid")
    metadata = json.loads(body)
    commit = metadata.get("sha")
    if not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise SetupError("upstream_revision_invalid")
    archive = Path(directory) / "source.tar.gz"
    digest, total, deadline = hashlib.sha256(), 0, time.monotonic() + 180
    with opener(ARCHIVE_BASE + commit) as response, archive.open("xb") as stream:
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_ARCHIVE or time.monotonic() > deadline:
                raise SetupError("upstream_archive_limit")
            digest.update(chunk)
            stream.write(chunk)
    # Validate the extraction surface before giving it to the image builder.
    expanded, count = 0, 0
    with tarfile.open(archive) as source:
        for member in source:
            parts = Path(member.name).parts
            if not parts or parts[0] != "hermes-agent-" + commit or ".." in parts:
                raise SetupError("upstream_archive_path")
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise SetupError("upstream_archive_special_file")
            expanded += member.size
            count += 1
            if expanded > MAX_SOURCE or count > 50000 or time.monotonic() > deadline:
                raise SetupError("upstream_archive_limit")
    return {"repository": REPOSITORY, "commit": commit, "archive_url": ARCHIVE_BASE + commit,
            "sha256": digest.hexdigest()}


def native_ha_status(opener=None):
    result = {"reachable": False, "onboarding_available": False, "onboarding_complete": False,
              "steps": [], "native_path": "/onboarding.html", "http_status": None,
              "contract": {"state": "unreachable", "error_code": "ha_onboarding_unavailable",
                           "observed_from": "outer_host_to_core_onboarding_api", "stability": "upstream_private_volatile"}}
    try:
        if opener is None:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open
        with opener(HA_URL, timeout=2) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                result["contract"].update(state="unsupported", error_code="ha_onboarding_contract_changed")
                return result
            body = response.read(16385)
        result["http_status"] = 200
        result["contract"].update(state="unsupported", error_code="ha_onboarding_contract_changed")
        if len(body) > 16384:
            return result
        steps = json.loads(body)
        allowed = {"user", "core_config", "integration", "analytics"}
        if not isinstance(steps, list) or not 1 <= len(steps) <= 8:
            return result
        if any(not isinstance(s, dict) or s.get("step") not in allowed or type(s.get("done")) is not bool for s in steps):
            return result
        clean = [{"step": s["step"], "done": s["done"]} for s in steps]
        ids = {s["step"] for s in clean}
        if len(ids) != len(clean) or not {"user", "core_config", "integration"} <= ids:
            return result
        complete = all(s["done"] for s in clean)
        result.update(reachable=True, onboarding_available=not complete, onboarding_complete=complete, steps=clean)
        result["contract"].update(state="supported", error_code=None)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403, 404, 502, 503}:
            result["http_status"] = exc.code
        if exc.code == 404:
            result["contract"].update(state="route_absent", error_code="ha_onboarding_route_absent")
    except Exception:
        pass
    return result


def initial_state(directory):
    """Reject an existing account/session, including a logged-out used install."""
    directory = Path(directory)
    for name in ("provider.json", "auth.json"):
        path = directory / name
        if path.is_symlink() or (path.exists() and json.loads(path.read_text())):
            raise SetupError("existing_hermes_account")
    oauth = directory / "oauth"
    if oauth.is_symlink() or (oauth.exists() and any(oauth.iterdir())):
        raise SetupError("existing_hermes_account")
    database = directory / "runtime.sqlite"
    if database.is_symlink():
        raise SetupError("existing_hermes_state")
    if database.exists():
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
            if db.execute("SELECT 1 FROM records WHERE kind IN ('run','session','auth') LIMIT 1").fetchone():
                raise SetupError("existing_hermes_sessions")
            if db.execute("SELECT 1 FROM requests LIMIT 1").fetchone():
                raise SetupError("existing_hermes_sessions")


def write_environment(path, image, variable="AGENTIC_HERMES_IMAGE"):
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise SetupError("candidate_image_invalid")
    path = Path(path)
    allowed = {"AGENTIC_HERMES_IMAGE", "AGENTIC_CONTROLLER_IMAGE"}
    if variable not in allowed:
        raise SetupError("candidate_environment_invalid")
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, separator, value = line.partition("=")
            # Image activation must preserve the administrator's provider policy.
            # It may never add arbitrary Compose variables or shell expansion.
            policy = (key == "AGENTIC_DISABLED_PROVIDERS" and len(value) <= 128
                      and (not value.strip() or re.fullmatch(
                          r"[ \t]*(?:codex|claude|agy|grok)[ \t]*(?:,[ \t]*(?:codex|claude|agy|grok)[ \t]*)*", value)))
            pinned_image = key in allowed and re.fullmatch(r"sha256:[a-f0-9]{64}", value)
            if not separator or key in values or not (policy or pinned_image):
                raise SetupError("candidate_environment_invalid")
            values[key] = value
    values[variable] = image
    temporary = path.with_suffix(".new")
    with temporary.open("w") as stream:
        os.chmod(temporary, 0o600)
        stream.write("".join(key + "=" + values[key] + "\n" for key in sorted(values)))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    sync_path(path.parent)


class InitialSetup:
    def __init__(self, manager, resolver=None):
        self.manager = manager
        self.root, self.product, self.run = manager.root, manager.product, manager.run
        self.path = self.root / "bootstrap-hermes.json"
        self.environment = self.root / "runtime.env"
        self.data = self.root.parent / "hermes"
        self.resolver = resolver

    def state(self):
        if self.path.exists():
            data = json.loads(self.path.read_text())
            return {k: data[k] for k in ("state", "commit", "source", "error_code") if k in data}
        lock = self.product / "hermes/hermes.lock.json"
        commit = json.loads(lock.read_text()).get("commit") if lock.exists() else None
        return {"state": "bundled", "commit": commit, "source": "bundled"}

    def status(self):
        from .compatibility import network_status
        return {"network": network_status(self.run),
                "hermes": self.state(), "ha": native_ha_status()}

    def compose(self, *args, **kwargs):
        return self.run("docker", "compose", "--env-file", str(self.environment), "-f",
                        str(self.product / "host/compose.yaml"), *args, **kwargs)

    def install(self):
        previous = self.state()
        if previous["state"] == "ready":
            return previous
        if previous["state"] in {"installing", "needs_reconcile"}:
            raise SetupError("hermes_install_needs_reconcile")
        initial_state(self.data)
        self.manager.require_compatibility("bootstrap_hermes")
        if shutil.disk_usage(self.root).free < 5 * 1024**3:
            raise SetupError("hermes_install_disk_space")
        images = json.loads((self.product / "images.lock.json").read_text())
        originals = [value for key, value in images.items() if re.fullmatch(r"agentic-home-hermes:[0-9.]+", key)]
        if len(originals) != 1 or not re.fullmatch(r"sha256:[a-f0-9]{64}", originals[0]):
            raise SetupError("bundled_image_invalid")
        original = originals[0]
        # Pin the fallback by digest too. Never trust mutable tags after failure.
        write_environment(self.environment, original)
        bundled = json.loads((self.product / "hermes/hermes.lock.json").read_text())
        if not re.fullmatch(r"[a-f0-9]{40}", str(bundled.get("commit", ""))):
            raise SetupError("bundled_image_invalid")
        selection = "latest" if self.resolver else "release"
        record = {"state": "installing", "source": selection, "commit": bundled["commit"], "started_at": time.time()}
        atomic(self.path, record)
        stopped, activated = False, False
        container = "agentic-hermes-bootstrap-" + uuid.uuid4().hex
        tag = "agentic-hermes-bootstrap:" + uuid.uuid4().hex
        phase, returncode = "workspace_preparation", None
        try:
            with tempfile.TemporaryDirectory(prefix="hermes-install-", dir=self.root) as temporary:
                work = Path(temporary)
                image = original
                if self.resolver:
                    # Explicit test/maintainer injection only; installed broker
                    # has no request field or switch to enable source discovery.
                    source = self.resolver(work)
                    record.update(source, source="latest")
                    atomic(self.path, record)
                    context = work / "context"
                    shutil.copytree(self.product / "hermes", context,
                                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                    lock_path = context / "hermes.lock.json"
                    lock = json.loads(lock_path.read_text())
                    lock.update(source)
                    lock_path.write_text(json.dumps(lock, indent=2) + "\n")
                    self.run("docker", "build", "--tag", tag, str(context), timeout=1200)
                    image = self.run("docker", "image", "inspect", "--format", "{{.Id}}", tag).stdout.strip()
                else:
                    phase = "image_inspection"
                    inspected = self.run("docker", "image", "inspect", "--format", "{{.Id}}", original).stdout.strip()
                    if inspected != original:
                        raise SetupError("bundled_image_invalid")
                if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
                    raise SetupError("candidate_image_invalid")
                phase = "probe_preparation"
                scratch = work / "probe"
                scratch.mkdir()
                os.chown(scratch, 10003, 10003)
                os.chmod(scratch, 0o700)
                phase = "offline_probe"
                probe = self.run("docker", "run", "--rm", "--name", container, "--network", "none", "--read-only",
                         "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--memory", "512m",
                         "--pids-limit", "64", "--cpus", "1", "--user", "10003:10003",
                         "--mount", f"type=bind,src={scratch},dst=/work", "--workdir", "/work",
                         "--env", "HOME=/work", "--env", "HERMES_HOME=/work", "--env", "TMPDIR=/work",
                         "--env", "PYTHONPATH=/opt/hermes:/app", image,
                         "python", "/app/compatibility_probe.py", timeout=90, check=False)
                returncode = probe.returncode
                result = None
                try:
                    result = probe_json(probe.stdout)
                    valid = (isinstance(result, dict) and result.get("compatible") is True
                             and type(result.get("adapter_contract")) is int
                             and result["adapter_contract"] == 1
                             and result.get("observed_from") == "runtime_container"
                             and result.get("cloud_account_verified") is False)
                except (ValueError, TypeError, RecursionError):
                    valid = False
                diagnostic = probe_failure_diagnostic(result, returncode)
                if diagnostic is not None:
                    record["probe_diagnostic"] = diagnostic
                if not valid or type(returncode) is not int or returncode != 0:
                    raise SetupError("hermes_adapter_contract_unavailable")
                # Stop then re-check: a second browser might have begun auth
                # during the probe. Never replace an authenticated runtime.
                phase, returncode = "runtime_stop", None
                self.compose("stop", "hermes", timeout=60)
                stopped = True
                phase = "account_recheck"
                initial_state(self.data)
                phase = "environment_activation"
                write_environment(self.environment, image)
                phase = "runtime_activation"
                self.compose("up", "-d", "--no-deps", "--wait", "hermes", timeout=120)
                activated = True
                phase = "state_commit"
                record.update(state="ready", image_id=image, compatibility="passed", finished_at=time.time())
                atomic(self.path, record)
                phase = "temporary_cleanup"
            phase = "state_readback"
            return self.state()
        except Exception as exc:
            record["diagnostic"] = install_diagnostic(phase, exc, returncode)
            code = str(exc) if isinstance(exc, SetupError) else "hermes_install_failed"
            state = "failed"
            if stopped and not activated:
                try:
                    phase = "fallback_environment"
                    write_environment(self.environment, original)
                    phase = "fallback_activation"
                    self.compose("up", "-d", "--no-deps", "--wait", "hermes", timeout=120)
                except Exception as fallback_error:
                    record["fallback_diagnostic"] = install_diagnostic(phase, fallback_error)
                    state, code = "needs_reconcile", "hermes_fallback_unconfirmed"
            record.update(state=state, error_code=code, finished_at=time.time())
            atomic(self.path, record)
            raise SetupError(code) from None
        finally:
            # Names belong to this attempt. No wildcard/container-wide cleanup.
            self.run("docker", "rm", "-f", container, timeout=30, check=False)
            if self.resolver:
                self.run("docker", "image", "rm", tag, timeout=30, check=False)
