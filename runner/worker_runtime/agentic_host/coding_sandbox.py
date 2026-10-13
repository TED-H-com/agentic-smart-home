"""Offline coding containment primitives; not a cloud CLI or an HAOS executor.

Only a trusted host broker may instantiate this launcher. No existing Runner profile
is changed. Production integration is deliberately disabled until provider egress and
account isolation have their own verified adapter. See host/worker/README.md.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .manager import atomic

UID = 10000
ID = re.compile(r"[a-z0-9][a-z0-9-]{7,63}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
# A key/value secret needs a quoted literal with 8 non-space characters on one line, or a
# 20+ character token with a letter and a digit that is not a call or an index. Type hints
# and expressions such as `api_key: SecretStr` or `password=body.password` do not match.
SECRET = re.compile(rb"(?i)(-----BEGIN [A-Z ]*PRIVATE KEY-----|Bearer\s+[A-Za-z0-9._~+/-]{16,}|"
                    rb"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password)\s*[\"']?\s*[:=]\s*"
                    rb"(?:\"(?=(?:[^\"\n]*[^\s\"]){8})[^\"\n]+\"|'(?=(?:[^'\n]*[^\s']){8})[^'\n]+'|"
                    rb"(?=[\w.~+/=-]*[A-Za-z])(?=[\w.~+/=-]*[0-9])[\w.~+/=-]{20,}(?![\w.~+/=(\[-]))|"
                    rb"sk-[A-Za-z0-9_-]{16,})")
# Added patch lines are code under review, so only token-like values count there.
# A test fixture such as password="not-a-real-secret" keeps the diff readable.
PATCH_SECRET = re.compile(rb"(?i)(-----BEGIN [A-Z ]*PRIVATE KEY-----|Bearer\s+[A-Za-z0-9._~+/-]{16,}|"
                          rb"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password)\s*[\"']?\s*[:=]\s*[\"']?"
                          rb"(?=[\w.~+/=-]*[A-Za-z])(?=[\w.~+/=-]*[0-9])[\w.~+/=-]{20,}(?![\w.~+/=(\[-])|"
                          rb"sk-[A-Za-z0-9_-]{16,})")
# The Controller rejects a whole result over 180 KiB per file or with a name it refuses.
ARTIFACT_FILE_BYTES = 180 * 1024
SKIPPED_LISTED = 100
SUFFIXES = {".txt", ".md", ".json", ".py", ".patch", ".diff", ".log"}
WEB_BUNDLE_SUFFIXES = {".html", ".js", ".css", ".svg", ".json", ".txt", ".png", ".webp", ".woff2", ".ico"}
WEB_TEXT_SUFFIXES = {".html", ".js", ".css", ".svg", ".json", ".txt"}
WEB_BUNDLE_MAX_FILES = 48
WEB_BUNDLE_MAX_FILE = 3 * 1024 * 1024
WEB_BUNDLE_MAX_TOTAL = 6 * 1024 * 1024
WEB_BUNDLE_MAX_PATH = 160
WEB_BUNDLE_MAX_DEPTH = 4
FIXTURE_MAX_BYTES = 1024 * 1024
FIXTURE_KEY = re.compile(r"[a-z0-9/_-]{1,120}\Z")
# Minified DOM input-type map (`password:!0,...,week:!0};function`). The tail after
# `}` may be an optional semicolon plus letters. Digits or punctuation still match.
_BENIGN_INPUT_TYPES = re.compile(rb"password:!0(?:,[A-Za-z]+:!0)+")


ERROR_CODES = frozenset({
    "sandbox_rejected", "command_failed", "command_timeout", "root_broker_required", "invalid_docker_info",
    "resource_limits_unavailable", "docker_security_unavailable", "state_root_permissions",
    "state_ancestor_permissions", "disk_backing_required", "host_reserve_insufficient",
    "workspace_mount_unverified", "coding_setup_failed",
})
RESOURCE_KEYS = ("MemoryLimit", "SwapLimit", "PidsLimit", "CpuCfsQuota")
OPERATIONS = {
    "docker": {"info", "create", "start", "inspect", "kill", "wait", "rm"},
    "git": {"clone", "checkout", "remote"},
    "findmnt": set(), "mkfs.ext4": set(), "mount": set(), "umount": set(),
}


class SandboxError(Exception):
    def __init__(self, message, *, code="sandbox_rejected", operation=None, exit_code=None, missing=(),
                 timeout_seconds=None):
        super().__init__(message)
        self.code = code if code in ERROR_CODES else "sandbox_rejected"
        allowed_operations = {name for name in OPERATIONS} | {
            name + "." + action for name, actions in OPERATIONS.items() for action in actions}
        self.diagnostics = {}
        if operation in allowed_operations:
            self.diagnostics["operation"] = operation
        if type(exit_code) is int and -128 <= exit_code <= 255:
            self.diagnostics["command_exit_code"] = exit_code
        if type(timeout_seconds) is int and 1 <= timeout_seconds <= 900:
            self.diagnostics["timeout_seconds"] = timeout_seconds
        absent = [name for name in (*RESOURCE_KEYS, "CgroupVersion", "seccomp", "apparmor") if name in missing]
        if absent:
            self.diagnostics["missing_capabilities"] = absent


def validate_docker_security(data):
    """Validate Docker's JSON wire names, which differ from its Go-template field names."""
    if not isinstance(data, dict):
        raise SandboxError("Invalid Docker info response", code="invalid_docker_info")
    missing = [key for key in RESOURCE_KEYS if data.get(key) is not True]
    if data.get("CgroupVersion") != "2":
        missing.append("CgroupVersion")
    if missing:
        raise SandboxError("Enforceable cgroup v2 memory, swap, CPU and PID limits are required",
                           code="resource_limits_unavailable", missing=missing)
    options = data.get("SecurityOptions")
    if not isinstance(options, list) or not all(isinstance(value, str) for value in options):
        raise SandboxError("Invalid Docker security info", code="invalid_docker_info")
    missing = [feature for feature in ("seccomp", "apparmor")
               if not any(value.split(",", 1)[0] == "name=" + feature for value in options)]
    if missing:
        raise SandboxError("Docker seccomp and AppArmor are required", code="docker_security_unavailable",
                           missing=missing)


