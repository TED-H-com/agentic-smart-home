"""HAOS-local coding service; shared dispatch, independent sandbox and credentials."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from agentic_host.coding_relay import Relay
from agentic_host.coding_sandbox import SandboxError, command
from agentic_host.coding_service import CodingService

from . import VERSION

REPOSITORY = "https://github.com/teddashh/agentic-smart-home.git"
PACKAGED_SOURCE = Path("/opt/agentic-worker/source")
# A separate, short-lived process bounds DNS as well as HTTP. Neither env nor argv contains credentials.
NETWORK_OBSERVATION = r'''
import http.client,json,socket
value={'dns_resolved':False,'reachable':False,'http_status':None,'http_port':None,'attempted_ports':[],'endpoints':[]}
try:
    socket.getaddrinfo('homeassistant',80,type=socket.SOCK_STREAM)
    value['dns_resolved']=True
    for port in (80,8123):
        value['attempted_ports'].append(port)
        endpoint={'port':port,'reachable':False,'http_status':None,'error_code':None}
        value['endpoints'].append(endpoint)
        connection=http.client.HTTPConnection('homeassistant',port,timeout=1)
        try:
            connection.request('GET','/api/',headers={'Accept':'application/json','Connection':'close'})
            response=connection.getresponse()
            endpoint.update(reachable=True,http_status=response.status)
            value.update(reachable=True,http_status=response.status,http_port=port)
            break
        except TimeoutError:endpoint['error_code']='timeout'
        except http.client.HTTPException:endpoint['error_code']='http_protocol_error'
        except OSError:endpoint['error_code']='connection_failed'
        finally:connection.close()
except (OSError,http.client.HTTPException):pass
print(json.dumps(value))
'''


def valid_network_receipt(value):
    if (not isinstance(value, dict) or
        set(value) != {"dns_resolved", "reachable", "http_status", "http_port", "attempted_ports", "endpoints"} or
        any(type(value[k]) is not bool for k in ("dns_resolved", "reachable"))):
        return False
    ports, endpoints = value["attempted_ports"], value["endpoints"]
    if (not isinstance(ports, list) or any(type(port) is not int for port in ports) or
        ports not in ([], [80], [80, 8123]) or not isinstance(endpoints, list) or len(endpoints) != len(ports) or
        value["dns_resolved"] != bool(ports)):
        return False
    for index, endpoint in enumerate(endpoints):
        if (not isinstance(endpoint, dict) or set(endpoint) != {"port", "reachable", "http_status", "error_code"} or
            type(endpoint["port"]) is not int or endpoint["port"] != ports[index] or
            type(endpoint["reachable"]) is not bool):
            return False
        if endpoint["reachable"]:
            if (index != len(ports) - 1 or endpoint["error_code"] is not None or
                type(endpoint["http_status"]) is not int or not 100 <= endpoint["http_status"] <= 599):
                return False
        elif endpoint["http_status"] is not None or endpoint["error_code"] not in (
                "timeout", "connection_failed", "http_protocol_error"):
            return False
    reached = bool(endpoints) and endpoints[-1]["reachable"]
    if value["reachable"] != reached:
        return False
    if reached:
        return (type(value["http_port"]) is int and type(value["http_status"]) is int and
                value["http_port"] == ports[-1] and value["http_status"] == endpoints[-1]["http_status"])
    return value["http_port"] is None and value["http_status"] is None and ports in ([], [80, 8123])


def network_observation():
    unknown = {"dns_resolved": False, "reachable": False, "http_status": None,
               "http_port": None, "attempted_ports": None, "endpoints": None, "timed_out": False}
    try:
        result = subprocess.run([sys.executable, "-I", "-c", NETWORK_OBSERVATION],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin"}, timeout=3, check=False)
        if result.returncode or len(result.stdout) > 1024:
            return {**unknown, "observation_failed": True}
        value = json.loads(result.stdout)
        if not valid_network_receipt(value):
            raise ValueError("Invalid observation")
        return {**value, "timed_out": False}
    except subprocess.TimeoutExpired:
        return {**unknown, "timed_out": True}
    except (OSError, ValueError):
        return {**unknown, "observation_failed": True}


def local_observation():
    from .home_launcher import BoundaryError, runtime_observation
    try:
        app = runtime_observation()
    except (OSError, ValueError, BoundaryError):
        app = {"available": False, "reason": "app_namespace_observation_unavailable"}
    return {"app_runtime": app, "core_api_from_app": network_observation()}


class HomeRelay(Relay):
    """Only this endpoint can observe the App network; candidate URLs are never accepted."""
    def __init__(self, *args, observer=None, version_refs=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.observer = observer or local_observation
        self.version_refs = dict(version_refs or {})
        self.observations = []
        self.observation_calls = 0

    def request(self, method, path, body):
        if path != "/observe":
            return super().request(method, path, body)
        if method != "GET" or body:
            raise SandboxError("Observation route not allowed")
        with self.lock:
            if time.time() >= self.deadline or self.calls >= 80 or self.observation_calls >= 8:
                raise SandboxError("Observation authorization exhausted")
            self.calls += 1
            self.observation_calls += 1
            sequence = self.observation_calls
        content = self.observer()
        raw = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        if len(raw) > 8192:
            raise SandboxError("Observation response limit")
        evidence = {"evidence_id": hashlib.sha256((self.path.stem + ":observe:" + str(sequence)).encode()).hexdigest()[:32],
            "observed_from": "haos_app", "target_component": "app_namespace_and_core_api_reachability",
            "collection_method": "fixed_read_only_app_probe", "observed_at": datetime.now(UTC).isoformat(),
            "version_refs": self.version_refs, "content": content, "content_digest": hashlib.sha256(raw).hexdigest(),
            "redaction": "fixed_public_fields", "limitations": [
                "App perspective only; not a Core-container or whole-HAOS health proof",
                "Core API request is unauthenticated; response bodies and redirects are never followed",
                "Only homeassistant ports 80 then 8123 are probed; custom ports and TLS are not tested",
                "No Home Assistant or Supervisor credentials are provided"]}
        with self.lock:
            if time.time() >= self.deadline:
                raise SandboxError("Observation authorization expired")
            self.observations.append(evidence)
        return 200, "application/json", json.dumps(evidence).encode()

    def evidence(self):
        with self.lock:
            return list(self.observations)


def read_lock(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4096 or
            info.st_mode & 0o022 or info.st_uid != os.geteuid()):
            raise SandboxError("Untrusted packaged source identity")
        data = json.loads(os.read(fd, 4097))
    finally:
        os.close(fd)
    if (not isinstance(data, dict) or set(data) != {"repo_id", "repository", "commit"} or
        data["repo_id"] != "agentic-home" or data["repository"] != REPOSITORY or
        not isinstance(data["commit"], str) or not re.fullmatch(r"[a-f0-9]{40}", data["commit"])):
        raise SandboxError("Invalid packaged source identity")
    return data


def validate_source(packaged=PACKAGED_SOURCE):
    """The image supplies immutable public Git; each sandbox makes its own private checkout."""
    lock = read_lock(packaged.with_suffix(".json"))
    if (packaged.is_symlink() or not packaged.is_dir() or packaged.stat().st_uid != os.geteuid() or
        packaged.stat().st_mode & 0o022):
        raise SandboxError("Packaged source unavailable")
    if command("git", "--git-dir", str(packaged), "rev-parse", "--is-bare-repository", timeout=10).stdout.strip() != "true":
        raise SandboxError("Packaged source must be bare")
    revision = command("git", "--git-dir", str(packaged), "rev-parse", "HEAD", timeout=10).stdout.strip()
    if revision != lock["commit"]:
        raise SandboxError("Packaged source revision mismatch")
    command("git", "--git-dir", str(packaged), "fsck", "--strict", "--no-reflogs", timeout=30)
    return lock


class HomeService(CodingService):
    def __init__(self, data: Path, *, sandbox_factory=None, relay_factory=None, packaged_source=PACKAGED_SOURCE,
                 runtime_ready=True):
        if sandbox_factory is None:
            from .home_sandbox import HomeSandbox
            sandbox_factory = HomeSandbox
        root = data / "coding"
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.source_error = None
        self.runtime_ready = runtime_ready
        refs = {"app": VERSION, "image": "unavailable"}
        try:
            refs["source"] = validate_source(packaged_source)["commit"]
        except (OSError, ValueError, SandboxError):
            self.source_error = "coding_source_unavailable"
        def make_relay(*args, **kwargs):
            return (relay_factory or HomeRelay)(*args, version_refs={**refs, "codex": "0.157.1"}, **kwargs)
        super().__init__(None, root=root, credential_path=data / "coding-auth/profiles/codex/auth.json",
            source=packaged_source, source_lock_path=packaged_source.with_suffix(".json"),
            worker_id="home-coder", runtime_location="haos_app", sandbox_factory=sandbox_factory,
            relay_factory=make_relay, version_refs=refs)

    def capabilities(self):
        value = super().capabilities()
        if self.source_error or not self.runtime_ready:
            value.update(available=False, state="blocked", reason=self.source_error or "hard_boundary_unavailable", modes=[])
            for provider in value["providers"]:
                provider["supported"] = False
        return value

    def collect(self, job, sandbox):
        patch = super().collect(job, sandbox)
        relay = self.relays.get(job["id"])
        if relay is not None:
            job["evidence"].extend(relay.evidence())
        status = sandbox.status(job["id"])
        counts = status.get("resource_counters")
        if isinstance(counts, dict) and set(counts) == {"memory.events", "pids.events", "cpu.stat", "cgroup.events"}:
            safe = all(isinstance(values, dict) and len(values) <= 32 and all(
                isinstance(key, str) and re.fullmatch(r"[a-z_]{1,48}", key) and
                type(value) is int and 0 <= value <= 2**63 - 1 for key, value in values.items())
                for values in counts.values())
            if safe:
                raw = json.dumps(counts, sort_keys=True, separators=(",", ":")).encode()
                job["evidence"].append({"evidence_id": hashlib.sha256((job["id"] + ":cgroup").encode()).hexdigest()[:32],
                    "observed_from": "haos_app", "target_component": "isolated_coding_worker_cgroup",
                    "collection_method": "kernel_cgroup_counters_after_writer_stop",
                    "observed_at": datetime.now(UTC).isoformat(), "version_refs": self.version_refs,
                    "content": counts, "content_digest": hashlib.sha256(raw).hexdigest(), "redaction": "fixed_public_fields",
                    "limitations": ["Worker cgroup only; not Core or whole HAOS resource health"]})
        return patch

    def handle(self, method, path, body=None):
        # Host Docker identity/reconciliation is never available from the HAOS App.
        if path in {"/coding/runtime", "/coding/reconcile-runtime"}:
            return 409, {"detail": "home_runtime_requires_local_stop_proof"}
        return super().handle(method, path, body)
