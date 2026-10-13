"""Fixed provider/source egress across a Unix socket, never a general network proxy.

Responses are forwarded as they arrive. ChatGPT subscription access tokens are
refreshed only against the fixed OpenAI token endpoint; the refresh token and
id token never leave this process.
"""
from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import os
import secrets
import socket
import socketserver
import ssl
import stat
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .coding_sandbox import SandboxError

SOURCE_HOSTS = frozenset({"github.com", "raw.githubusercontent.com", "api.github.com",
    "docs.python.org", "developers.home-assistant.io", "www.home-assistant.io",
    "hermes-agent.nousresearch.com", "developers.openai.com", "platform.openai.com"})
MAX_REQUEST = 2 * 1024**2
MAX_RESPONSE = 8 * 1024**2
MAX_CALLS = 200
FETCH_LIMIT = 512 * 1024
MAX_CREDENTIAL = 32768
# Idle gap between response bytes. Codex's own stream idle limit is 300s.
# The job deadline still bounds the total time.
IDLE_TIMEOUT = 300
ACCESS_REFRESH_WINDOW = 5 * 60
TOKEN_REFRESH_INTERVAL = 8 * 24 * 60 * 60
CHATGPT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_HOST = "auth.openai.com"
TOKEN_PATH = "/oauth/token"
# Pinned Codex treats these refresh failures as permanent: sign in again.
PERMANENT_REFRESH_CODES = frozenset({"refresh_token_expired", "refresh_token_reused", "refresh_token_invalidated"})
_REJECTED = b'{"error":"coding_egress_rejected"}'
# Pinned Codex 0.157.1 Responses HTTP headers. Match names case-insensitively,
# drop invalid values, and never fail the request for an unknown header.
# x-oai-attestation is device identity and is denied below. The relay filters
# again; a sandbox proxy is not trusted. The last request flag is ChatGPT-only.
REQUEST_HEADER_RULES = (
    ("x-openai-internal-codex-responses-lite", 4, "true", False),
    ("originator", 128, "originator", False),
    ("x-codex-turn-state", 1024, "ascii", False),
    ("x-codex-beta-features", 1024, "beta", False),
    ("session-id", 120, "session", False),
    ("thread-id", 36, "uuid", False),
    ("x-client-request-id", 36, "uuid", False),
    ("x-codex-window-id", 57, "window", False),
    ("x-codex-parent-thread-id", 36, "uuid", False),
    ("x-codex-turn-metadata", 8192, "metadata", False),
    ("x-openai-subagent", 64, "subagent", False),
    ("x-openai-memgen-request", 4, "true", False),
    ("x-codex-routing-hint", 128, "routing", True),
)
RESPONSE_HEADER_RULES = (
    ("x-codex-turn-state", 1024, "ascii"),
    ("openai-model", 128, "slug"),
    ("x-models-etag", 256, "ascii"),
    ("x-reasoning-included", 32, "ascii"),
    ("x-request-id", 128, "ascii"),
    ("x-oai-request-id", 128, "ascii"),
    ("cf-ray", 128, "ascii"),
    ("x-codex-safety-buffering-enabled", 16, "ascii"),
    ("x-codex-safety-buffering-faster-model", 128, "slug"),
    ("x-codex-promo-message", 512, "ascii"),
    ("x-codex-rate-limit-reached-type", 64, "reached"),
    ("x-codex-active-limit", 64, "limit-id"),
    ("x-codex-credits-has-credits", 8, "bool"),
    ("x-codex-credits-unlimited", 8, "bool"),
    ("x-codex-credits-balance", 64, "ascii"),
)
RATE_FIELDS = (
    ("primary-used-percent", "decimal"),
    ("primary-window-minutes", "int"),
    ("primary-reset-at", "int"),
    ("secondary-used-percent", "decimal"),
    ("secondary-window-minutes", "int"),
    ("secondary-reset-at", "int"),
    ("limit-name", "ascii"),
)
REACHED_TYPES = (
    "rate_limit_reached",
    "workspace_owner_credits_depleted",
    "workspace_member_credits_depleted",
    "workspace_owner_usage_limit_reached",
    "workspace_member_usage_limit_reached",
)
DENIED_HEADERS = (
    "accept", "authorization", "chatgpt-account-id", "connection", "content-length", "content-type",
    "cookie", "host", "openai-beta", "proxy-authorization", "set-cookie", "transfer-encoding", "user-agent",
    "x-error-json", "x-oai-attestation", "x-openai-authorization-error",
)
MAX_FORWARDED_REQUEST_HEADERS = 16
MAX_FORWARDED_RESPONSE_HEADERS = 40
_REQUEST_BY_NAME = {name: (limit, kind, chatgpt_only)
                    for name, limit, kind, chatgpt_only in REQUEST_HEADER_RULES}