@dataclass(frozen=True)
class Limits:
    disk_mib: int = 256
    memory_mib: int = 256
    pids: int = 64
    wall_seconds: int = 300
    cpus: int = 1
    artifact_bytes: int = 8 * 1024 * 1024
    artifact_files: int = 64
    web_artifacts: bool = False

    def __post_init__(self):
        if type(self.web_artifacts) is not bool:
            raise SandboxError("Invalid web artifact mode")
        for field, low, high in (("disk_mib", 64, 2048), ("memory_mib", 128, 1024),
                                 ("pids", 16, 128), ("wall_seconds", 1, 900), ("cpus", 1, 2),
                                 ("artifact_bytes", 1, 16 * 1024 * 1024), ("artifact_files", 1, 100)):
            value = getattr(self, field)
            if type(value) is not int or not low <= value <= high:
                raise SandboxError(f"Invalid {field} limit")


def command(*args, check=True, timeout=30):
    # The trusted broker never inherits Git hooks, provider credentials or Docker contexts.
    executable = Path(args[0]).name
    action = next((arg for arg in args[1:] if arg in OPERATIONS.get(executable, set())), None)
    operation = executable + "." + action if action else executable
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C",
                                     "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                                     "GIT_TERMINAL_PROMPT": "0", "DOCKER_HOST": "unix:///var/run/docker.sock"})
    except subprocess.TimeoutExpired as exc:
        raise SandboxError("Sandbox command timed out", code="command_timeout", operation=operation,
                           timeout_seconds=timeout) from exc
    if check and result.returncode:
        raise SandboxError(f"{executable} failed (exit {result.returncode})", code="command_failed",
                           operation=operation, exit_code=result.returncode)
    return result


def container_command(name: str, image: str, workspace: Path, limits: Limits, *, relay: Path | None = None):
    """Fixed launch shape. Only the trusted timeout watchdog is root; job code is not."""
    if not ID.fullmatch(name.removeprefix("agentic-coding-")) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise SandboxError("Invalid fixed container identity")
    if not workspace.is_absolute() or any(c in str(workspace) for c in ",\n\r"):
        raise SandboxError("Invalid workspace mount")
    execute = (f"exec /usr/bin/timeout --signal=KILL {limits.wall_seconds}s "
               "/usr/bin/setpriv --reuid=10000 --regid=10000 --clear-groups "
               "--inh-caps=-all --ambient-caps=-all --bounding-set=-all "
               "/usr/local/bin/python -I /work/task.py > /work/stdout.txt 2> /work/stderr.txt")
    args = ["docker", "create", "--name", name, "--label", f"agentic.coding={name}",
            "--network", "none", "--read-only", "--cap-drop", "ALL", "--cap-add", "SETUID",
            "--cap-add", "SETGID", "--cap-add", "SETPCAP", "--cap-add", "KILL", "--security-opt", "no-new-privileges",
            "--memory", f"{limits.memory_mib}m", "--memory-swap", f"{limits.memory_mib}m",
            "--cpus", str(limits.cpus), "--pids-limit", str(limits.pids),
            "--ulimit", "core=0:0", "--ulimit", "nofile=256:256", "--ulimit", "fsize=16777216:16777216",
            "--ipc", "none", "--cgroupns", "private", "--restart", "no", "--log-driver", "none",
            "--user", "0:0", "--workdir", "/work/source", "--env", "HOME=/work/profile",
            "--env", "TMPDIR=/work/scratch", "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", "GIT_CONFIG_NOSYSTEM=1", "--env", "GIT_CONFIG_GLOBAL=/dev/null",
            "--mount", f"type=bind,src={workspace},dst=/work,bind-propagation=rprivate",
            "--mount", f"type=bind,src={workspace / 'scratch'},dst=/tmp,bind-propagation=rprivate",
            ]
    if relay is not None:
        if not relay.is_absolute() or relay.is_symlink() or any(c in str(relay) for c in ",\n\r"):
            raise SandboxError("Invalid fixed relay socket")
        args += ["--mount", f"type=bind,src={relay},dst=/relay.sock,readonly",
                 "--mount", f"type=bind,src={workspace / 'source' / '.git'},dst=/work/source/.git,readonly"]
    return [*args, "--entrypoint", "/bin/sh", image, "-c", execute]


