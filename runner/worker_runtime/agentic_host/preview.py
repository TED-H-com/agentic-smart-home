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
from pathlib import PurePosixPath

from .coding_volume import DiskVolume
from .fence import fence
from .manager import HostError, atomic
from .resources import ready

WIDTHS = {1440: 960, 768: 1024, 390: 844}
MAX_BYTES = 4 * 1024 * 1024
SUFFIXES = {".html", ".css", ".js", ".json", ".png", ".jpg", ".jpeg", ".svg", ".webp", ".woff2", ".txt"}


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
    def __init__(self, manager, volume=DiskVolume):
        self.manager, self.run, self.volume = manager, manager.run, volume
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

    def submit(self, body):
        if not self.transport_ready:
            raise HostError("preview_listener_unavailable")
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
            self.update(record, state="ready", image_digests=hashes)
        except Exception:
            self.update(record, state="failed", error_code="preview_render_failed")
        finally:
            try:
                if not stopped:
                    self.fenced(name)
                volume.close()
            except Exception:
                self.update(record, state="stop_unconfirmed", error_code="preview_cleanup_unconfirmed")

    def read_png(self, capability, width):
        with self.lock:
            record = next((r for r in self.records.values() if secrets.compare_digest(r["capability"], capability)), None)
            if record is None or record["state"] != "ready" or record["expires_at"] <= time.time() or width not in WIDTHS:
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


def preview_server(previews, address=("127.0.0.2", 8100)):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
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
    return ThreadingHTTPServer(address, Handler)
