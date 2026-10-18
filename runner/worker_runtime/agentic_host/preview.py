"""Bounded, offline rendering; the public surface serves PNG bytes only."""
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from .coding_sandbox import SandboxError, load_stored_bundle
from .coding_volume import DiskVolume
from .fence import fence
from .manager import HostError, atomic
from .resources import ready

WIDTHS = {1440: 960, 768: 1024, 390: 844}
MAX_BYTES = 4 * 1024 * 1024
SUFFIXES = {".html", ".css", ".js", ".json", ".png", ".jpg", ".jpeg", ".svg", ".webp", ".woff2", ".txt"}
INTERACTIVE_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
    "font-src 'self'; connect-src 'self'; "
    "frame-ancestors http://localhost:8099 http://127.0.0.1:8099; "
    "base-uri 'none'; form-action 'none'; object-src 'none'; worker-src 'none'; manifest-src 'none'")
INTERACTIVE_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
    ".svg": "image/svg+xml", ".json": "application/json", ".txt": "text/plain; charset=utf-8",
    ".png": "image/png", ".webp": "image/webp", ".woff2": "font/woff2", ".ico": "image/x-icon"}
FIXTURE_ROUTE = re.compile(r"[a-z0-9/_-]{1,120}\Z")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def validate(body):
    if not isinstance(body, dict) or set(body) != {"id", "artifact_id", "artifact_digest", "files"}:
        raise HostError("preview_request_invalid")
    if any(not isinstance(body[k], str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", body[k]) for k in ("id", "artifact_id")):
        raise HostError("preview_identity_invalid")
    if not isinstance(body["files"], list) or not 1 <= len(body["files"]) <= 64:
        raise HostError("preview_file_limit")
    files, total = {}, 0
    for item in body["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "content_base64"}:
            raise HostError("preview_file_invalid")
        name = item["path"]
        if not isinstance(name, str) or len(name) > 200 or not re.fullmatch(r"[A-Za-z0-9_./-]+", name):
            raise HostError("preview_path_invalid")
        parts = PurePosixPath(name).parts
        if name.startswith("/") or name != str(PurePosixPath(name)) or len(parts) > 4 or any(p.startswith(".") for p in parts) or PurePosixPath(name).suffix not in SUFFIXES or name in files:
            raise HostError("preview_path_invalid")
        try:
            if not isinstance(item["content_base64"], str) or len(item["content_base64"]) > MAX_BYTES * 2:
                raise ValueError()
            data = base64.b64decode(item["content_base64"], validate=True)
        except ValueError:
            raise HostError("preview_encoding_invalid") from None
        total += len(data)
        if total > MAX_BYTES:
            raise HostError("preview_byte_limit")
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise HostError("preview_file_digest_mismatch")
        if any(name.startswith(existing + "/") or existing.startswith(name + "/") for existing in files):
            raise HostError("preview_path_conflict")
        files[name] = data
    if "index.html" not in files:
        raise HostError("preview_entry_required")
    digest = hashlib.sha256(canonical([{"path": name, "sha256": hashlib.sha256(files[name]).hexdigest()}
                                       for name in sorted(files)])).hexdigest()
    if digest != body["artifact_digest"]:
        raise HostError("preview_artifact_digest_mismatch")
    return files


def png(data, width):
    if not 33 <= len(data) <= MAX_BYTES or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR" or struct.unpack(">II", data[16:24]) != (width, WIDTHS[width]):
        raise HostError("preview_output_invalid")
    return hashlib.sha256(data).hexdigest()


class Previews:
    def __init__(self, manager, volume=DiskVolume, bundles=None):
        self.manager, self.run, self.volume = manager, manager.run, volume
        self.bundles = None if bundles is None else Path(bundles)
        self.public_host, self.public_port = "127.0.0.2", 8100
        self.root = manager.root / "previews"
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.lock = threading.RLock()
        self.transport_ready = True
        self.path = self.root / "records.json"
        self.records = json.loads(self.path.read_text()) if self.path.exists() else {}
        for record in self.records.values():
            if record["state"] in {"queued", "running"}:
                record.update(state="stop_unconfirmed", error_code="preview_interrupted")
        atomic(self.path, self.records)

    def image(self):
        values = json.loads((self.manager.product / "images.lock.json").read_text())
        found = [v for k, v in values.items() if re.fullmatch(r"agentic-home-preview:[0-9.]+", k)]
        if len(found) != 1 or not re.fullmatch(r"sha256:[a-f0-9]{64}", found[0]):
            raise HostError("preview_image_unavailable")
        return found[0]

    def view(self, record):
        if record.get("mode") == "interactive":
            result = {k: v for k, v in record.items() if k not in {"capability", "container"}}
            if result.get("state") == "ready":
                if record["expires_at"] <= time.time():
                    result["state"] = "expired"
                else:
                    result["interactive_url"] = (
                        f"http://{self.public_host}:{self.public_port}/i/{record['capability']}/")
            return result
        result = {k: v for k, v in record.items() if k not in {"capability", "container"}}
        if result["state"] == "ready":
            if record["expires_at"] <= time.time():
                result["state"] = "expired"
            else:
                result["preview_urls"] = {str(w): f"http://127.0.0.2:8100/p/{record['capability']}/{w}.png" for w in WIDTHS}
        return result

    def update(self, record, **values):
        with self.lock:
            record.update(values)
            atomic(self.path, self.records)

    def open_bundle(self, job_id):
        if self.bundles is None or not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise HostError("preview_bundle_unavailable")
        try:
            return load_stored_bundle(self.bundles / job_id)
        except SandboxError as exc:
            if str(exc) == "preview_artifact_digest_mismatch":
                raise HostError("preview_artifact_digest_mismatch") from None
            raise HostError("preview_bundle_unavailable") from None

    def interactive_record(self, capability):
        with self.lock:
            for record in self.records.values():
                stored = record.get("capability")
                if (record.get("mode") != "interactive" or not isinstance(stored, str)
                        or len(stored) != len(capability) or not secrets.compare_digest(stored, capability)):
                    continue
                if record.get("state") != "ready" or record.get("expires_at", 0) <= time.time():
                    return None
                return {"job_id": record.get("job_id"), "artifact_digest": record.get("artifact_digest")}
        return None

    def submit_interactive(self, body):
        if not isinstance(body, dict) or set(body) != {"id", "mode", "job_id"} or body.get("mode") != "interactive":
            raise HostError("preview_request_invalid")
        if not isinstance(body["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", body["id"]):
            raise HostError("preview_identity_invalid")
        if not isinstance(body["job_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", body["job_id"]):
            raise HostError("preview_identity_invalid")
        _files, _fixtures, digest = self.open_bundle(body["job_id"])
        with self.lock:
            previous = self.records.get(body["id"])
            if previous:
                if (previous.get("mode") != "interactive" or previous.get("job_id") != body["job_id"]
                        or previous.get("artifact_digest") != digest):
                    raise HostError("preview_idempotency_conflict")
                return self.view(previous)
            if len([item for item in self.records.values() if item.get("expires_at", 0) > time.time()]) >= 8:
                raise HostError("preview_retention_limit")
            record = {"id": body["id"], "mode": "interactive", "job_id": body["job_id"], "artifact_digest": digest,
                      "state": "ready", "created_at": time.time(), "expires_at": time.time() + 1800,
                      "capability": secrets.token_urlsafe(32)}
            self.records[body["id"]] = record
            atomic(self.path, self.records)
        return self.view(record)

    def submit(self, body):
        if not self.transport_ready:
            raise HostError("preview_listener_unavailable")
        if isinstance(body, dict) and body.get("mode") == "interactive":
            return self.submit_interactive(body)
        files = validate(body)
        image = self.image()
        ready(self.run, 1536)
        with self.lock:
            previous = self.records.get(body["id"])
            if previous:
                if any(previous[k] != body[k] for k in ("artifact_id", "artifact_digest")):
                    raise HostError("preview_idempotency_conflict")
                return self.view(previous)
            if any(r["state"] in {"queued", "running", "stop_unconfirmed"} for r in self.records.values()):
                raise HostError("preview_busy_or_unfenced")
            if len([r for r in self.records.values() if r.get("expires_at", 0) > time.time()]) >= 8:
                raise HostError("preview_retention_limit")
            record = {"id": body["id"], "artifact_id": body["artifact_id"], "artifact_digest": body["artifact_digest"],
                      "state": "queued", "created_at": time.time(), "expires_at": time.time() + 1800,
                      "capability": secrets.token_urlsafe(32), "image_id": image, "observation": "offline_preview_renderer"}
            self.records[body["id"]] = record
            atomic(self.path, self.records)
        threading.Thread(target=self.render, args=(record, files), daemon=True).start()
        return self.view(record)

    def fenced(self, name):
        fence(self.run, name, "agentic.preview")

    def render(self, record, files):
        volume = self.volume(self.root / "volumes", hashlib.sha256(record["id"].encode()).hexdigest()[:32], 128)
        name = "agentic-preview-" + secrets.token_hex(16)
        stopped = False
        result = {"state": "failed", "error_code": "preview_render_failed"}
        try:
            self.update(record, state="running", container=name)
            mount = volume.create()
            source, scratch = mount / "input", mount / "work"
            source.mkdir(mode=0o755)
            source.chmod(0o755)  # Broker umask is 0077; renderer must traverse read-only assets.
            scratch.mkdir(mode=0o700)
            os.chown(scratch, 10004, 10004)
            for path, data in files.items():
                target = source / path
                directory = source
                for part in PurePosixPath(path).parts[:-1]:
                    directory = directory / part
                    directory.mkdir(exist_ok=True, mode=0o755)
                    directory.chmod(0o755)
                target.write_bytes(data)
                target.chmod(0o444)
            self.run("docker", "run", "--name", name, "--label", "agentic.preview=" + name, "--network", "none", "--read-only", "--cap-drop", "ALL",
                     "--security-opt", "no-new-privileges:true", "--user", "10004:10004", "--memory", "768m",
                     "--memory-swap", "768m", "--pids-limit", "128", "--cpus", "1", "--ipc", "none", "--log-driver", "none",
                     "--mount", f"type=bind,src={source},dst=/input,readonly",
                     "--mount", f"type=bind,src={scratch},dst=/work",
                     "--mount", f"type=bind,src={scratch},dst=/tmp", record["image_id"], timeout=60)
            self.fenced(name)
            stopped = True
            output = self.root / record["id"]
            output.mkdir(mode=0o700, exist_ok=False)
            hashes = {}
            for width in WIDTHS:
                path = scratch / f"{width}.png"
                if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
                    raise HostError("preview_output_invalid")
                data = path.read_bytes()
                hashes[str(width)] = png(data, width)
                (output / path.name).write_bytes(data)
            result = {"state": "ready", "image_digests": hashes}
        except Exception:
            result = {"state": "failed", "error_code": "preview_render_failed"}
        finally:
            try:
                if not stopped:
                    self.fenced(name)
                volume.close()
            except Exception:
                result = {"state": "stop_unconfirmed", "error_code": "preview_cleanup_unconfirmed"}
            # Keep the renderer slot occupied until both the process and its
            # volume are gone. A terminal result permits a new submission.
            self.update(record, **result)

    def read_png(self, capability, width):
        with self.lock:
            record = next((r for r in self.records.values() if secrets.compare_digest(r["capability"], capability)), None)
            if (record is None or record.get("mode") == "interactive" or record["state"] != "ready"
                    or record["expires_at"] <= time.time() or width not in WIDTHS):
                return None
            data = (self.root / record["id"] / f"{width}.png").read_bytes()
            if png(data, width) != record["image_digests"][str(width)]:
                return None
            return data

    def expire(self):
        with self.lock:
            for record in self.records.values():
                if record["state"] in {"ready", "failed"} and record["expires_at"] <= time.time():
                    shutil.rmtree(self.root / record["id"], ignore_errors=True)
                    record.update(state="expired")
            atomic(self.path, self.records)

    def revoke(self, identifier):
        with self.lock:
            record = self.records.get(identifier)
            if record is None:
                raise HostError("preview_not_found")
            if record["state"] in {"queued", "running"}:
                raise HostError("preview_render_in_progress")
            if record["state"] == "stop_unconfirmed":
                if record.get("container"):
                    self.fenced(record["container"])
                self.volume(self.root / "volumes", hashlib.sha256(identifier.encode()).hexdigest()[:32], 128).close()
            shutil.rmtree(self.root / identifier, ignore_errors=True)
            self.update(record, state="expired", expires_at=time.time())
            return self.view(record)

    def handle(self, method, path, body=None):
        if method == "POST" and path == "/previews":
            return 202, self.submit(body)
        if method == "POST" and path.startswith("/previews/") and path.endswith("/revoke") and body == {}:
            return 200, self.revoke(path.removeprefix("/previews/").removesuffix("/revoke"))
        if method == "GET" and path.startswith("/previews/"):
            with self.lock:
                record = self.records.get(path.removeprefix("/previews/"))
                if record:
                    return 200, self.view(record)
            return 404, {"detail": "preview_not_found"}
        raise HostError("preview_request_invalid")


def _interactive_target(path):
    match = re.fullmatch(r"/i/([A-Za-z0-9_-]{43})(?:/(.*))?", path.split("?", 1)[0])
    if not match:
        return None
    capability, tail = match.group(1), match.group(2)
    if not tail:
        return capability, "file", "index.html"
    try:
        decoded = unquote(tail, errors="strict")
    except UnicodeError:
        return capability, "reject", ""
    if "\x00" in decoded or "\\" in decoded or any(part in {".", ".."} for part in decoded.split("/")):
        return capability, "reject", ""
    if decoded.startswith("api/"):
        return capability, "api", decoded[4:]
    return capability, "file", decoded


def preview_server(previews, address=("127.0.0.2", 8100)):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _interactive(self, status, body, content_type):
            if isinstance(body, str):
                body = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", INTERACTIVE_CSP)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _deny_json(self, status, detail):
            self._interactive(status, json.dumps({"detail": detail}, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")

        def serve_interactive(self):
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                length = 0
            if length > 0:
                self.rfile.read(min(length, 65536))
            if "Service-Worker" in self.headers:
                self._interactive(404, b"", "text/plain")
                return
            if self.headers.get("Host") != f"{address[0]}:{self.server.server_port}":
                self._interactive(404, b"", "text/plain")
                return
            target = _interactive_target(self.path)
            if target is None:
                self._interactive(404, b"", "text/plain")
                return
            capability, kind, name = target
            record = previews.interactive_record(capability)
            if record is None or kind == "reject":
                self._interactive(404, b"", "text/plain")
                return
            try:
                files, fixtures, digest = previews.open_bundle(record["job_id"])
            except HostError:
                self._interactive(404, b"", "text/plain")
                return
            if digest != record["artifact_digest"]:
                self._interactive(404, b"", "text/plain")
                return
            if self.command != "GET":
                self._deny_json(403, "預覽不會執行操作")
                return
            if kind == "api":
                if FIXTURE_ROUTE.fullmatch(name) is None or name not in fixtures:
                    self._deny_json(404, "預覽沒有這項資料")
                    return
                self._interactive(200, json.dumps(fixtures[name], ensure_ascii=False).encode(),
                                  "application/json; charset=utf-8")
                return
            data = files.get(name)
            content_type = INTERACTIVE_TYPES.get(PurePosixPath(name).suffix)
            if data is None or content_type is None:
                self._interactive(404, b"", "text/plain")
                return
            self._interactive(200, data, content_type)

        def do_GET(self):
            if self.path.split("?", 1)[0].startswith("/i/"):
                self.serve_interactive()
                return
            match = re.fullmatch(r"/p/([A-Za-z0-9_-]{43})/(1440|768|390)\.png", self.path)
            data = None
            if self.headers.get("Host") == f"{address[0]}:{self.server.server_port}" and match:
                data = previews.read_png(match[1], int(match[2]))
            self.send_response(200 if data else 404)
            self.send_header("Content-Type", "image/png" if data else "text/plain")
            self.send_header("Content-Length", str(len(data or b"")))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; sandbox; frame-ancestors 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if data:
                self.wfile.write(data)

        def do_POST(self):
            if self.path.split("?", 1)[0].startswith("/i/"):
                self.serve_interactive()
                return
            self.send_error(501)

        # The demo API refuses every write the same way.
        do_PUT = do_PATCH = do_DELETE = do_POST

    server = ThreadingHTTPServer(address, Handler)
    previews.public_host = address[0]
    previews.public_port = server.server_port
    return server