_RESPONSE_BY_NAME = {name: (limit, kind) for name, limit, kind in RESPONSE_HEADER_RULES}
_DENIED = frozenset(DENIED_HEADERS)
_REACHED = frozenset(REACHED_TYPES)


def _ascii(value, limit):
    return isinstance(value, str) and 0 < len(value) <= limit and all(32 <= ord(ch) <= 126 for ch in value)


def _hex_uuid(value):
    if not isinstance(value, str) or len(value) != 36:
        return False
    parts = value.split("-")
    if [len(part) for part in parts] != [8, 4, 4, 4, 12]:
        return False
    hex_digits = "0123456789abcdefABCDEF"
    return all(ch in hex_digits for part in parts for ch in part)


def _slug(value, limit):
    return _ascii(value, limit) and all(ch.isalnum() or ch in "._:-" for ch in value)


def _model_slug(value, limit):
    if not isinstance(value, str) or not 0 < len(value) <= limit:
        return False
    return all(ch.isalnum() or ch in "._-" for ch in value)


def _kind_ok(value, kind, limit):
    if kind == "ascii":
        return _ascii(value, limit)
    if kind == "true":
        return value == "true"
    if kind == "uuid":
        return _hex_uuid(value)
    if kind == "originator":
        return (_ascii(value, limit) and value[0].isalnum()
                and all(ch.isalnum() or ch in " ._/-" for ch in value))
    if kind == "beta":
        if not _ascii(value, limit):
            return False
        parts = value.split(",")
        return all(part and all(ch.islower() or ch.isdigit() or ch == "_" for ch in part) for part in parts)
    if kind == "window":
        if not isinstance(value, str) or value.count(":") != 1:
            return False
        thread, number = value.split(":", 1)
        return _hex_uuid(thread) and number.isdigit() and 1 <= len(number) <= 20
    if kind == "session":
        if _hex_uuid(value):
            return True
        if not isinstance(value, str) or value.count(":") != 1 or not _ascii(value, limit):
            return False
        label, thread = value.split(":", 1)
        label_ok = 1 <= len(label) <= 64 and all(ch.islower() or ch.isdigit() or ch == "_" for ch in label)
        return label_ok and _hex_uuid(thread)
    if kind == "subagent":
        return _ascii(value, limit) and all(ch.isalnum() or ch in "_-" for ch in value)
    if kind == "routing":
        if not _ascii(value, limit) or not value.startswith("model="):
            return False
        rest = value[6:]
        if ";tier=" in rest:
            model, _, tier = rest.partition(";tier=")
            return _model_slug(model, 64) and _model_slug(tier, 32)
        return _model_slug(rest, 64) and ";" not in rest
    if kind == "metadata":
        if not _ascii(value, limit) or not value.startswith("{"):
            return False
        try:
            parsed = json.loads(value)
        except ValueError:
            return False
        return isinstance(parsed, dict)
    if kind == "slug":
        return _slug(value, limit)
    if kind == "decimal":
        if not isinstance(value, str) or not value:
            return False
        whole, dot, frac = value.partition(".")
        if not whole.isdigit() or not 1 <= len(whole) <= 6:
            return False
        if not dot:
            return True
        return frac.isdigit() and 1 <= len(frac) <= 4
    if kind == "int":
        return isinstance(value, str) and value.isdigit() and 1 <= len(value) <= min(limit, 19)
    if kind == "bool":
        return isinstance(value, str) and value.lower() in {"true", "false", "0", "1"}
    if kind == "reached":
        return value in _REACHED
    if kind == "limit-id":
        return _ascii(value, limit) and all(ch.isalnum() or ch in "_-" for ch in value)
    return False


def _denied(name):
    if name in _DENIED or name.startswith("proxy-"):
        return True
    if name.endswith("-key") or name.endswith("_key"):
        return True
    return "token" in name or "bearer" in name or "cookie" in name or "authorization" in name


