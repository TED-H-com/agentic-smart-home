"""First-install Hermes preparation, owned by the fixed host broker.

No model output, URL, command, image name or filesystem path is accepted here.
The upstream revision is resolved once and the candidate retains the product's
locked dependencies and adapter. Incompatible upstream code fails before use.
This is deliberately not a general installed-system updater.
"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
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
              "steps": [], "native_path": "/onboarding.html", "http_status": None}
    try:
        if opener is None:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open
        with opener(HA_URL, timeout=2) as response:
            body = response.read(16385)
        result["http_status"] = 200
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
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403, 404, 502, 503}:
            result["http_status"] = exc.code
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
            key, separator, value = line.partition("=")
            if not separator or key not in allowed or not re.fullmatch(r"sha256:[a-f0-9]{64}", value):
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
    def __init__(self, manager, resolver=latest_source):
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
        ready = False
        try:
            result = self.run("ip", "-json", "route", "show", "default", timeout=3, check=False)
            routes = json.loads(result.stdout)
            ready = result.returncode == 0 and isinstance(routes, list) and any(
                isinstance(r, dict) and r.get("dev") and r.get("type", "unicast") == "unicast"
                for r in routes
            )
        except Exception:
            pass
        return {"network": {"ready": bool(ready), "mode": "automatic", "remote_access": "deferred",
                            "observation": "outer_host_default_route", "internet_verified": False},
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
        if shutil.disk_usage(self.root).free < 5 * 1024**3:
            raise SetupError("hermes_install_disk_space")
        images = json.loads((self.product / "images.lock.json").read_text())
        originals = [value for key, value in images.items() if re.fullmatch(r"agentic-home-hermes:[0-9.]+", key)]
        if len(originals) != 1 or not re.fullmatch(r"sha256:[a-f0-9]{64}", originals[0]):
            raise SetupError("bundled_image_invalid")
        original = originals[0]
        # Pin the fallback by digest too. Never trust mutable tags after failure.
        write_environment(self.environment, original)
        record = {"state": "installing", "source": "latest", "commit": None, "started_at": time.time()}
        atomic(self.path, record)
        stopped, activated = False, False
        container = "agentic-hermes-bootstrap-" + uuid.uuid4().hex
        tag = "agentic-hermes-bootstrap:" + uuid.uuid4().hex
        try:
            with tempfile.TemporaryDirectory(prefix="hermes-install-", dir=self.root) as temporary:
                work = Path(temporary)
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
                # Use the trusted bundled Dockerfile, adapter and hash-locked
                # requirements. Only the immutable upstream source changes.
                self.run("docker", "build", "--tag", tag, str(context), timeout=1200)
                image = self.run("docker", "image", "inspect", "--format", "{{.Id}}", tag).stdout.strip()
                if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
                    raise SetupError("candidate_image_invalid")
                scratch = work / "probe"
                scratch.mkdir()
                os.chown(scratch, 10003, 10003)
                os.chmod(scratch, 0o700)
                self.run("docker", "run", "--rm", "--name", container, "--network", "none", "--read-only",
                         "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--memory", "512m",
                         "--pids-limit", "64", "--cpus", "1", "--user", "10003:10003",
                         "--mount", f"type=bind,src={scratch},dst=/work", "--workdir", "/work",
                         "--env", "HOME=/work", "--env", "HERMES_HOME=/work", "--env", "TMPDIR=/work",
                         "--env", "PYTHONPATH=/opt/hermes:/app", image,
                         "python", "/app/compatibility_probe.py", timeout=90)
                # Stop then re-check: a second browser might have begun auth
                # during the build. Never replace an authenticated runtime.
                self.compose("stop", "hermes", timeout=60)
                stopped = True
                initial_state(self.data)
                write_environment(self.environment, image)
                self.compose("up", "-d", "--no-deps", "--wait", "hermes", timeout=120)
                activated = True
                record.update(state="ready", image_id=image, compatibility="passed", finished_at=time.time())
                atomic(self.path, record)
                return self.state()
        except Exception as exc:
            code = str(exc) if isinstance(exc, SetupError) else "hermes_install_failed"
            state = "failed"
            if stopped and not activated:
                try:
                    write_environment(self.environment, original)
                    self.compose("up", "-d", "--no-deps", "--wait", "hermes", timeout=120)
                except Exception:
                    state, code = "needs_reconcile", "hermes_fallback_unconfirmed"
            record.update(state=state, error_code=code, finished_at=time.time())
            atomic(self.path, record)
            raise SetupError(code) from None
        finally:
            # Names belong to this attempt. No wildcard/container-wide cleanup.
            self.run("docker", "rm", "-f", container, timeout=30, check=False)
            self.run("docker", "image", "rm", tag, timeout=30, check=False)
