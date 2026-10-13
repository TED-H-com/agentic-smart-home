"""Product-owned Ingress transport. No Host cookie, generic proxy or new Agent.

Only the paired HAOS App may assert a Supervisor-authenticated Ingress principal.
The transport bearer alone authorizes heartbeat, never household or case access.
All other requests recheck the currently bound active HA Owner before dispatch.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import stat
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import aiohttp
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field, ValidationError, field_validator

from . import VERSION
from .ha import HAError
from .host import host_call
from .models import Strict

BRIDGE_CONTRACT = 1
HOST_PROOF_TTL, HOST_PROOF_TIMEOUT = 15, 5
HOST, PORT, PEER = "192.168.177.1", 8444, "192.168.177.2"
KEY, MARKER = "ingress_bridge", "agentic.ingress_bridge"
IDENT = r"[a-f0-9]{32}"
ARTIFACT = r"[a-zA-Z0-9_-]{1,128}"
HEADER = Annotated[str, Field(max_length=2048, pattern=r"^[^\r\n]*$")]
GET_PATHS = (
    r"/api/(deployment|session|gateway/messages)",
    rf"/api/hermes/cases/{IDENT}", rf"/api/tasks/{IDENT}",
    rf"/api/worker/jobs/{IDENT}", rf"/api/worker/jobs/{IDENT}/artifacts/{ARTIFACT}",
    rf"/api/worker/previews/{IDENT}", rf"/api/bridge/previews/{IDENT}/(390|768|1440)\.png",
)
POST_PATHS = (
    r"/api/gateway/messages", rf"/api/tasks/{IDENT}/(reply|cancel)",
    rf"/api/proposals/{IDENT}/(approve|test|reconcile)",
    rf"/api/worker/jobs/{IDENT}/(cancel|reconcile|previews)",
    rf"/api/worker/previews/{IDENT}/revoke",
)


class BridgeRequest(Strict):
    protocol: Literal[1]
    pairing_id: Annotated[str, Field(pattern=rf"^{IDENT}$")]
    user_id: Annotated[str, Field(pattern=rf"^({IDENT})?$")]
    method: Literal["GET", "POST"]
    path: Annotated[str, Field(max_length=256)]
    body: dict | None = None
    csrf: HEADER = ""
    idempotency_key: Annotated[str, Field(max_length=200, pattern=r"^[^\r\n]*$")] = ""
    origin: HEADER = ""
    host: HEADER = ""
    fetch_site: HEADER = ""

    @field_validator("protocol", mode="before")
    @classmethod
    def exact_protocol(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("unsupported_bridge_protocol")
        return value


@dataclass(frozen=True)
class BridgeIdentity:
    issuer: object
    pairing_id: str
    owner: str
    method: str
    path: str
    expires: int = 0


class IngressBridge:
    def __init__(self, app, store, ha, gateway, coordinator, secret, *, host_status=None):
        self.app, self.store, self.ha = app, store, ha
        self.gateway, self.coordinator, self.secret = gateway, coordinator, secret
        self.root = Path(store.path).parent / "ingress-bridge"
        self.issuer = object()
        self.on_revoke = lambda: None
        self.owner_valid_until = 0.0
        self.host_status = host_status or host_call
        self.host_valid_until, self.host_scope = 0.0, None
        self.host_lock = asyncio.Lock()
        self.host_stop_task = None
        self.listener_ready = False
        self.server, self.listener_task, self.owner_task = None, None, None
        self.stop_tasks = set()
        self.transport = self.create_transport()

    def record(self):
        return self.store.setting(KEY, {})

    def authority_scope(self):
        return (self.store.setting("owner"), getattr(self.ha, "token", None),
                self.store.setting("hermes_web_epoch"))

    def host_current(self):
        # A DB marker or an earlier Controller process is never a Host proof.
        return self.host_valid_until > time.monotonic() and self.host_scope == self.authority_scope()

    def host_guard(self):
        if not self.host_current():
            raise HTTPException(503, "bridge_host_contract_required")

    def invalidate_host(self):
        self.host_valid_until, self.host_scope, self.owner_valid_until = 0.0, None, 0.0
        if self.server:
            self.server.should_exit = True
        generation = self.record().get("pairing_id")
        if generation and (self.host_stop_task is None or self.host_stop_task.done()):
            self.host_stop_task = asyncio.create_task(self.stop_origin(generation))
            self.stop_tasks.add(self.host_stop_task)
            self.host_stop_task.add_done_callback(self.stop_tasks.discard)

    async def require_host(self, *, force=False):
        if not force and self.host_current():
            return
        try:
            # Include lock contention and any earlier stop fence in this bound.
            async with asyncio.timeout(HOST_PROOF_TIMEOUT):
                async with self.host_lock:
                    if not force and self.host_current():
                        return
                    scope = self.authority_scope()
                    result = await self.host_status()
                    value = result.get("controller_update") if isinstance(result, dict) else None
                    major = value.get("ingress_bridge_contract") if isinstance(value, dict) else None
                    if type(major) is not int or major != BRIDGE_CONTRACT:
                        raise HTTPException(503, "bridge_host_contract_required")
                    if self.host_stop_task:
                        await asyncio.shield(self.host_stop_task)
                    if scope != self.authority_scope():
                        raise HTTPException(403, "bridge_authority_revoked")
                    self.host_scope, self.host_valid_until = scope, time.monotonic() + HOST_PROOF_TTL
        except asyncio.CancelledError:
            self.invalidate_host()
            raise
        except HTTPException as exc:
            self.invalidate_host()
            if exc.detail in {"bridge_host_contract_required", "bridge_authority_revoked"}:
                raise
            raise HTTPException(503, "bridge_host_unavailable") from None
        except Exception:
            self.invalidate_host()
            raise HTTPException(503, "bridge_host_unavailable") from None

    def private_read(self, name, limit=16384):
        fd = os.open(self.root / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077
                    or info.st_size > limit):
                raise ValueError("invalid private bridge file")
            return stream.read(limit + 1)

    def private_write(self, name, value):
        target = self.root / name
        temp = self.root / ("." + uuid.uuid4().hex)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    def repair_layout(self):
        """Only replace this service's private regular files; never follow an odd path."""
        entries = list(self.root.iterdir())
        if len(entries) > 8:
            raise ValueError("unexpected bridge files")
        temporary = []
        for path in entries:
            is_temporary = bool(re.fullmatch(r"\.[a-f0-9]{32}", path.name))
            if path.name not in {"tls.pem", "tls.key", "service-token"} and not is_temporary:
                raise ValueError("unexpected bridge file")
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077
                    or info.st_nlink != 1 or info.st_size > 16384):
                raise ValueError("unsafe bridge file")
            if is_temporary:
                temporary.append(path)
        return temporary

    def unused_pairing_guard(self):
        record = self.record()
        if (self.store.setting("bridge_maintenance") is not None or
                record.get("state") not in {None, "prepared"} or
                (record and record.get("owner") != self.store.setting("owner")) or
                record.get("service_last_seen") is not None or record.get("owner_verified_at") is not None or
                self.store.db.execute("SELECT 1 FROM records WHERE "
                    "json_type(body,'$.bridge_pairing_id') IS NOT NULL OR "
                    "json_type(body,'$.authority.bridge_pairing_id') IS NOT NULL LIMIT 1").fetchone()):
            raise HTTPException(409, "bridge_initial_repair_not_allowed")

    def repair_unused_pairing(self):
        """Explicit Host route only, after its fresh Owner/epoch guard and under manager lock."""
        self.unused_pairing_guard()
        try:
            # A lost repair reply reuses the already valid unused generation.
            self.prepare_pairing()
        except HTTPException as exc:
            if exc.detail != "bridge_pairing_needs_repair":
                raise
            self.prepare_pairing(repair=True)
        self.store.set_setting("bridge_enrollment", {"state": "not_authorized", "code": None})
        return self.status()

    def prepare_pairing(self, repair=False):
        self.host_guard()
        owner, previous = self.store.setting("owner"), self.record()
        if not isinstance(owner, str) or not re.fullmatch(IDENT, owner):
            raise HTTPException(409, "bridge_owner_required")
        if previous and previous.get("owner") != owner and not repair:
            raise HTTPException(409, "bridge_owner_changed")
        if previous.get("state") == "revoked" and not repair:
            raise HTTPException(409, "bridge_pairing_needs_repair")
        try:
            self.root.mkdir(mode=0o700, exist_ok=True)
            directory = self.root.lstat()
            if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.geteuid() or directory.st_mode & 0o077:
                raise ValueError("invalid private bridge directory")
            if previous and not repair:
                certificate = x509.load_pem_x509_certificate(self.private_read("tls.pem"))
                private = serialization.load_pem_private_key(self.private_read("tls.key"), password=None)
                token = self.private_read("service-token", 128).decode()
                fingerprint = certificate.fingerprint(hashes.SHA256()).hex()
                if (fingerprint != previous.get("fingerprint") or
                    private.public_key().public_numbers() != certificate.public_key().public_numbers() or
                    not re.fullmatch(r"[A-Za-z0-9_-]{64}", token) or
                    not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), previous.get("token_hash", ""))):
                    raise ValueError("bridge identity mismatch")
            else:
                temporary = self.repair_layout() if repair else []
                if not repair and any(self.root.iterdir()):
                    raise ValueError("uncommitted bridge identity")
                key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
                name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Agentic Home Ingress")])
                certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                    .public_key(key.public_key()).serial_number(x509.random_serial_number())
                    .not_valid_before(datetime.now(UTC) - timedelta(minutes=5))
                    .not_valid_after(datetime.now(UTC) + timedelta(days=3650))
                    .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(HOST))]), False)
                    .sign(key, hashes.SHA256()))
                token = secrets.token_urlsafe(48)
                fingerprint = certificate.fingerprint(hashes.SHA256()).hex()
                self.private_write("tls.key", key.private_bytes(serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
                self.private_write("tls.pem", certificate.public_bytes(serialization.Encoding.PEM))
                self.private_write("service-token", token.encode())
                for path in temporary:
                    path.unlink()
                previous = {"protocol": 1, "pairing_id": uuid.uuid4().hex, "owner": owner, "state": "prepared",
                    "fingerprint": fingerprint, "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                    "service_last_seen": None, "owner_verified_at": None, "created_at": time.time()}
                self.owner_valid_until = 0
                self.store.set_setting(KEY, previous)
                if self.server:
                    self.server.should_exit = True  # Reload rotated TLS material on the fixed listener.
            return {"protocol": 1, "host": HOST, "port": PORT, "fingerprint": fingerprint,
                    "token": token, "pairing_id": previous["pairing_id"]}
        except (OSError, ValueError, TypeError):
            raise HTTPException(409, "bridge_pairing_needs_repair") from None

    def status(self):
        record = self.record()
        return {"protocol": 1, "state": record.get("state", "unpaired"),
                **{k: record.get(k) for k in ("pairing_id", "fingerprint", "owner", "service_last_seen", "owner_verified_at")},
                "last_seen": record.get("service_last_seen"),
                "listener_ready": self.listener_ready and self.host_current(),
                "host_contract": "verified" if self.host_current() else "unverified"}

    def current(self, pairing_id, owner=None):
        record = self.record()
        return (record.get("state") in {"prepared", "connected"} and record.get("pairing_id") == pairing_id
                and record.get("owner") == self.store.setting("owner")
                and (owner is None or owner == record.get("owner")))

    def origin_valid(self, item):
        generation = item.get("bridge_pairing_id")
        return not generation or (self.host_current() and self.current(generation, item.get("sender"))
                                  and self.owner_valid_until > time.time())

    def identity(self, request):
        marker = request.scope.get(MARKER)
        if not isinstance(marker, BridgeIdentity) or marker.issuer is not self.issuer:
            return None
        if (not self.host_current() or marker.method != request.method or marker.path != request.url.path or
                not self.current(marker.pairing_id, marker.owner) or self.owner_valid_until <= time.time()
                or (marker.method == "POST" and marker.expires <= time.time())):
            raise HTTPException(403, "bridge_authority_revoked")
        return marker

    def csrf(self, owner, pairing_id, expires):
        text = f"bridge:{pairing_id}:{owner}:{expires}"
        return f"{expires}." + hmac.new(bytes.fromhex(self.secret), text.encode(), hashlib.sha256).hexdigest()

    async def authorize(self, generation, owner):
        if not owner or not self.current(generation, owner):
            raise HTTPException(403, "bridge_owner_required")
        await self.require_host()
        token = getattr(self.ha, "token", None)
        try:
            async with asyncio.timeout(5):
                valid = await self.ha.owner(owner)
        except (HAError, OSError, TimeoutError):
            self.owner_valid_until = 0
            raise HTTPException(503, "bridge_owner_unavailable") from None
        if (not valid or not self.current(generation, owner) or token != getattr(self.ha, "token", None)):
            self.owner_valid_until = 0
            raise HTTPException(403, "bridge_owner_required")
        self.host_guard()
        self.owner_valid_until = time.time() + 15
        self.store.set_setting(KEY, {**self.record(), "owner_verified_at": time.time()})

    def service(self, token, pairing_id):
        record = self.record()
        if (not self.current(pairing_id) or not re.fullmatch(r"[A-Za-z0-9_-]{64}", token) or
            not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), record.get("token_hash", ""))):
            raise HTTPException(401, "bridge_unpaired")

    def owner_messages(self, owner):
        return self.gateway.owner_messages(owner)

    def require_case(self, case_id, owner):
        if not case_id or not any(item.get("case_id") == case_id for item in self.owner_messages(owner)):
            raise HTTPException(404, "bridge_resource_not_found")

    def acl(self, path, owner):
        parts = path.split("/")
        if path.startswith("/api/hermes/cases/"):
            self.require_case(parts[4], owner)
        elif path.startswith("/api/tasks/") or path.startswith("/api/proposals/"):
            kind = "task" if parts[2] == "tasks" else "proposal"
            record = self.store.get(kind, parts[3]) or {}
            if kind == "proposal":
                record = self.store.get("task", record.get("task_id", "")) or {}
            self.require_case(record.get("case_id"), owner)
        elif path.startswith("/api/worker/jobs/"):
            self.require_case((self.store.get("work_job", parts[4]) or {}).get("case_id"), owner)
        elif path.startswith(("/api/worker/previews/", "/api/bridge/previews/")):
            preview = self.store.get("worker_preview", parts[4]) or {}
            self.require_case(preview.get("case_id"), owner)

    def validate_request(self, value):
        paths = GET_PATHS if value.method == "GET" else POST_PATHS
        if not any(re.fullmatch(pattern, value.path) for pattern in paths):
            raise HTTPException(403, "bridge_path_not_allowed")
        if value.method == "GET" and value.body not in (None, {}):
            raise HTTPException(422, "bridge_body_invalid")
        expires = 0
        if value.method == "POST":
            try:
                expires = int(value.csrf.split(".")[0])
                origin = urlsplit(value.origin)
                valid_origin = (origin.scheme in {"http", "https"} and bool(origin.netloc) and origin.netloc == value.host
                    and not any((origin.username, origin.password, origin.path, origin.query, origin.fragment)))
            except ValueError:
                valid_origin = False
            if (not time.time() < expires <= time.time() + 3700 or not valid_origin or value.fetch_site not in {"same-origin", "same-site", ""} or
                    not hmac.compare_digest(value.csrf, self.csrf(value.user_id, value.pairing_id, expires))):
                raise HTTPException(403, "bridge_csrf_invalid")
            if value.path == "/api/gateway/messages":
                if (value.body or {}).get("coordinator") != "hermes" or (value.body or {}).get("runtime", "host") != "host" or (value.body or {}).get("provider", "codex") != "codex":
                    raise HTTPException(422, "bridge_coordinator_required")
        return expires

    async def dispatch(self, value, expires):
        """Internal typed ASGI delegation, without cookies or caller-supplied auth headers."""
        body = json.dumps(value.body).encode() if value.body is not None else b""
        headers = [(b"host", (value.host or HOST).encode()), (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()), (b"idempotency-key", value.idempotency_key.encode())]
        marker = BridgeIdentity(self.issuer, value.pairing_id, value.user_id, value.method, value.path, expires)
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
            "method": value.method, "scheme": "https", "path": value.path, "raw_path": value.path.encode(),
            "root_path": "", "query_string": b"", "headers": headers, "client": (PEER, 0),
            "server": (HOST, PORT), MARKER: marker}
        received, finished, chunks, status = False, asyncio.Event(), [], 500
        content_type, size = "application/json", 0
        async def receive():
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": body, "more_body": False}
            await finished.wait()
            return {"type": "http.disconnect"}
        async def send(message):
            nonlocal status, content_type, size
            if message["type"] == "http.response.start":
                status = message["status"]
                content_type = dict(message.get("headers", [])).get(b"content-type", b"application/json").decode()
            elif message["type"] == "http.response.body":
                size += len(message.get("body", b""))
                if size > 1048576:
                    raise HTTPException(502, "bridge_response_too_large")
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    finished.set()
        await self.app(scope, receive, send)
        if content_type.split(";")[0] != "application/json":
            raise HTTPException(502, "bridge_response_invalid")
        self.acl(value.path, value.user_id)
        self.host_guard()
        if not self.current(value.pairing_id, value.user_id):
            raise HTTPException(403, "bridge_authority_revoked")
        payload = json.loads(b"".join(chunks))
        if value.path.startswith("/api/worker/") and "previews" in value.path:
            payload = self.preview_view(payload)
        return JSONResponse(payload, status_code=status)

    @staticmethod
    def preview_view(payload):
        if not isinstance(payload, dict):
            return payload
        result = {key: value for key, value in payload.items()
                  if key not in {"preview_urls", "capability", "container", "interactive_url"}}
        if payload.get("state") == "ready" and re.fullmatch(IDENT, payload.get("id", "")):
            result["preview_urls"] = {str(width): f'api/bridge/previews/{payload["id"]}/{width}.png'
                for width in (390, 768, 1440) if str(width) in payload.get("preview_urls", {})}
        return result

    async def png(self, path, owner):
        identifier, width = path.split("/")[4:6]
        width = width.removesuffix(".png")
        value = await self.app.state.operations.preview_status(identifier)
        url = value.get("preview_urls", {}).get(width, "")
        expected = value.get("image_digests", {}).get(width)
        if (value.get("state") != "ready" or not re.fullmatch(r"http://127\.0\.0\.2:8100/p/[A-Za-z0-9_-]{43}/(390|768|1440)\.png", url)
                or not re.fullmatch(r"[a-f0-9]{64}", expected or "") or not url.endswith(f"/{width}.png")):
            raise HTTPException(404, "bridge_preview_unavailable")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10), trust_env=False) as session:
                async with session.get(url, allow_redirects=False) as response:
                    if response.status != 200 or response.content_type != "image/png":
                        raise HTTPException(502, "bridge_preview_invalid")
                    content = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        content.extend(chunk)
                        if len(content) > 4 * 1024 * 1024:
                            raise HTTPException(502, "bridge_preview_invalid")
        except (aiohttp.ClientError, TimeoutError):
            raise HTTPException(503, "bridge_preview_unavailable") from None
        self.acl(path, owner)
        if not content.startswith(b"\x89PNG\r\n\x1a\n") or hashlib.sha256(content).hexdigest() != expected:
            raise HTTPException(502, "bridge_preview_invalid")
        return Response(bytes(content), media_type="image/png")

    def create_transport(self):
        transport = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        @transport.post("/v1/request")
        async def request(request: Request):
            if not request.client or request.client.host != PEER:
                raise HTTPException(403, "bridge_peer_invalid")
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 131072:
                    raise HTTPException(413, "bridge_request_too_large")
            try:
                value = BridgeRequest.model_validate_json(raw)
            except ValidationError:
                raise HTTPException(422, "bridge_request_invalid") from None
            authorization = request.headers.get("authorization", "")
            self.service(authorization[7:] if authorization.startswith("Bearer ") else "", value.pairing_id)
            heartbeat = value.method == "GET" and value.path == "/api/bridge/heartbeat"
            if heartbeat:
                if value.user_id or value.body not in (None, {}) or any((value.csrf, value.idempotency_key, value.origin, value.host, value.fetch_site)):
                    raise HTTPException(422, "bridge_heartbeat_invalid")
                await self.require_host()
            else:
                expires = self.validate_request(value)
                await self.authorize(value.pairing_id, value.user_id)
                self.acl(value.path, value.user_id)
            self.host_guard()
            self.service(authorization[7:] if authorization.startswith("Bearer ") else "", value.pairing_id)
            self.store.set_setting(KEY, {**self.record(), "state": "connected", "service_last_seen": time.time()})
            if heartbeat:
                result = JSONResponse({"protocol": 1, "version": VERSION, "paired": True, "state": "ready"})
            elif value.path.startswith("/api/bridge/previews/"):
                result = await self.png(value.path, value.user_id)
                self.host_guard()
                if not self.current(value.pairing_id, value.user_id):
                    raise HTTPException(403, "bridge_authority_revoked")
            else:
                result = await self.dispatch(value, expires)
            result.headers["Cache-Control"] = "no-store"
            result.headers["X-Content-Type-Options"] = "nosniff"
            return result
        return transport

    async def stop_origin(self, generation):
        if not generation:
            return
        messages = [item for item in self.store.list("conversation", 10000) if item.get("bridge_pairing_id") == generation]
        identifiers = {item["id"] for item in messages}
        for grant in self.store.list("mcp_grant", 10000):
            if grant.get("message_id") in identifiers:
                self.coordinator.revoke(grant["run_id"])
        pending = []
        for identifier in identifiers:
            task = self.gateway.running.get("chat-" + identifier)
            if task and not task.done():
                if not task.cancelling():
                    task.cancel()
                pending.append(task)
        for job in self.store.list("work_job", 10000):
            if job.get("authority", {}).get("message_id") in identifiers and job.get("state") not in {"succeeded", "failed", "cancelled", "expired"}:
                pending.append(self.coordinator.worker_manager.cancel(job["id"]))
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # Also fence orphaned persisted runs without an in-process Gateway task.
        await self.coordinator.recover(message_ids=identifiers)

    async def revoke(self):
        record = self.record()
        self.owner_valid_until = 0
        self.store.set_setting(KEY, {**record, "state": "revoked", "revoked_at": time.time()})
        self.on_revoke()
        # Authorization disappears synchronously before any stop request can await.
        task = asyncio.create_task(self.stop_origin(record.get("pairing_id")))
        self.stop_tasks.add(task)
        task.add_done_callback(self.stop_tasks.discard)
        await asyncio.shield(task)
        return self.status()

    async def watch_owner(self):
        while True:
            await asyncio.sleep(5)
            record = self.record()
            generation = record.get("pairing_id")
            if not generation:
                continue
            try:
                await self.require_host(force=True)
            except HTTPException:
                continue
            messages = [item for item in self.store.list("conversation", 10000) if item.get("bridge_pairing_id") == generation]
            identifiers = {item["id"] for item in messages}
            active = any("chat-" + identifier in self.gateway.running for identifier in identifiers)
            active |= any(item.get("authority", {}).get("message_id") in identifiers and item.get("state") not in
                {"succeeded", "failed", "cancelled", "expired"} for item in self.store.list("work_job", 10000))
            if not active:
                continue
            try:
                await self.authorize(generation, record.get("owner"))
            except HTTPException:
                self.owner_valid_until = 0
                await self.stop_origin(generation)

    async def listen(self):
        while True:
            sock = None
            try:
                if self.record().get("state") in {"prepared", "connected"}:
                    await self.require_host()
                    self.prepare_pairing()
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind((HOST, PORT))
                    sock.listen(64)
                    sock.setblocking(False)
                    self.server = uvicorn.Server(uvicorn.Config(self.transport, ssl_certfile=str(self.root / "tls.pem"),
                        ssl_keyfile=str(self.root / "tls.key"), proxy_headers=False, access_log=False,
                        log_level="warning", timeout_keep_alive=5, limit_concurrency=16))
                    self.server.capture_signals = contextlib.nullcontext
                    self.listener_ready = True
                    await self.server.serve(sockets=[sock])
            except (OSError, HTTPException):
                pass  # Missing managed-network address is retried without exposing another interface.
            finally:
                self.listener_ready = False
                self.server = None
                if sock:
                    sock.close()
            await asyncio.sleep(3)

    async def start(self):
        self.listener_task = asyncio.create_task(self.listen())
        self.owner_task = asyncio.create_task(self.watch_owner())

    async def close(self):
        if self.server:
            self.server.should_exit = True
        for task in (self.listener_task, self.owner_task):
            if task:
                task.cancel()
        await asyncio.gather(*(task for task in (self.listener_task, self.owner_task) if task), return_exceptions=True)
        await asyncio.gather(*self.stop_tasks, return_exceptions=True)