def _pairs(headers):
    if headers is None:
        return ()
    items = getattr(headers, "items", None)
    if callable(items):
        return items()
    if isinstance(headers, (str, bytes, bytearray)):
        return ()
    try:
        return [(name, value) for name, value in headers]
    except (TypeError, ValueError):
        return ()


def _header_name(raw):
    if not isinstance(raw, str):
        return None
    name = raw.strip().lower()
    if not name or not _ascii(name, 128):
        return None
    return name


def _limit_token(token):
    if not token or len(token) > 33:
        return False
    if not (token[0].islower() or token[0].isdigit()):
        return False
    return all(ch.islower() or ch.isdigit() or ch == "-" for ch in token)


def _rate_kind(name):
    if not name.startswith("x-"):
        return None
    for suffix, kind in RATE_FIELDS:
        marker = "-" + suffix
        if name.endswith(marker) and _limit_token(name[2:-len(marker)]):
            return kind
    return None


def select_request_headers(headers, *, chatgpt):
    """Return the canonical request headers safe to forward. The first value wins."""
    chosen, seen = {}, set()
    for raw_name, raw_value in _pairs(headers):
        name = _header_name(raw_name)
        if name is None or name in seen:
            continue
        seen.add(name)
        rule = _REQUEST_BY_NAME.get(name)
        if rule is None or _denied(name) or not isinstance(raw_value, str):
            continue
        limit, kind, chatgpt_only = rule
        if chatgpt_only and not chatgpt:
            continue
        if not _kind_ok(raw_value, kind, limit):
            continue
        chosen[name] = raw_value
        if len(chosen) >= MAX_FORWARDED_REQUEST_HEADERS:
            break
    return chosen


def select_response_headers(headers):
    """Return the canonical response headers pinned Codex reads. Anything else is dropped."""
    chosen, seen = {}, set()
    for raw_name, raw_value in _pairs(headers):
        name = _header_name(raw_name)
        if name is None or name in seen:
            continue
        seen.add(name)
        if _denied(name) or not isinstance(raw_value, str):
            continue
        rule = _RESPONSE_BY_NAME.get(name)
        if rule is not None:
            limit, kind = rule
        else:
            kind = _rate_kind(name)
            if kind is None:
                continue
            limit = 64 if kind == "ascii" else 19 if kind == "int" else 16
        if not _kind_ok(raw_value, kind, limit):
            continue
        chosen[name] = raw_value
        if len(chosen) >= MAX_FORWARDED_RESPONSE_HEADERS:
            break
    return chosen


def credential_metadata_ok(info):
    return (stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid in {0, 1010}
            and not info.st_mode & 0o077 and 0 <= info.st_size <= MAX_CREDENTIAL)


def jwt_payload(token):
    """Return JWT claims, or None when the token is not a JWT. Mirrors parse_jwt_expiration."""
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        return None
    try:
        payload = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        claims = json.loads(payload)
    except (ValueError, json.JSONDecodeError):
        return None
    return claims if isinstance(claims, dict) else None


def jwt_expiration(token):
    claims = jwt_payload(token)
    if claims is None:
        return None
    exp = claims.get("exp")
    if type(exp) is not int:
        return None
    return exp


def refresh_instant(value):
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def refresh_due(value, now):
    """Mirror pinned Codex: a parsed JWT exp decides alone (5-minute window).
    Without one, refresh when last_refresh is older than 8 days."""
    tokens = value.get("tokens") if isinstance(value, dict) else None
    access = tokens.get("access_token") if isinstance(tokens, dict) else None
    if isinstance(access, str):
        exp = jwt_expiration(access)
        if exp is not None:
            return exp <= now + ACCESS_REFRESH_WINDOW
    refreshed = refresh_instant(value.get("last_refresh")) if isinstance(value, dict) else None
    return refreshed is not None and refreshed < now - TOKEN_REFRESH_INTERVAL


def refresh_error_code(raw):
    """The code pinned Codex reads: string `error`, then `error.code`, then top-level `code`."""
    try:
        detail = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(detail, dict):
        return None
    error = detail.get("error")
    if isinstance(error, str) and error:
        return error
    if isinstance(error, dict) and isinstance(error.get("code"), str) and error["code"]:
        return error["code"]
    code = detail.get("code")
    return code if isinstance(code, str) and code else None