def _artifact_skip_reason(relative: str, path: Path, suffixes: set[str]) -> str | None:
    """Style mistakes are skipped. Links and odd file types are rejected by the caller.

    Names follow the Controller's rule (160 characters, no backslash). skipped.json is generated.
    """
    if (len(relative) > 160 or "/" in relative or "\\" in relative or relative == "skipped.json"
            or any(ord(char) < 32 for char in relative)):
        return "name_not_allowed"
    if path.suffix not in suffixes:
        return "unsupported_type"
    return None


def _secret_found(name: str, data: bytes) -> bool:
    """Patches are scanned on added lines only. Context and deletions are repository content."""
    if not name.endswith((".patch", ".diff")):
        return SECRET.search(data) is not None
    added = b"".join(line for line in data.splitlines(keepends=True)
                     if line.startswith(b"+") and not line.startswith(b"+++"))
    return PATCH_SECRET.search(added) is not None


def read_artifacts(root: Path, limits: Limits):
    """Caller MUST first prove the writer stopped. Never follows links.

    An unsupported name or a file past the quota is skipped and listed in skipped.json.
    A secret match is withheld with metadata only. Links, hardlinks, a changed size,
    and non-UTF-8 or binary content in an allowed text file still fail the export.
    """
    if root.is_symlink() or not root.is_dir():
        raise SandboxError("Invalid artifact directory")
    result, total, entries, skipped = [], 0, 0, []
    suffixes = SUFFIXES | ({".html", ".css", ".js", ".svg"} if limits.web_artifacts else set())
    for directory, dirs, files in os.walk(root, followlinks=False):
        entries += len(dirs) + len(files)
        if entries > 512 or len(Path(directory).relative_to(root).parts) > 12:
            raise SandboxError("Artifact directory limit exceeded")
        dirs.sort()
        kept = []
        for name in dirs:
            path = Path(directory) / name
            if path.is_symlink():
                raise SandboxError("Artifact symlinks are forbidden")
            relative_dir = path.relative_to(root).as_posix()
            # The interactive bundle is stored separately. A link still fails above.
            if relative_dir == "web-preview" or relative_dir.startswith("web-preview/"):
                continue
            kept.append(name)
        dirs[:] = kept
        for name in sorted(files):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if relative == "web-fixtures.json" or relative.startswith("web-preview/"):
                try:
                    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                except OSError as exc:
                    raise SandboxError("Artifact cannot be safely opened") from exc
                try:
                    info = os.fstat(descriptor)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise SandboxError("Artifact must be an unlinked regular file")
                finally:
                    os.close(descriptor)
                continue
            try:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except OSError as exc:
                raise SandboxError("Artifact cannot be safely opened") from exc
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise SandboxError("Artifact must be an unlinked regular file")
                reason = _artifact_skip_reason(relative, path, suffixes)
                if reason is None and len(result) >= limits.artifact_files:
                    reason = "file_limit"
                if reason is None and (info.st_size > ARTIFACT_FILE_BYTES or
                                       info.st_size > limits.artifact_bytes - total):
                    reason = "size_limit"
                if reason:
                    skipped.append({"name": relative, "reason": reason})
                    continue
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    data = stream.read(limits.artifact_bytes - total + 1)
                if len(data) != info.st_size or len(data) > limits.artifact_bytes - total:
                    raise SandboxError("Artifact changed or exceeds quota")
            finally:
                os.close(descriptor)
            try:
                content = data.decode("utf-8")
            except UnicodeError as exc:
                raise SandboxError("Only UTF-8 text artifacts are supported") from exc
            if "\x00" in content:
                raise SandboxError("Binary artifacts are unsupported")
            total += len(data)
            item = {"name": relative, "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data), "untrusted": True,
                    "redaction": "pattern_scan_only; not a guarantee of no secrets"}
            if _secret_found(relative, data):
                item["kind"] = "withheld"
            else:
                item["content"] = content
            result.append(item)
    if skipped:
        listed = {"skipped": [{"name": item["name"] if len(item["name"]) <= 160 else item["name"][:159] + "…",
                               "reason": item["reason"]} for item in skipped[:SKIPPED_LISTED]]}
        if len(skipped) > SKIPPED_LISTED:
            listed["omitted"] = len(skipped) - SKIPPED_LISTED
        payload = json.dumps(listed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        result.append({"name": "skipped.json", "sha256": hashlib.sha256(payload).hexdigest(),
                       "size": len(payload), "content": payload.decode(), "untrusted": True,
                       "redaction": "pattern_scan_only; not a guarantee of no secrets"})
    return result


def web_text_secret(data: bytes) -> bool:
    for match in SECRET.finditer(data):
        head, sep, tail = match.group(0).partition(b"}")
        if sep and _BENIGN_INPUT_TYPES.fullmatch(head) and re.fullmatch(rb";?[A-Za-z]+", tail):
            continue
        return True
    return False


def bundle_relative(name: str) -> str:
    if not isinstance(name, str) or not name or len(name) > WEB_BUNDLE_MAX_PATH:
        raise SandboxError("Unsupported web bundle path")
    if "\\" in name or "\x00" in name or name.startswith("/"):
        raise SandboxError("Unsupported web bundle path")
    path = PurePosixPath(name)
    parts = path.parts
    if (name != path.as_posix() or path.is_absolute() or not parts or len(parts) > WEB_BUNDLE_MAX_DEPTH
            or any(part in {".", ".."} or part.startswith(".") for part in parts)
            or path.suffix not in WEB_BUNDLE_SUFFIXES):
        raise SandboxError("Unsupported web bundle path")
    return name


def bundle_digest(files: dict) -> str:
    listing = [{"path": name, "sha256": hashlib.sha256(files[name]).hexdigest()} for name in sorted(files)]
    payload = json.dumps(listing, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _read_regular(path: Path, limit: int) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise SandboxError("Artifact cannot be safely opened") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SandboxError("Artifact must be an unlinked regular file")
        if info.st_size > limit:
            raise SandboxError("Web bundle file exceeds quota")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) != info.st_size or len(data) > limit:
            raise SandboxError("Web bundle file changed")
        return data
    finally:
        os.close(descriptor)


def _read_bundle_file(bundle: Path, name: str) -> bytes:
    bundle_relative(name)
    if bundle.is_symlink() or not bundle.is_dir():
        raise SandboxError("Web preview bundle is unavailable")
    current = bundle
    parts = PurePosixPath(name).parts
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink() or not current.is_dir():
            raise SandboxError("Web preview bundle is unavailable")
    return _read_regular(current / parts[-1], WEB_BUNDLE_MAX_FILE)


def _read_fixtures(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise SandboxError("Web preview fixtures are missing")
    data = _read_regular(path, FIXTURE_MAX_BYTES)
    if web_text_secret(data):
        raise SandboxError("Potential secret in artifact; private review required")
    try:
        value = json.loads(data)
    except ValueError as exc:
        raise SandboxError("Web preview fixtures are invalid") from exc
    if not isinstance(value, dict) or not value:
        raise SandboxError("Web preview fixtures are invalid")
    if any(not isinstance(key, str) or FIXTURE_KEY.fullmatch(key) is None for key in value):
        raise SandboxError("Web preview fixtures are invalid")
    return data


def read_web_bundle(root: Path):
    """Read web-preview/ and web-fixtures.json. Caller proved the writer stopped."""
    if root.is_symlink() or not root.is_dir():
        raise SandboxError("Invalid artifact directory")
    preview = root / "web-preview"
    if preview.is_symlink() or not preview.is_dir():
        raise SandboxError("Web preview bundle is missing")
    files, total = {}, 0
    for directory, dirnames, names in os.walk(preview, followlinks=False):
        dirnames.sort()
        for name in list(dirnames):
            path = Path(directory) / name
            if path.is_symlink():
                raise SandboxError("Artifact symlinks are forbidden")
        for name in sorted(names):
            path = Path(directory) / name
            relative = bundle_relative(path.relative_to(preview).as_posix())
            data = _read_regular(path, WEB_BUNDLE_MAX_FILE)
            if path.suffix in WEB_TEXT_SUFFIXES and web_text_secret(data):
                raise SandboxError("Potential secret in artifact; private review required")
            if len(files) >= WEB_BUNDLE_MAX_FILES or total + len(data) > WEB_BUNDLE_MAX_TOTAL:
                raise SandboxError("Web bundle quota exceeded")
            total += len(data)
            files[relative] = data
    if "index.html" not in files:
        raise SandboxError("Web preview entry is missing")
    return files, _read_fixtures(root / "web-fixtures.json")


def _write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(descriptor, data)
    finally:
        os.close(descriptor)
    path.chmod(0o600)


def store_web_bundle(destination: Path, files: dict, fixtures: bytes) -> dict:
    """Persist a proved bundle. Modes are 0700/0600. Digest matches preview.validate."""
    if destination.is_symlink():
        raise SandboxError("Web preview bundle is unavailable")
    parent = destination.parent
    if parent.is_symlink():
        raise SandboxError("Web preview bundle is unavailable")
    if not parent.is_dir():
        parent.mkdir(mode=0o700)
        parent.chmod(0o700)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(mode=0o700)
    destination.chmod(0o700)
    bundle = destination / "bundle"
    bundle.mkdir(mode=0o700)
    bundle.chmod(0o700)
    entries = []
    for name in sorted(files):
        bundle_relative(name)
        data = files[name]
        if not isinstance(data, bytes):
            raise SandboxError("Web preview bundle is unavailable")
        target = bundle
        for part in PurePosixPath(name).parts[:-1]:
            target = target / part
            target.mkdir(mode=0o700, exist_ok=True)
            target.chmod(0o700)
        target = target / PurePosixPath(name).parts[-1]
        _write_private(target, data)
        entries.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
    _write_private(destination / "fixtures.json", fixtures)
    digest = bundle_digest(files)
    manifest = {"files": entries, "fixtures": {"sha256": hashlib.sha256(fixtures).hexdigest(), "size": len(fixtures)},
                "artifact_digest": digest}
    atomic(destination / "manifest.json", manifest)
    return {"artifact_digest": digest, "files": len(files), "bytes": sum(len(item) for item in files.values())}


def load_stored_bundle(root: Path):
    """Re-read a stored bundle. Any manifest mismatch raises SandboxError."""
    if root.is_symlink() or not root.is_dir():
        raise SandboxError("Web preview bundle is unavailable")
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise SandboxError("Web preview bundle is unavailable")
    try:
        manifest = json.loads(manifest_path.read_text())
        items = manifest["files"]
        if not isinstance(items, list) or not items:
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SandboxError("Web preview bundle is unavailable") from exc
    files = {}
    for item in items:
        try:
            name = bundle_relative(item["path"])
            data = _read_bundle_file(root / "bundle", name)
        except (SandboxError, KeyError, TypeError) as exc:
            raise SandboxError("Web preview bundle is unavailable") from exc
        if hashlib.sha256(data).hexdigest() != item.get("sha256") or len(data) != item.get("size"):
            raise SandboxError("preview_artifact_digest_mismatch")
        files[name] = data
    digest = bundle_digest(files)
    if digest != manifest.get("artifact_digest"):
        raise SandboxError("preview_artifact_digest_mismatch")
    try:
        fixtures = _read_regular(root / "fixtures.json", FIXTURE_MAX_BYTES)
        meta = manifest["fixtures"]
        if not isinstance(meta, dict):
            raise TypeError()
    except (SandboxError, KeyError, TypeError) as exc:
        raise SandboxError("Web preview bundle is unavailable") from exc
    if hashlib.sha256(fixtures).hexdigest() != meta.get("sha256") or len(fixtures) != meta.get("size"):
        raise SandboxError("preview_artifact_digest_mismatch")
    try:
        parsed = json.loads(fixtures)
    except ValueError as exc:
        raise SandboxError("Web preview bundle is unavailable") from exc
    if not isinstance(parsed, dict):
        raise SandboxError("Web preview bundle is unavailable")
    return files, parsed, digest


class CodingSandbox:
    """Single offline job, fixed local repo allowlist, hard disk and container limits.

    No HTTP route exposes this class yet. A future adapter must authorize the program
    and source scope before calling launch. Existing capabilities remain fail-closed.
    """
    def __init__(self, root: Path, image: str, repos: dict[str, Path], *, limits=None, execute=command, relay=None,
                 inputs=None):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise SandboxError("Worker image must be pinned by local image digest")
        self.root, self.image, self.repos = Path(root).absolute(), image, repos.copy()
        self.limits, self.execute = limits or Limits(), execute
        self.relay = relay
        self.inputs = inputs or {}
        if set(self.inputs) - {"candidate.patch"} or any(not isinstance(v, bytes) or len(v) > 180 * 1024
                                                        for v in self.inputs.values()):
            raise SandboxError("Invalid trusted verifier input")
        if any(c in str(self.root) for c in ",\n\r"):
            raise SandboxError("Invalid state root")
        if self.root.exists() and (self.root.is_symlink() or self.root.resolve() != self.root):
            raise SandboxError("State root cannot contain symlinks")

    def capabilities(self):
        return {"work_contract_major": 1, "host-coder": {"available": False, "modes": [],
                "foundation": "offline Python containment only; not connected to Runner",
                "blocked_reasons": ["cloud provider proxy and private account adapter not implemented",
                                    "fixed-size workspace requires trusted root broker integration"]},
                "home-coder": {"available": False, "modes": [], "blocked_reasons": [
                    "HAOS App network, credentials and hard resource boundaries have not been verified" ]}}

    def preflight(self):
        if os.geteuid() != 0:
            raise SandboxError("A trusted root broker is required for fixed-size disk workspaces",
                               code="root_broker_required")
        try:
            data = json.loads(self.execute("docker", "info", "--format", "{{json .}}").stdout)
        except ValueError as exc:
            raise SandboxError("Invalid Docker info response", code="invalid_docker_info") from exc
        validate_docker_security(data)
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.root.resolve() != self.root or self.root.stat().st_uid != 0 or self.root.stat().st_mode & 0o077:
            raise SandboxError("State root must be private and owned by root", code="state_root_permissions")
        for parent in self.root.parents:
            if parent.stat().st_uid != 0 or parent.stat().st_mode & 0o022:
                raise SandboxError("State ancestors must be owned and writable only by root", code="state_ancestor_permissions")
        fs = self.execute("findmnt", "-n", "-o", "FSTYPE", "-T", str(self.root)).stdout.strip()
        if fs not in {"ext4", "xfs", "btrfs"}:
            raise SandboxError("Workspace backing file must reside on disk, not tmpfs", code="disk_backing_required")
        space = os.statvfs(self.root)
        if space.f_bavail * space.f_frsize < self.limits.disk_mib * 1024 * 1024 + 4 * 1024**3:
            raise SandboxError("At least 4 GiB of host reserve must remain after workspace allocation",
                               code="host_reserve_insufficient")
        return True

    @contextmanager
    def lock(self):
        with (self.root / "writer.lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def record(self, job_id):
        if not isinstance(job_id, str) or not ID.fullmatch(job_id):
            raise SandboxError("Invalid job ID")
        path = self.root / f"{job_id}.json"
        if not path.exists():
            raise SandboxError("Unknown job")
        return json.loads(path.read_text())

    def save(self, job):
        atomic(self.root / f"{job['job_id']}.json", job)
        return {k: v for k, v in job.items() if k not in {"request_digest", "container_id"}}

    def inspect(self, job):
        response = self.execute("docker", "inspect", job["container"], check=False)
        if response.returncode:
            # Docker outage and genuinely absent container cannot safely be conflated.
            raise SandboxError("Container stop cannot be confirmed")
        info = json.loads(response.stdout)[0]
        if info.get("Config", {}).get("Labels", {}).get("agentic.coding") != job["container"]:
            raise SandboxError("Container identity mismatch")
        if job.get("container_id") and info["Id"] != job["container_id"]:
            raise SandboxError("Container was replaced")
        return info

    def _unmount_workspace(self, job_id):
        mount = self.root / job_id / "workspace"
        if os.path.ismount(mount):
            self.execute("umount", str(mount))

    def _labeled_container_ids(self, job):
        """Return label matches, or None when the daemon did not answer. None is never proof."""
        try:
            version = self.execute("docker", "version", "--format", "{{.Server.Version}}", check=False)
            if version.returncode != 0:
                return None
            listed = self.execute("docker", "ps", "-a", "--no-trunc", "--filter",
                                  "label=agentic.coding=" + job["container"], "--format", "{{.ID}}", check=False)
        except (OSError, SandboxError, subprocess.SubprocessError):
            return None
        if listed.returncode != 0:
            return None
        return [line.strip() for line in listed.stdout.splitlines() if line.strip()]

    def _stop_named_container(self, job):
        """Kill a labeled container when it is running. Return inspect info once it is stopped."""
        try:
            info = self.inspect(job)
            if info["State"]["Running"] or info["State"].get("Paused"):
                self.execute("docker", "kill", "--signal", "KILL", job["container"], check=False)
                self.execute("docker", "wait", job["container"], timeout=15)
                info = self.inspect(job)
            if not info["State"]["Running"] and not info["State"].get("Paused"):
                return info
        except (OSError, SandboxError, subprocess.SubprocessError, ValueError, KeyError, IndexError, TypeError):
            return None
        return None

    def _confirm_writer(self, job):
        """Prove a record with no container id is absent, or stop the one container found by label.

        Returns 'absent', 'stopped', or 'unknown'. A silent daemon is always 'unknown'.
        """
        original = job.get("container_id")
        if original and self._stop_named_container(job) is not None:
            return "stopped"
        found = self._labeled_container_ids(job)
        if found is None:
            return "unknown"
        if not found:
            return "absent"
        if len(found) != 1:
            return "unknown"
        job["container_id"] = found[0]
        if self._stop_named_container(job) is not None:
            self.save(job)
            return "stopped"
        if original:
            job["container_id"] = original
        else:
            job.pop("container_id", None)
        return "unknown"

    def _fail_setup(self, job, exc):
        code = exc.code if isinstance(exc, SandboxError) else "coding_setup_failed"
        job.update(state="failed", setup_failure=code)
        self.save(job)
        error = SandboxError("Coding setup failed before a writer started", code="coding_setup_failed")
        error.stopped = True
        raise error from exc

    def prepare(self, job, program):
        directory = self.root / job["job_id"]
        directory.mkdir(mode=0o700)
        disk, mount = directory / "workspace.ext4", directory / "workspace"
        mount.mkdir(mode=0o700)
        fd = os.open(disk, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            # Reserve real blocks. Do not overcommit a sparse file to a busy household disk.
            os.posix_fallocate(fd, 0, self.limits.disk_mib * 1024 * 1024)
        finally:
            os.close(fd)
        self.execute("mkfs.ext4", "-q", "-F", "-m", "0", "-O", "^has_journal",
                     "-E", "nodiscard,lazy_itable_init=0", str(disk))
        self.execute("mount", "-o", "loop,nodev,nosuid", str(disk), str(mount))
        # Kernel filesystem capacity is the hard quota, including every subprocess/file.
        if not os.path.ismount(mount) or os.statvfs(mount).f_blocks * os.statvfs(mount).f_frsize > disk.stat().st_size:
            raise SandboxError("Bounded workspace mount could not be verified", code="workspace_mount_unverified")
        mount.chmod(0o755)
        source = mount / "source"
        self.execute("git", "-c", "core.hooksPath=/dev/null", "clone", "--no-local", "--no-hardlinks",
                     "--no-checkout", "--", str(self.repos[job["repo_id"]].resolve()), str(source), timeout=90)
        self.execute("git", "-C", str(source), "-c", "core.hooksPath=/dev/null", "checkout", "--detach",
                     job["base_commit"], timeout=60)
        self.execute("git", "-C", str(source), "remote", "remove", "origin")
        for name in ("profile", "scratch", "artifacts"):
            (mount / name).mkdir(mode=0o700)
            os.chown(mount / name, UID, UID)
        for path, dirs, files in os.walk(source, followlinks=False):
            os.chown(path, UID, UID, follow_symlinks=False)
            for name in dirs + files:
                os.chown(Path(path) / name, UID, UID, follow_symlinks=False)
        (mount / "task.py").write_text(program)
        (mount / "task.py").chmod(0o444)
        for name, data in self.inputs.items():
            (mount / name).write_bytes(data)
            (mount / name).chmod(0o444)
        if "candidate.patch" in self.inputs:
            tests = mount / "trusted-tests"
            tests.mkdir(mode=0o755)
            for name in ("test_engine.py", "test_api.py", "conftest.py"):
                source_test = source / "tests" / name
                if source_test.is_symlink() or not source_test.is_file() or source_test.stat().st_size > 512 * 1024:
                    raise SandboxError("Trusted baseline verification test missing")
                (tests / name).write_bytes(source_test.read_bytes())
                (tests / name).chmod(0o444)
            # The broker uses umask 077. Make trusted tests traversable by the
            # unprivileged verifier without giving it directory write access.
            tests.chmod(0o555)
            # Baseline tests resolve product helpers relative to their parent.
            # Keep those tests immutable while exercising the candidate helper
            # tree through a fixed alias owned by the trusted workspace root.
            (mount / "scripts").symlink_to("source/scripts", target_is_directory=True)
            (mount / "verification.ini").write_text("[pytest]\nasyncio_mode=auto\n")
            (mount / "verification.ini").chmod(0o444)
        return mount

    def launch(self, job_id, lease_epoch, repo_id, base_commit, program):
        if not isinstance(job_id, str) or not ID.fullmatch(job_id) or type(lease_epoch) is not int or lease_epoch < 1:
            raise SandboxError("Invalid job or lease")
        if not isinstance(repo_id, str) or repo_id not in self.repos or not isinstance(base_commit, str) or not SHA.fullmatch(base_commit):
            raise SandboxError("Repository and fixed commit are required")
        if not isinstance(program, str) or not 0 < len(program.encode()) <= 65536:
            raise SandboxError("Program exceeds limit")
        identity = [job_id, lease_epoch, repo_id, base_commit, program]
        if self.inputs:
            identity.append({k: hashlib.sha256(v).hexdigest() for k, v in self.inputs.items()})
        request_digest = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
        self.preflight()
        with self.lock():
            if (self.root / f"{job_id}.json").exists():
                previous = self.record(job_id)
                if previous["request_digest"] != request_digest:
                    raise SandboxError("Job ID reused with different payload")
                return self.save(previous)
            for path in self.root.glob("*.json"):
                # failed / cancelled / interrupted already proved absence or a stop.
                if json.loads(path.read_text())["state"] in {"preparing", "running", "stop_unconfirmed"}:
                    raise SandboxError("Previous writer has not been fenced")
            if sum(path.is_dir() for path in self.root.iterdir()) >= 4:
                raise SandboxError("Retained workspace budget exhausted; trusted retention cleanup is required")
            job = {"job_id": job_id, "lease_epoch": lease_epoch, "repo_id": repo_id,
                   "base_commit": base_commit, "request_digest": request_digest,
                   "container": f"agentic-coding-{job_id}", "state": "preparing",
                   "observed_from": "host_container", "model_backend": "none", "ha_backend": "none"}
            self.save(job)
            try:
                workspace = self.prepare(job, program)
                created = self.execute(*container_command(job["container"], self.image, workspace, self.limits,
                                                         relay=self.relay))
                job["container_id"] = created.stdout.strip()
                self.save(job)  # Persist exact identity before starting the writer.
                self.execute("docker", "start", job["container"])
                job["state"] = "running"
            except (OSError, SandboxError, subprocess.SubprocessError) as exc:
                proof = self._confirm_writer(job)
                if proof in {"absent", "stopped"}:
                    try:
                        self._unmount_workspace(job["job_id"])
                    except (OSError, SandboxError, subprocess.SubprocessError):
                        proof = "unknown"
                if proof in {"absent", "stopped"}:
                    self._fail_setup(job, exc)
                job["state"] = "stop_unconfirmed"
                self.save(job)
                raise
            return self.save(job)

    def _record_exit(self, job, info):
        state = info.get("State") or {}
        if state.get("Running") or state.get("Paused"):
            job["state"] = "stop_unconfirmed"
            return
        exit_code = state.get("ExitCode")
        if state.get("Status") in {"exited", "dead"}:
            job.update(state="succeeded" if exit_code == 0 else "failed", exit_code=exit_code,
                       oom_killed=state.get("OOMKilled", False))
        else:
            job.update(state="failed", exit_code=exit_code)

    def status(self, job_id):
        with self.lock():
            job = self.record(job_id)
            if job["state"] in {"running", "preparing", "stop_unconfirmed"}:
                if not job.get("container_id"):
                    proof = self._confirm_writer(job)
                    if proof == "absent":
                        job["state"] = "failed"
                    elif proof == "stopped":
                        try:
                            self._record_exit(job, self.inspect(job))
                        except SandboxError:
                            job["state"] = "failed"
                    else:
                        job["state"] = "stop_unconfirmed"
                else:
                    try:
                        info = self.inspect(job)
                        if info["State"].get("Status") in {"exited", "dead"} and not info["State"].get("Paused"):
                            exit_code = info["State"]["ExitCode"]
                            job.update(state="succeeded" if exit_code == 0 else "failed", exit_code=exit_code,
                                       oom_killed=info["State"].get("OOMKilled", False))
                    except SandboxError:
                        job["state"] = "stop_unconfirmed"
            return self.save(job)

    def cancel(self, job_id):
        with self.lock():
            job = self.record(job_id)
            if job["state"] in {"cancelled", "succeeded", "failed", "interrupted", "disposed"}:
                return self.save(job)
            job["state"] = "stop_unconfirmed"
            job.pop("stop_failure", None)
            self.save(job)
            if not job.get("container_id"):
                if self._confirm_writer(job) in {"absent", "stopped"}:
                    job["state"] = "cancelled"
                return self.save(job)
            try:
                info = self.inspect(job)
                if info["State"]["Running"] or info["State"].get("Paused"):
                    self.execute("docker", "kill", "--signal", "KILL", job["container"], check=False)
                    self.execute("docker", "wait", job["container"], timeout=15)
                info = self.inspect(job)
                if not info["State"]["Running"] and not info["State"].get("Paused"):
                    job["state"] = "cancelled"
            except SandboxError as exc:
                job["stop_failure"] = {"error_code": exc.code, **exc.diagnostics}
            except subprocess.SubprocessError:
                pass
            return self.save(job)

    def recover(self):
        """Explicit broker-start reconciliation. Never automatically reruns candidate code."""
        results = []
        for path in self.root.glob("*.json"):
            job = json.loads(path.read_text())
            if job["state"] in {"preparing", "running", "stop_unconfirmed"}:
                result = self.cancel(job["job_id"])
                if result["state"] == "cancelled":
                    with self.lock():
                        job = self.record(job["job_id"])
                        job["state"] = "interrupted"
                        result = self.save(job)
                results.append(result)
        return results

    def artifacts(self, job_id):
        with self.lock():
            job = self.record(job_id)
            # Recheck the real process even after a previous terminal result.
            info = self.inspect(job)
            if info["State"]["Running"] or info["State"].get("Paused"):
                raise SandboxError("Writer must stop before artifact export")
            return read_artifacts(self.root / job_id / "workspace" / "artifacts", self.limits)

    def dispose(self, job_id):
        """Remove only a proven stopped container and unmount its fixed disk. Keep audit/image."""
        result = self.cancel(job_id)
        if result["state"] == "disposed":
            return result
        if result["state"] == "stop_unconfirmed":
            raise SandboxError("Cannot release workspace before writer stop is confirmed")
        with self.lock():
            job = self.record(job_id)
            if not job.get("container_id"):
                proof = self._confirm_writer(job)
                if proof == "stopped":
                    job = self.record(job_id)
                elif proof != "absent":
                    raise SandboxError("Cannot release workspace before writer stop is confirmed")
                else:
                    self._unmount_workspace(job_id)
                    job["state"] = "disposed"
                    return self.save(job)
            info = self.inspect(job)
            if info["State"]["Running"] or info["State"].get("Paused"):
                raise SandboxError("Writer still active")
            self._unmount_workspace(job_id)
            self.execute("docker", "rm", job["container"])
            job["state"] = "disposed"
            return self.save(job)