def refresh_rejected(status, raw):
    """True for a permanent refresh failure. Anything else is transient and may be retried."""
    code = (refresh_error_code(raw) or "").lower()
    return status == 401 or code in PERMANENT_REFRESH_CODES or (status == 400 and code == "invalid_grant")


class RefreshRejected(SandboxError):
    def __init__(self):
        super().__init__("Coding token refresh rejected")


def read_auth(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not credential_metadata_ok(info):
            raise SandboxError("Unsafe coding credential file")
        raw = os.read(fd, MAX_CREDENTIAL + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_CREDENTIAL:
        raise SandboxError("Unsafe coding credential file")
    try:
        value = json.loads(raw)
    except ValueError:
        raise SandboxError("Unsafe coding credential file") from None
    if not isinstance(value, dict):
        raise SandboxError("Coding login required")
    return value


def provider_view(value):
    if isinstance(value.get("OPENAI_API_KEY"), str) and value["OPENAI_API_KEY"]:
        return "api.openai.com", "/v1/responses", {"Authorization": "Bearer " + value["OPENAI_API_KEY"]}
    tokens = value.get("tokens") or {}
    if isinstance(tokens.get("access_token"), str) and tokens["access_token"]:
        headers = {"Authorization": "Bearer " + tokens["access_token"], "OpenAI-Beta": "responses=experimental"}
        if isinstance(tokens.get("account_id"), str):
            headers["ChatGPT-Account-Id"] = tokens["account_id"]
        return "chatgpt.com", "/backend-api/codex/responses", headers
    raise SandboxError("Coding login required")


def credentials(path):
    return provider_view(read_auth(path))


def apply_refresh(value, refreshed):
    """Persist id_token, access_token, refresh_token and last_refresh the way Codex does."""
    if not isinstance(refreshed, dict):
        raise SandboxError("Coding token refresh failed")
    tokens = dict(value.get("tokens") or {})
    for key in ("id_token", "access_token", "refresh_token"):
        if key not in refreshed or refreshed[key] is None:
            continue
        incoming = refreshed[key]
        if not isinstance(incoming, str) or not incoming:
            raise SandboxError("Coding token refresh failed")
        if key == "id_token" and jwt_payload(incoming) is None:
            raise SandboxError("Coding token refresh failed")
        tokens[key] = incoming
    if not isinstance(tokens.get("access_token"), str) or not tokens["access_token"]:
        raise SandboxError("Coding token refresh failed")
    updated = dict(value)
    updated["tokens"] = tokens
    updated["last_refresh"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return updated


def write_auth(path, value):
    """Atomically replace a credential file. Owner, mode 0600 and nlink 1 stay intact."""
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    if len(raw) > MAX_CREDENTIAL:
        raise SandboxError("Unsafe coding credential file")
    directory = source = temp_fd = None
    temp_name = None
    try:
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        source = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
        info = os.fstat(source)
        real = os.stat(path, follow_symlinks=False)
        if info.st_ino != real.st_ino or not credential_metadata_ok(info):
            raise SandboxError("Unsafe coding credential file")
        if not stat.S_ISREG(real.st_mode) or real.st_nlink != 1:
            raise SandboxError("Unsafe coding credential file")
        temp_name = "." + path.name + "." + secrets.token_hex(8)
        temp_fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                          0o600, dir_fd=directory)
        os.fchmod(temp_fd, 0o600)
        os.fchown(temp_fd, real.st_uid, real.st_gid)
        pending = raw
        while pending:
            count = os.write(temp_fd, pending)
            if count <= 0:
                raise SandboxError("Unsafe coding credential file")
            pending = pending[count:]
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = None
        os.rename(temp_name, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        temp_name = None
        os.fsync(directory)
        final = os.stat(path, follow_symlinks=False)
        if (not stat.S_ISREG(final.st_mode) or final.st_nlink != 1 or final.st_mode & 0o077
            or final.st_uid != real.st_uid or final.st_size > MAX_CREDENTIAL):
            raise SandboxError("Unsafe coding credential file")
    except OSError:
        raise SandboxError("Unsafe coding credential file") from None
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        if temp_name is not None and directory is not None:
            try:
                os.unlink(temp_name, dir_fd=directory)
            except OSError:
                pass
        if source is not None:
            os.close(source)
        if directory is not None:
            os.close(directory)


def _bearer(headers):
    value = headers.get("Authorization", "")
    if isinstance(value, str) and value.startswith("Bearer "):
        return value[7:]
    return None


class PublicTLS(http.client.HTTPSConnection):
    def connect(self):
        # Resolve once, reject every non-public result, then connect to the checked IP.
        results = socket.getaddrinfo(self.host, 443, type=socket.SOCK_STREAM)
        if not results or any(not ipaddress.ip_address(item[4][0]).is_global for item in results):
            raise SandboxError("Private network destination rejected")
        address = results[0][4][0]
        raw = socket.create_connection((address, 443), timeout=self.timeout)
        raw.settimeout(self.timeout)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)


class _Upstream:
    def __init__(self, connection, response, maximum):
        self.connection = connection
        self.response = response
        self.status = response.status
        self.content_type = response.getheader("Content-Type") or "application/octet-stream"
        self.maximum = maximum
        self.total = 0

    def read(self, size, timeout=None):
        if self.total > self.maximum:
            raise SandboxError("Relay response limit")
        sock = self.connection.sock
        if sock is not None and timeout is not None:
            sock.settimeout(timeout if timeout > 0 else 0.001)
        amount = min(size, self.maximum - self.total + 1)
        chunk = self.response.read1(amount)
        if not isinstance(chunk, (bytes, bytearray)):
            chunk = bytes(chunk)
        if chunk and self.total + len(chunk) > self.maximum:
            raise SandboxError("Relay response limit")
        self.total += len(chunk)
        return chunk

    def abort(self):
        sock = self.connection.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            self.connection.close()
        except OSError:
            pass


def upstream(host, path, method, body, headers, maximum=MAX_RESPONSE, on_connect=None, timeout=None):
    connection = PublicTLS(host, timeout=IDLE_TIMEOUT if timeout is None else timeout)
    if on_connect is not None:
        connect = connection.connect

        def wrapped():
            connect()
            on_connect(connection)

        connection.connect = wrapped
    try:
        # The relay's own User-Agent wins over any sandbox-supplied value.
        outgoing = {key: value for key, value in headers.items() if str(key).lower() != "user-agent"}
        connection.request(method, path, body=body,
                           headers={"User-Agent": "agentic-home-coding/1", **outgoing})
        response = connection.getresponse()
    except BaseException:
        connection.close()
        raise
    stream = _Upstream(connection, response, maximum)
    relayed = select_response_headers(response.getheaders())
    return RelayResponse(stream.status, stream.content_type, maximum, read=stream.read, abort=stream.abort,
                         headers=relayed)


class RelayResponse:
    """A provider response. `body` is set only when the bytes are already complete."""

    def __init__(self, status, content_type, maximum, *, body=None, read=None, abort=None, headers=None):
        self.status = status
        self.content_type = content_type
        self.maximum = maximum
        self.headers = {} if headers is None else dict(headers)
        self.body = body
        self._view = body
        self._read = read
        self._abort = abort or (lambda: None)

    def read(self, size, timeout=None):
        if self._view is not None:
            part = self._view[:size]
            self._view = self._view[size:]
            return part
        if self._read is None:
            return b""
        part = self._read(size, timeout)
        return b"" if not part else bytes(part)

    def abort(self):
        self._abort()

    def collect(self):
        if self.body is not None:
            return self.body
        chunks, total = [], 0
        while True:
            part = self.read(65536, None)
            if not part:
                break
            total += len(part)
            if total > self.maximum:
                self.abort()
                raise SandboxError("Relay response limit")
            chunks.append(part)
        self.body = b"".join(chunks)
        self._view = b""
        return self.body

    def __getitem__(self, index):
        if index == 0:
            return self.status
        if index == 1:
            return self.content_type
        if index == 2:
            return self.collect()
        raise IndexError(index)

    def __iter__(self):
        yield self.status
        yield self.content_type
        yield self.collect()


def as_response(result, maximum):
    if isinstance(result, RelayResponse):
        if result.maximum > maximum:
            result.maximum = maximum
        result.headers = select_response_headers(result.headers)
        return result
    if isinstance(result, tuple) and len(result) in {3, 4}:
        status, content_type, payload = result[:3]
        headers = select_response_headers(result[3] if len(result) == 4 else None)
        if isinstance(payload, (bytes, bytearray)):
            if len(payload) > maximum:
                raise SandboxError("Relay response limit")
            return RelayResponse(int(status), str(content_type), maximum, body=bytes(payload),
                                 headers=headers)
        read = getattr(payload, "read", None)
        abort = getattr(payload, "abort", None)
        if read is None:
            iterator = iter(payload)

            def read(size, timeout=None, iterator=iterator):
                try:
                    return next(iterator)
                except StopIteration:
                    return b""

            abort = None
        return RelayResponse(int(status), str(content_type), maximum, read=read, abort=abort, headers=headers)
    raise SandboxError("Relay response limit")


def _nodelay(sock):
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass


class Relay:
    def __init__(self, path: Path, credential_path: Path, deadline: float, *, transport=None,
                 model="gpt-6-astra", idle_timeout=None):
        self.path, self.credential_path, self.deadline = path, credential_path, deadline
        self.transport = transport or self._upstream
        self.calls, self.lock = 0, threading.Lock()
        self.refresh_lock = threading.Lock()
        self.server = self.thread = None
        self.model = model
        self.connections = threading.BoundedSemaphore(2)
        self.auth_failed = False
        self.quota_reached = False
        self.provider_rejected = False
        self.provider_unavailable = False
        self.turns_exhausted = False
        if idle_timeout is None:
            self.idle_timeout = float(IDLE_TIMEOUT)
        elif isinstance(idle_timeout, bool) or not isinstance(idle_timeout, (int, float)) or idle_timeout <= 0:
            raise ValueError("Invalid relay idle timeout")
        else:
            self.idle_timeout = float(idle_timeout)
        self.stopped = threading.Event()
        self._connections = []
        self._connections_lock = threading.Lock()
        # HomeRelay.request keeps the three-argument signature the HAOS App overrides.
        self._client_headers = threading.local()

    def _bind_client_headers(self, headers):
        self._client_headers.value = headers

    def _clear_client_headers(self):
        self._client_headers.value = None

    def _read_sandbox_headers(self):
        return getattr(self._client_headers, "value", None)

    def _timeout(self):
        """Idle gap, shortened so a blocked read cannot outlive the job deadline."""
        remaining = self.deadline - time.time()
        if remaining <= 0 or self.stopped.is_set():
            raise SandboxError("Relay authorization expired or exhausted")
        return min(self.idle_timeout, remaining)

    def _track(self, connection):
        with self._connections_lock:
            self._connections.append(connection)
        if self.stopped.is_set() and connection.sock is not None:
            try:
                connection.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _upstream(self, host, path, method, body, headers, maximum=MAX_RESPONSE):
        return upstream(host, path, method, body, headers, maximum=maximum, on_connect=self._track,
                        timeout=self._timeout())

    def _abort_connections(self):
        with self._connections_lock:
            connections = list(self._connections)
        for connection in connections:
            sock = getattr(connection, "sock", None)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            try:
                connection.close()
            except OSError:
                pass

    def _read_limited(self, response):
        raw = b""
        while len(raw) <= response.maximum:
            timeout = self._timeout()
            try:
                chunk = response.read(65536, timeout)
            except TimeoutError:
                if time.time() >= self.deadline or timeout < self.idle_timeout:
                    raise SandboxError("Relay authorization expired or exhausted") from None
                raise SandboxError("Coding token refresh failed") from None
            if not chunk:
                break
            raw += chunk
        if len(raw) > response.maximum:
            raise SandboxError("Relay response limit")
        return raw

    def _fetch_token(self, refresh_token):
        body = json.dumps({"client_id": CHATGPT_CLIENT_ID, "grant_type": "refresh_token",
                           "refresh_token": refresh_token}, separators=(",", ":"), sort_keys=True).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        try:
            result = self.transport(TOKEN_HOST, TOKEN_PATH, "POST", body, headers, maximum=MAX_CREDENTIAL)
        except SandboxError:
            raise
        except Exception:
            raise SandboxError("Coding token refresh failed") from None
        response = as_response(result, MAX_CREDENTIAL)
        try:
            raw = self._read_limited(response)
            if response.status != 200:
                if refresh_rejected(response.status, raw):
                    raise RefreshRejected()
                raise SandboxError("Coding token refresh failed")
            try:
                parsed = json.loads(raw)
            except ValueError:
                raise SandboxError("Coding token refresh failed") from None
            if not isinstance(parsed, dict):
                raise SandboxError("Coding token refresh failed")
            return parsed
        finally:
            response.abort()

    def _refresh(self, *, force, rejected=None):
        """Return "ok", "rejected" (sign in again) or "unavailable" (transient; try later)."""
        with self.refresh_lock:
            try:
                value = read_auth(self.credential_path)
            except (OSError, ValueError, SandboxError):
                return "rejected"
            tokens = value.get("tokens")
            if not isinstance(tokens, dict):
                return "rejected"
            access = tokens.get("access_token")
            if rejected is not None and isinstance(access, str) and access != rejected:
                return "ok"
            if not force and not refresh_due(value, time.time()):
                return "ok"
            refresh_token = tokens.get("refresh_token")
            if not isinstance(refresh_token, str) or not refresh_token:
                return "rejected"
            try:
                updated = apply_refresh(value, self._fetch_token(refresh_token))
            except RefreshRejected:
                return "rejected"
            except SandboxError as exc:
                if exc.args and exc.args[0] == "Relay authorization expired or exhausted":
                    raise
                return "unavailable"
            except Exception:
                return "unavailable"
            try:
                write_auth(self.credential_path, updated)
            except SandboxError:
                # The provider may already have rotated the refresh token. Only a new sign-in recovers.
                return "rejected"
            return "ok"

    def _provider(self):
        return credentials(self.credential_path)

    def _exchange(self, host, target, payload, headers, maximum):
        result = self.transport(host, target, "POST" if payload is not None else "GET", payload, headers,
                                maximum=maximum)
        return as_response(result, maximum)

    def _rejected(self):
        return RelayResponse(401, "application/json", MAX_RESPONSE, body=_REJECTED)

    def request(self, method, path, body):
        with self.lock:
            if self.calls >= MAX_CALLS:
                self.turns_exhausted = True
                raise SandboxError("Relay authorization expired or exhausted")
            if time.time() >= self.deadline or self.stopped.is_set():
                raise SandboxError("Relay authorization expired or exhausted")
            self.calls += 1
        if method == "POST" and path == "/v1/responses":
            if self.auth_failed:
                raise SandboxError("Coding provider requires reauthentication")
            if len(body) > MAX_REQUEST:
                raise SandboxError("Relay request limit")
            value = json.loads(body)
            if not isinstance(value, dict) or value.get("model") != self.model:
                raise SandboxError("Unsupported model")
            host, target, headers = self._provider()
            # Remote MCP and hosted computer/network tools are not this worker's authority.
            allowed_tools = {"function", "custom", "local_shell"}
            if any(tool.get("type") not in allowed_tools for tool in value.get("tools", [])):
                raise SandboxError("Remote tool not allowed")
            value["store"] = False
            payload = json.dumps(value).encode()
            sandbox_headers = self._read_sandbox_headers()

            def model_headers(auth, provider_host):
                forwarded = select_request_headers(sandbox_headers, chatgpt=provider_host == "chatgpt.com")
                base = {**forwarded, **auth, "Content-Type": "application/json"}
                return {**base, "Accept": "text/event-stream"}

            request_headers = model_headers(headers, host)
            if host == "chatgpt.com":
                try:
                    due = refresh_due(read_auth(self.credential_path), time.time())
                except (OSError, ValueError, SandboxError):
                    due = False
                if due:
                    # A transient failure keeps the current token; an expired one comes back as 401 below.
                    if self._refresh(force=False) == "rejected":
                        self.auth_failed = True
                        return self._rejected()
                    host, target, headers = self._provider()
                    request_headers = model_headers(headers, host)
            response = self._exchange(host, target, payload, request_headers, MAX_RESPONSE)
            if response.status == 401 and host == "chatgpt.com":
                rejected = _bearer(headers)
                response.abort()
                outcome = self._refresh(force=True, rejected=rejected)
                if outcome == "rejected":
                    self.auth_failed = True
                    return self._rejected()
                if outcome == "unavailable":
                    raise SandboxError("Coding token refresh unavailable")
                host, target, headers = self._provider()
                request_headers = model_headers(headers, host)
                response = self._exchange(host, target, payload, request_headers, MAX_RESPONSE)
            if response.status in {401, 403}:
                self.auth_failed = True
            elif response.status == 429:
                self.quota_reached = True
            elif 400 <= response.status <= 499:
                self.provider_rejected = True
            elif 500 <= response.status <= 599:
                self.provider_unavailable = True
            return response
        query = urlsplit(path)
        if method == "GET" and query.path == "/fetch":
            params = parse_qs(query.query, strict_parsing=True)
            if set(params) != {"url"} or len(params["url"]) != 1:
                raise SandboxError("Invalid source request")
            url = urlsplit(params["url"][0])
            if (url.scheme != "https" or url.hostname not in SOURCE_HOSTS or url.port not in {None, 443}
                or url.username or url.password or url.fragment or len(url.geturl()) > 2048):
                raise SandboxError("Source not allowed")
            return self._exchange(url.hostname, url.path + ("?" + url.query if url.query else ""), None,
                                  {"Accept": "text/plain,application/json,text/html"}, FETCH_LIMIT)
        raise SandboxError("Relay route not allowed")

    def start(self):
        relay = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                self.connection.settimeout(relay.idle_timeout)
                _nodelay(self.connection)
                self._started = False

            def log_message(self, *args):
                pass

            def _chunk(self, chunk):
                self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()

            def _begin(self, response, length=None):
                self.send_response(response.status)
                self._started = True
                self.send_header("Content-Type", response.content_type)
                for name, value in response.headers.items():
                    self.send_header(name, value)
                if length is None:
                    self.send_header("Transfer-Encoding", "chunked")
                else:
                    self.send_header("Content-Length", str(length))
                self.send_header("Connection", "close")
                self.end_headers()

            def _emit(self, response):
                self.close_connection = True
                if time.time() >= relay.deadline or relay.stopped.is_set():
                    raise SandboxError("Relay authorization expired or exhausted")
                if response.body is not None:
                    if len(response.body) > response.maximum:
                        raise SandboxError("Relay response limit")
                    self._begin(response, len(response.body))
                    self.wfile.write(response.body)
                    self.wfile.flush()
                    return
                self._begin(response)
                self.wfile.flush()
                total = 0
                while True:
                    timeout = relay._timeout()
                    try:
                        chunk = response.read(65536, timeout)
                    except TimeoutError:
                        if time.time() >= relay.deadline or timeout < relay.idle_timeout:
                            raise SandboxError("Relay authorization expired or exhausted") from None
                        raise SandboxError("Relay idle timeout") from None
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > response.maximum:
                        raise SandboxError("Relay response limit")
                    self._chunk(chunk)
                self._chunk_end()

            def _chunk_end(self):
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

            def _fail(self):
                body = _REJECTED
                self.close_connection = True
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()

            def handle_request(self):
                self.close_connection = True
                self.connection.settimeout(relay.idle_timeout)
                response = None
                relay._bind_client_headers(self.headers)
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 <= length <= MAX_REQUEST or self.headers.get("Transfer-Encoding"):
                        raise ValueError("Invalid framing")
                    result = relay.request(self.command, self.path, self.rfile.read(length))
                    response = result if isinstance(result, RelayResponse) else as_response(result, MAX_RESPONSE)
                    self._emit(response)
                except (OSError, ValueError, SandboxError, http.client.HTTPException):
                    if not self._started:
                        try:
                            self._fail()
                        except OSError:
                            pass
                    else:
                        self.close_connection = True
                        try:
                            self.connection.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
                finally:
                    relay._clear_client_headers()
                    if response is not None:
                        response.abort()

            do_GET = do_POST = handle_request

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
            block_on_close = True

            def process_request(self, request, client_address):
                if not relay.connections.acquire(blocking=False):
                    self.shutdown_request(request)
                    return
                try:
                    super().process_request(request, client_address)
                except BaseException:
                    relay.connections.release()
                    raise

            def process_request_thread(self, request, client_address):
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    relay.connections.release()

        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.server = Server(str(self.path), Handler)
        try:
            self.path.chmod(0o600)
            os.chown(self.path, 10000, 10000)
        except OSError:
            self.server.server_close()
            self.path.unlink(missing_ok=True)
            self.server = None
            raise
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.deadline = 0
        self.stopped.set()
        self._abort_connections()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.thread:
            self.thread.join(timeout=3)
            self.thread = None
        self.path.unlink(missing_ok=True)
