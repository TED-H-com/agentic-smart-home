"""Local-socket proof of relay streaming and ChatGPT token refresh. No network and no account."""
import base64
import json
import os
import socket
import stat
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "host"))
from agentic_host.coding_program import program, sandbox_proxy  # noqa: E402
from agentic_host.coding_relay import (  # noqa: E402
    ACCESS_REFRESH_WINDOW,
    CHATGPT_CLIENT_ID,
    FETCH_LIMIT,
    IDLE_TIMEOUT,
    MAX_CALLS,
    MAX_CREDENTIAL,
    MAX_REQUEST,
    MAX_RESPONSE,
    TOKEN_HOST,
    TOKEN_PATH,
    TOKEN_REFRESH_INTERVAL,
    PublicTLS,
    Relay,
    RelayResponse,
    apply_refresh,
    credential_metadata_ok,
    credentials,
    refresh_due,
    select_request_headers,
    select_response_headers,
    upstream,
    write_auth,
)
from agentic_host.coding_sandbox import SandboxError  # noqa: E402

REQUEST = b'{"model":"gpt-6-astra"}'
OLD_REFRESH = "rt-old-refresh-secret"
NEW_REFRESH = "rt-new-refresh-secret"
LEAK = "rt-must-never-reach-sandbox"
# AF_UNIX paths stay short; the disk scratch root is the one CI and AGENTS.md use.
SOCK_ROOT = Path.home() / "agent-work/tmp/pytest/relay-sockets"


def stamp(when):
    return datetime.fromtimestamp(when, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def jwt_token(payload):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return "e30." + body + ".sig"


def auth_value(access, refresh, last_refresh, id_token, **extra):
    value = {"auth_mode": "chatgpt", "tokens": {"id_token": id_token, "access_token": access,
             "refresh_token": refresh, "account_id": "acct-1"}, "last_refresh": last_refresh}
    value.update(extra)
    return value


def install_auth(path, value, mode=0o600):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    try:
        os.fchmod(fd, mode)
        os.write(fd, json.dumps(value).encode())
    finally:
        os.close(fd)


def patch_owner(monkeypatch):
    """Pretend the credential inode is uid 1010. os.stat stays the real owner."""
    real = os.fstat

    def fake(fd):
        info = real(fd)
        return os.stat_result((info.st_mode, info.st_ino, info.st_dev, info.st_nlink, 1010, info.st_gid,
                               info.st_size, info.st_atime, info.st_mtime, info.st_ctime))

    monkeypatch.setattr(os, "fstat", fake)
    return real


def sock_path(name):
    SOCK_ROOT.mkdir(parents=True, exist_ok=True)
    path = SOCK_ROOT / f"{os.getpid()}-{name}-{uuid.uuid4().hex[:8]}.sock"
    path.unlink(missing_ok=True)
    assert len(str(path)) < 108
    return path


def http_request(body=REQUEST, extra=b""):
    head = b"POST /v1/responses HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
    return head + b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n" + extra + b"\r\n" + body


def recv_until(sock, needle, timeout=2):
    sock.settimeout(timeout)
    data = b""
    while needle not in data:
        part = sock.recv(65536)
        if not part:
            break
        data += part
    return data


def recv_rest(sock, timeout=2):
    sock.settimeout(timeout)
    data = b""
    while True:
        try:
            part = sock.recv(1024 * 1024)
        except TimeoutError:
            break
        if not part:
            break
        data += part
    return data


def recv_request(conn):
    """Read one whole request, body included, as the real relay does before it answers.

    http.client sends the head and the body separately. Answering after the head alone
    races the body write and the proxy reports 502.
    """
    data = b""
    while b"\r\n\r\n" not in data:
        part = conn.recv(65536)
        if not part:
            return data
        data += part
    head, _, rest = data.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value)
    while len(rest) < length:
        part = conn.recv(65536)
        if not part:
            break
        rest += part
    return head + b"\r\n\r\n" + rest

def patch_chown(monkeypatch):
    monkeypatch.setattr(os, "chown", lambda *args, **kwargs: None)


def patch_credentials(monkeypatch):
    monkeypatch.setattr("agentic_host.coding_relay.credentials",
                        lambda path: ("chatgpt.com", "/backend-api/codex/responses",
                                      {"Authorization": "Bearer fixture-access"}))


def start_relay(tmp_path, monkeypatch, transport, **kwargs):
    patch_chown(monkeypatch)
    path = sock_path("relay")
    relay = Relay(path, kwargs.pop("credential_path", tmp_path / "missing-auth.json"),
                  kwargs.pop("deadline", time.time() + 30), transport=transport, **kwargs)
    relay.start()
    return relay


def blocking_stream(relay, release, early=b"data: early\n\n", late=b"data: late\n\n"):
    sent = []

    def read(size, timeout=None):
        if not sent:
            sent.append(True)
            return early
        deadline_at = time.monotonic() + 5
        while time.monotonic() < deadline_at:
            if release.is_set():
                return late
            if relay.stopped.wait(0.05):
                raise SandboxError("Relay authorization expired or exhausted")
        raise TimeoutError()

    def transport(host, path, method, body, headers, maximum=None):
        assert json.loads(body)["store"] is False
        return RelayResponse(200, "text/event-stream", maximum or MAX_RESPONSE, read=read)

    return transport


def test_stream_reaches_client_before_upstream_finishes(tmp_path, monkeypatch):
    patch_credentials(monkeypatch)
    release = threading.Event()
    relay = None
    try:
        relay = Relay(sock_path("direct"), tmp_path / "missing.json", time.time() + 30)
        relay.transport = blocking_stream(relay, release)
        patch_chown(monkeypatch)
        relay.start()
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(relay.path))
        client.sendall(http_request())
        early = recv_until(client, b"data: early")
        assert not release.is_set()
        assert early.count(b"HTTP/1.") == 1 and b"chunked" in early.lower()
        release.set()
        rest = recv_rest(client)
        assert b"data: late" in rest and (early + rest).count(b"HTTP/1.") == 1
        client.close()
    finally:
        release.set()
        if relay and relay.server:
            relay.close()


def test_proxy_forwards_incrementally_and_idle_timeout_fires(tmp_path):
    early, late = b"data: early\n\n", b"data: late\n\n"
    body = early + late
    release, ready = threading.Event(), threading.Event()
    path = sock_path("upstream")

    def upstream():
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen(1)
        ready.set()
        server.settimeout(5)
        conn, _ = server.accept()
        try:
            conn.settimeout(2)
            data = recv_request(conn)
            assert b"Authorization" not in data and LEAK.encode() not in data
            head = (b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: "
                    + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n")
            conn.sendall(head + early)
            assert release.wait(5)
            conn.sendall(late)
        finally:
            conn.close()
            server.close()

    thread = threading.Thread(target=upstream, daemon=True)
    thread.start()
    assert ready.wait(2)
    namespace = {}
    exec(compile(sandbox_proxy(str(path), port=0, idle=30), "proxy", "exec"), namespace)
    proxy = namespace["server"]
    try:
        client = socket.create_connection(("127.0.0.1", proxy.server_address[1]), timeout=2)
        client.sendall(http_request(extra=b"Authorization: Bearer " + LEAK.encode() + b"\r\n"))
        first = recv_until(client, b"data: early")
        assert not release.is_set()
        assert first.count(b"HTTP/1.") == 1 and b"chunked" in first.lower() and LEAK.encode() not in first
        release.set()
        assert b"data: late" in recv_rest(client)
        client.close()
    finally:
        release.set()
        proxy.shutdown()
        proxy.server_close()
        thread.join(2)

    hang_path, hang_ready = sock_path("hang"), threading.Event()

    def hang():
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(hang_path))
        server.listen(1)
        hang_ready.set()
        server.settimeout(3)
        try:
            conn, _ = server.accept()
            conn.settimeout(2)
            try:
                while conn.recv(65536):
                    pass
            except OSError:
                pass
            conn.close()
        except OSError:
            pass
        server.close()

    hanger = threading.Thread(target=hang, daemon=True)
    hanger.start()
    assert hang_ready.wait(2)
    namespace = {}
    started = time.monotonic()
    exec(compile(sandbox_proxy(str(hang_path), port=0, idle=0.3), "proxy-idle", "exec"), namespace)
    idle_proxy = namespace["server"]
    try:
        client = socket.create_connection(("127.0.0.1", idle_proxy.server_address[1]), timeout=2)
        client.sendall(http_request())
        data = recv_rest(client, timeout=2)
        assert time.monotonic() - started < 2
        assert b"502" in data and data.count(b"HTTP/1.") == 1
        client.close()
    finally:
        idle_proxy.shutdown()
        idle_proxy.server_close()
        hanger.join(2)


def test_idle_timeout_cap_deadline_and_cancellation(tmp_path, monkeypatch):
    patch_credentials(monkeypatch)
    observed = []

    def idle_transport(host, path, method, body, headers, maximum=None):
        def read(size, timeout=None):
            observed.append(timeout)
            if len(observed) == 1:
                return b"data: early\n\n"
            if timeout is None or timeout > 2:
                raise SandboxError("Relay idle timeout")
            time.sleep(timeout)
            raise TimeoutError()
        return RelayResponse(200, "text/event-stream", MAX_RESPONSE, read=read)

    relay = start_relay(tmp_path, monkeypatch, idle_transport, idle_timeout=0.3)
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(relay.path))
        started = time.monotonic()
        client.sendall(http_request())
        data = recv_until(client, b"data: early", timeout=2) + recv_rest(client, timeout=2)
        elapsed = time.monotonic() - started
        assert b"data: early" in data and data.count(b"HTTP/1.") == 1 and b"0\r\n\r\n" not in data
        assert observed[1] == pytest.approx(0.3, abs=0.05) and 0.2 <= elapsed < 2
        client.close()
    finally:
        relay.close()

    marker = b"b" * 32
    pieces = [b"a" * (1024 * 1024)] * 8 + [marker + b"b" * (1024 * 1024 - len(marker))]

    def capped(host, path, method, body, headers, maximum=None):
        iterator = iter(pieces)

        def read(size, timeout=None):
            try:
                return next(iterator)
            except StopIteration:
                return b""

        return RelayResponse(200, "application/octet-stream", MAX_RESPONSE, read=read)

    relay = start_relay(tmp_path, monkeypatch, capped)
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(relay.path))
        client.sendall(http_request())
        data = recv_rest(client, timeout=5)
        payload = data.split(b"\r\n\r\n", 1)[1]
        received = payload.count(b"a")
        assert received == MAX_RESPONSE and marker not in payload and data.count(b"HTTP/1.") == 1
        client.close()
    finally:
        relay.close()

    deadline_seen = []

    def deadline_transport(host, path, method, body, headers, maximum=None):
        def read(size, timeout=None):
            deadline_seen.append(timeout)
            if len(deadline_seen) == 1:
                return b"data: early\n\n"
            if timeout is None or timeout > 2:
                raise SandboxError("Relay authorization expired or exhausted")
            time.sleep(timeout)
            raise TimeoutError()
        return RelayResponse(200, "text/event-stream", MAX_RESPONSE, read=read)

    relay = start_relay(tmp_path, monkeypatch, deadline_transport, deadline=time.time() + 0.4, idle_timeout=10)
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(relay.path))
        started = time.monotonic()
        client.sendall(http_request())
        data = recv_until(client, b"data: early", timeout=2) + recv_rest(client, timeout=2)
        assert time.monotonic() - started < 2 and deadline_seen[1] < 2
        assert data.count(b"HTTP/1.") == 1 and b"0\r\n\r\n" not in data
        client.close()
    finally:
        relay.close()

    release = threading.Event()
    relay = Relay(sock_path("cancel"), tmp_path / "missing.json", time.time() + 30, idle_timeout=30)
    relay.transport = blocking_stream(relay, release, late=b"data: should-not-arrive\n\n")
    patch_chown(monkeypatch)
    relay.start()
    box = {}

    def client_read():
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(relay.path))
        client.sendall(http_request())
        early = recv_until(client, b"data: early", timeout=2)
        box["early"] = early
        box["data"] = early + recv_rest(client, timeout=3)
        client.close()

    reader = threading.Thread(target=client_read, daemon=True)
    try:
        reader.start()
        assert _wait(lambda: b"data: early" in box.get("early", b""))
        started = time.monotonic()
        closer = threading.Thread(target=relay.close, daemon=True)
        closer.start()
        closer.join(3)
        assert not closer.is_alive() and time.monotonic() - started < 3
        reader.join(3)
        data = box["data"]
        assert b"should-not-arrive" not in data and data.count(b"HTTP/1.") == 1
    finally:
        release.set()
        if relay.server:
            relay.close()


def _wait(predicate, limit=2):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_refresh_rules_write_back_and_one_retry(tmp_path, monkeypatch):
    real_fstat = patch_owner(monkeypatch)
    now = 1_800_000_000
    far = jwt_token({"exp": now + 30 * 86400})
    soon = jwt_token({"exp": now + 60})
    recent = stamp(now)
    assert refresh_due(auth_value(soon, OLD_REFRESH, recent, far), now)
    assert refresh_due(auth_value(jwt_token({"exp": now + ACCESS_REFRESH_WINDOW}), OLD_REFRESH, recent, far), now)
    assert not refresh_due(auth_value(jwt_token({"exp": now + ACCESS_REFRESH_WINDOW + 1}), OLD_REFRESH, recent, far), now)
    assert not refresh_due(auth_value(far, OLD_REFRESH, stamp(now - TOKEN_REFRESH_INTERVAL + 5), far), now)
    # Like pinned Codex, a parsed exp decides alone; the 8-day rule is only for tokens without one.
    assert not refresh_due(auth_value(far, OLD_REFRESH, stamp(now - TOKEN_REFRESH_INTERVAL - 5), far), now)
    assert not refresh_due(auth_value("opaque-access", OLD_REFRESH, None, far), now)
    assert refresh_due(auth_value("opaque-access", OLD_REFRESH, stamp(now - TOKEN_REFRESH_INTERVAL - 5), far), now)
    bool_exp = jwt_token({"exp": True})
    assert not refresh_due(auth_value(bool_exp, OLD_REFRESH, recent, far), now)

    def run(value, replies):
        path = tmp_path / uuid.uuid4().hex / "auth.json"
        install_auth(path, value)
        calls = []

        def transport(host, route, method, body, headers, maximum=None):
            calls.append((host, route, body, dict(headers), maximum))
            return replies.pop(0)

        relay = Relay(tmp_path / "unused.sock", path, time.time() + 30, transport=transport)
        status, content_type, raw = relay.request("POST", "/v1/responses", REQUEST)
        return path, calls, status, content_type, raw, relay

    new_access = jwt_token({"exp": int(time.time()) + 30 * 86400, "kind": "access"})
    new_id = jwt_token({"exp": int(time.time()) + 30 * 86400, "kind": "id-token-secret"})
    token_reply = (200, "application/json", json.dumps({"access_token": new_access, "id_token": new_id,
                   "refresh_token": NEW_REFRESH, "token_type": "bearer"}).encode())
    model_reply = (200, "text/event-stream", b"data: ok\n\n")
    live_soon = jwt_token({"exp": int(time.time()) + 60})
    live_far = jwt_token({"exp": int(time.time()) + 30 * 86400})
    path, calls, status, _, raw, relay = run(
        auth_value(live_soon, OLD_REFRESH, stamp(time.time()), jwt_token({"kind": "id-old"})),
        [token_reply, model_reply])
    assert status == 200 and raw == b"data: ok\n\n" and relay.calls == 1
    assert [item[0] for item in calls] == [TOKEN_HOST, "chatgpt.com"]
    assert calls[0][1] == TOKEN_PATH and calls[0][4] == MAX_CREDENTIAL
    assert calls[0][2] == json.dumps({"client_id": CHATGPT_CLIENT_ID, "grant_type": "refresh_token",
                                      "refresh_token": OLD_REFRESH}, separators=(",", ":"), sort_keys=True).encode()
    assert calls[0][3]["Content-Type"] == "application/json"
    assert calls[1][3]["Authorization"] == "Bearer " + new_access
    assert calls[1][3]["ChatGPT-Account-Id"] == "acct-1"
    assert json.loads(calls[1][2])["store"] is False
    assert OLD_REFRESH.encode() not in calls[1][2] and NEW_REFRESH.encode() not in raw and new_id.encode() not in raw
    saved = json.loads(path.read_text())
    assert saved["tokens"]["refresh_token"] == NEW_REFRESH and saved["tokens"]["access_token"] == new_access
    assert saved["tokens"]["id_token"] == new_id and saved["tokens"]["account_id"] == "acct-1"
    assert saved["auth_mode"] == "chatgpt" and "token_type" not in saved
    info = path.stat()
    assert stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid()
    assert info.st_mode & 0o777 == 0o600 and [item.name for item in path.parent.iterdir()] == ["auth.json"]
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        assert not credential_metadata_ok(real_fstat(fd))
    finally:
        os.close(fd)
    assert credentials(path)[2]["Authorization"] == "Bearer " + new_access

    kept = auth_value("opaque-access", OLD_REFRESH, stamp(time.time() - TOKEN_REFRESH_INTERVAL - 5),
                      jwt_token({"kind": "id-old"}))
    path, calls, status, _, raw, _ = run(kept, [(200, "application/json", json.dumps(
        {"access_token": new_access, "id_token": new_id}).encode()), model_reply])
    assert calls[0][0] == TOKEN_HOST and json.loads(path.read_text())["tokens"]["refresh_token"] == OLD_REFRESH
    assert NEW_REFRESH.encode() not in raw

    fresh = auth_value(live_far, OLD_REFRESH, stamp(time.time()), jwt_token({"kind": "id-old"}))
    _, calls, status, _, _, relay = run(fresh, [model_reply])
    assert calls[0][0] == "chatgpt.com" and relay.auth_failed is False

    quiet = auth_value(live_far, OLD_REFRESH, stamp(time.time()), jwt_token({"kind": "id-old"}))
    secret_body = json.dumps({"error": LEAK, "refresh_token": OLD_REFRESH}).encode()
    path = tmp_path / "transient" / "auth.json"
    install_auth(path, quiet)
    calls = []
    replies = [(401, "application/json", b'{"error":"expired"}'), (500, "application/json", secret_body)]

    def transient(host, route, method, body, headers, maximum=None):
        calls.append(host)
        return replies.pop(0)

    # A token endpoint outage is transient: no sign-in is demanded and Codex may retry.
    relay = Relay(tmp_path / "transient.sock", path, time.time() + 30, transport=transient)
    with pytest.raises(SandboxError) as failure:
        relay.request("POST", "/v1/responses", REQUEST)
    assert calls == ["chatgpt.com", TOKEN_HOST] and not relay.auth_failed
    assert LEAK not in str(failure.value) and failure.value.__cause__ is None
    assert json.loads(path.read_text())["tokens"]["refresh_token"] == OLD_REFRESH
    assert json.loads(path.read_text())["tokens"]["access_token"] == live_far

    for reply in [(401, "application/json", secret_body),
                  (400, "application/json", b'{"error":"invalid_grant"}'),
                  (400, "application/json", b'{"error":{"code":"refresh_token_reused","message":"used"}}')]:
        path, calls, status, _, raw, relay = run(quiet, [(401, "application/json", b'{"error":"expired"}'), reply])
        assert status == 401 and raw == b'{"error":"coding_egress_rejected"}' and relay.auth_failed
        assert [item[0] for item in calls] == ["chatgpt.com", TOKEN_HOST]
        assert LEAK.encode() not in raw and OLD_REFRESH.encode() not in raw
        assert json.loads(path.read_text())["tokens"]["access_token"] == live_far
    with pytest.raises(SandboxError) as failure:
        relay.request("POST", "/v1/responses", REQUEST)
    assert LEAK not in str(failure.value) and failure.value.__cause__ is None

    # A due refresh that cannot reach the token host keeps the current, still valid token.
    soon_value = auth_value(live_soon, OLD_REFRESH, stamp(time.time()), jwt_token({"kind": "id-old"}))
    path, calls, status, _, raw, relay = run(soon_value, [(503, "application/json", b"{}"), model_reply])
    assert status == 200 and raw == b"data: ok\n\n" and not relay.auth_failed
    assert not relay.provider_unavailable and not relay.quota_reached and not relay.provider_rejected
    assert [item[0] for item in calls] == [TOKEN_HOST, "chatgpt.com"]
    assert calls[1][3]["Authorization"] == "Bearer " + live_soon

    burst = auth_value(live_far, OLD_REFRESH, stamp(time.time()), jwt_token({"kind": "id-old"}))
    path = tmp_path / "burst" / "auth.json"
    install_auth(path, burst)
    calls = []

    def retry_transport(host, route, method, body, headers, maximum=None):
        calls.append((host, headers.get("Authorization"), body))
        if host == TOKEN_HOST:
            return token_reply
        if headers.get("Authorization") == "Bearer " + live_far:
            return 401, "application/json", b'{"error":"expired"}'
        return 200, "text/event-stream", b"data: retried\n\n"

    relay = Relay(tmp_path / "retry.sock", path, time.time() + 30, transport=retry_transport)
    status, _, raw = relay.request("POST", "/v1/responses", REQUEST)
    assert status == 200 and raw == b"data: retried\n\n" and relay.calls == 1
    assert [item[0] for item in calls] == ["chatgpt.com", TOKEN_HOST, "chatgpt.com"]
    assert calls[2][1] == "Bearer " + new_access and OLD_REFRESH.encode() not in raw
    calls.clear()

    def forbidden(host, route, method, body, headers, maximum=None):
        calls.append(host)
        return 403, "application/json", b'{"error":"forbidden"}'

    relay.transport = forbidden
    relay.auth_failed = False
    status, _, raw = relay.request("POST", "/v1/responses", REQUEST)
    assert status == 403 and calls == ["chatgpt.com"] and relay.auth_failed and LEAK.encode() not in raw


def test_failed_refresh_exception_and_api_key_skip_token_endpoint(tmp_path, monkeypatch):
    patch_owner(monkeypatch)
    path = tmp_path / "auth.json"
    original = auth_value(jwt_token({"exp": int(time.time()) + 10 * 86400}), OLD_REFRESH, stamp(time.time()),
                          jwt_token({"kind": "id-old"}))
    install_auth(path, original)
    before = path.read_bytes()

    def explode(host, route, method, body, headers, maximum=None):
        if host == TOKEN_HOST:
            raise RuntimeError(LEAK + body.decode())
        return 401, "application/json", b"{}"

    relay = Relay(tmp_path / "sock", path, time.time() + 30, transport=explode)
    for _ in range(2):
        with pytest.raises(SandboxError) as failure:
            relay.request("POST", "/v1/responses", REQUEST)
        assert not relay.auth_failed and path.read_bytes() == before
        assert LEAK not in str(failure.value) and failure.value.__cause__ is None

    key_path = tmp_path / "key.json"
    install_auth(key_path, auth_value(jwt_token({"exp": int(time.time()) + 30}), OLD_REFRESH,
                 stamp(time.time() - TOKEN_REFRESH_INTERVAL - 10), jwt_token({"kind": "id-old"}),
                 OPENAI_API_KEY="sk-test-key"))
    hosts = []

    def api_transport(host, route, method, body, headers, maximum=None):
        hosts.append(host)
        assert OLD_REFRESH.encode() not in body and "sk-test-key" not in body.decode()
        return 401, "application/json", b'{"error":"expired"}'

    relay = Relay(tmp_path / "api.sock", key_path, time.time() + 30, transport=api_transport)
    status, _, raw = relay.request("POST", "/v1/responses", REQUEST)
    assert hosts == ["api.openai.com"] and status == 401 and relay.auth_failed and LEAK.encode() not in raw
    with pytest.raises(SandboxError):
        relay.request("POST", "/v1/responses", REQUEST)
    assert hosts == ["api.openai.com"]


def test_concurrent_refresh_happens_once(tmp_path, monkeypatch):
    patch_owner(monkeypatch)
    access = jwt_token({"exp": int(time.time()) + 30 * 86400, "n": 1})
    path = tmp_path / "auth.json"
    install_auth(path, auth_value(access, OLD_REFRESH, stamp(time.time()), jwt_token({"kind": "id-old"})))
    barrier, calls, results, errors = threading.Barrier(2), [], [], []
    new_access = jwt_token({"exp": int(time.time()) + 30 * 86400, "n": 2})

    def transport(host, route, method, body, headers, maximum=None):
        if host == TOKEN_HOST:
            calls.append(body)
            time.sleep(0.2)
            return 200, "application/json", json.dumps({"access_token": new_access, "id_token": jwt_token({"n": 3}),
                                                        "refresh_token": NEW_REFRESH}).encode()
        if headers.get("Authorization") == "Bearer " + access:
            barrier.wait(3)
            return 401, "application/json", b"{}"
        return 200, "text/event-stream", b"data: ok\n\n"

    relay = Relay(tmp_path / "sock", path, time.time() + 30, transport=transport)

    def call():
        try:
            results.append(tuple(relay.request("POST", "/v1/responses", REQUEST)))
        except Exception as exc:  # noqa: BLE001 - the test records worker failures
            errors.append(exc)

    threads = [threading.Thread(target=call) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert not errors and len(results) == 2 and len(calls) == 1
    assert all(item[0] == 200 and NEW_REFRESH.encode() not in item[2] and OLD_REFRESH.encode() not in item[2]
               for item in results)
    assert json.loads(path.read_text())["tokens"]["refresh_token"] == NEW_REFRESH


def test_metadata_rejection_limits_and_fixed_program(tmp_path, monkeypatch):
    real_fstat = patch_owner(monkeypatch)
    loose = tmp_path / "loose.json"
    install_auth(loose, auth_value("a", OLD_REFRESH, stamp(time.time()), "id"), mode=0o644)
    with pytest.raises(SandboxError) as failure:
        credentials(loose)
    assert OLD_REFRESH not in str(failure.value)
    linked = tmp_path / "linked.json"
    install_auth(linked, auth_value("a", OLD_REFRESH, stamp(time.time()), "id"))
    extra = tmp_path / "linked-again.json"
    os.link(linked, extra)
    with pytest.raises(SandboxError):
        credentials(linked)
    extra.unlink()
    target = tmp_path / "target.json"
    install_auth(target, {"OPENAI_API_KEY": "secret"})
    alias = tmp_path / "alias.json"
    alias.symlink_to(target)
    with pytest.raises((OSError, SandboxError)):
        credentials(alias)

    with pytest.raises(SandboxError):
        apply_refresh(auth_value("old", OLD_REFRESH, stamp(time.time()), jwt_token({"k": 1})),
                      {"access_token": "", "refresh_token": NEW_REFRESH})
    with pytest.raises(SandboxError):
        apply_refresh(auth_value("old", OLD_REFRESH, stamp(time.time()), jwt_token({"k": 1})),
                      {"access_token": "next", "id_token": "not-a-jwt"})
    good = apply_refresh(auth_value("old", OLD_REFRESH, stamp(time.time()), jwt_token({"k": 1})),
                         {"access_token": "next", "refresh_token": None, "id_token": jwt_token({"k": 2})})
    assert good["tokens"]["refresh_token"] == OLD_REFRESH and good["tokens"]["access_token"] == "next"
    path = tmp_path / "rewrite.json"
    install_auth(path, good)
    write_auth(path, good)
    info = path.stat()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        owned = real_fstat(fd)
    finally:
        os.close(fd)
    assert info.st_nlink == 1 and info.st_mode & 0o777 == 0o600 and info.st_uid == owned.st_uid == os.getuid()
    assert info.st_size <= MAX_CREDENTIAL

    monkeypatch.setattr("agentic_host.coding_relay.credentials",
                        lambda path: ("api.openai.com", "/v1/responses", {"Authorization": "Bearer fixture"}))
    seen = []

    def transport(host, route, method, body, headers, maximum=None):
        seen.append((host, route, maximum, body))
        if route == "/v1/responses":
            return 200, "application/json", b"{}"
        return 200, "text/plain", b"documentation"

    relay = Relay(tmp_path / "limits.sock", tmp_path / "auth.json", time.time() + 30, transport=transport)
    with pytest.raises(SandboxError):
        relay.request("POST", "/v1/responses", b"x" * (MAX_REQUEST + 1))
    tools = {"model": "gpt-6-astra", "tools": [{"type": "function"}, {"type": "custom"}, {"type": "local_shell"}],
             "store": True}
    relay.request("POST", "/v1/responses", json.dumps(tools).encode())
    assert json.loads(seen[-1][3])["store"] is False and seen[-1][2] == MAX_RESPONSE
    with pytest.raises(SandboxError):
        relay.request("POST", "/v1/responses", b'{"model":"gpt-6-astra","tools":[{"type":"mcp"}]}')
    huge = Relay(tmp_path / "fetch.sock", tmp_path / "auth.json", time.time() + 30,
                 transport=lambda *args, **kwargs: (200, "text/plain", b"z" * (FETCH_LIMIT + 1)))
    with pytest.raises(SandboxError):
        huge.request("GET", "/fetch?url=https://docs.python.org/3/", b"")
    status, _, raw = relay.request("GET", "/fetch?url=https://docs.python.org/3/library/", b"")
    assert status == 200 and raw == b"documentation" and seen[-1][0] == "docs.python.org" and seen[-1][2] == FETCH_LIMIT

    patch_chown(monkeypatch)
    relay = start_relay(tmp_path, monkeypatch, transport)
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(relay.path))
        client.sendall(b"POST /v1/responses HTTP/1.1\r\nTransfer-Encoding: chunked\r\nContent-Length: 2\r\n\r\n{}")
        data = recv_rest(client, timeout=2)
        assert data.startswith(b"HTTP/1.1 502") and data.count(b"HTTP/1.") == 1
        assert b'{"error":"coding_egress_rejected"}' in data and LEAK.encode() not in data
        client.close()
    finally:
        relay.close()

    def leaky(host, route, method, body, headers, maximum=None):
        raise SandboxError(LEAK)

    relay = start_relay(tmp_path, monkeypatch, leaky)
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(relay.path))
        client.sendall(http_request())
        data = recv_rest(client, timeout=2)
        assert b"502" in data and b'{"error":"coding_egress_rejected"}' in data and LEAK.encode() not in data
        assert data.count(b"HTTP/1.") == 1
        client.close()
    finally:
        relay.close()

    script = program({"mode": "development", "goal": "x", "questions": [], "model": ""})
    compile(script, "coding launcher", "exec")
    assert IDLE_TIMEOUT == 300 and "/relay.sock" in script and str(IDLE_TIMEOUT) in script
    assert "settimeout(50)" not in script and "auth.openai.com" not in script and "refresh_token" not in script
    assert "--dangerously-bypass-approvals-and-sandbox" in script and "--ephemeral" in script
    assert "SUPERVISOR_TOKEN" not in script and "requires_openai_auth" in script
    proxy_only = sandbox_proxy()
    compile(proxy_only + "raise SystemExit(0)\n", "proxy", "exec")


def test_refresh_uses_public_tls_on_the_fixed_token_host(tmp_path, monkeypatch):
    patch_owner(monkeypatch)
    path = tmp_path / "auth.json"
    install_auth(path, auth_value(jwt_token({"exp": int(time.time()) + 30}), OLD_REFRESH, stamp(time.time()),
                                  jwt_token({"kind": "id"})))
    seen = []

    def connect(self):
        seen.append((self.host, self.timeout))
        raise SandboxError("Private network destination rejected")

    monkeypatch.setattr(PublicTLS, "connect", connect)
    relay = Relay(tmp_path / "sock", path, time.time() + 30)
    with pytest.raises(SandboxError) as failure:
        relay.request("POST", "/v1/responses", REQUEST)
    assert seen and seen[0][0] == TOKEN_HOST and seen[0][1] <= IDLE_TIMEOUT
    # An unreachable token host is transient: the still valid token is tried, and no sign-in is demanded.
    assert [host for host, _ in seen] == [TOKEN_HOST, "chatgpt.com"]
    assert OLD_REFRESH not in str(failure.value) and not relay.auth_failed


THREAD_ID = "22222222-2222-4222-8222-222222222222"
SESSION_ID = "11111111-1111-4111-8111-111111111111"
PARENT_ID = "33333333-3333-4333-8333-333333333333"
WINDOW_ID = THREAD_ID + ":0"
TURN_META = '{"turn_id":"turn-1","request_kind":"turn"}'
CANONICAL_REQUEST = {
    "x-openai-internal-codex-responses-lite": "true",
    "originator": "codex_cli_rs",
    "x-codex-turn-state": "ts-1",
    "x-codex-beta-features": "analytics_plan_history,remote_compaction_v2",
    "session-id": SESSION_ID,
    "thread-id": THREAD_ID,
    "x-client-request-id": THREAD_ID,
    "x-codex-window-id": WINDOW_ID,
    "x-codex-parent-thread-id": PARENT_ID,
    "x-codex-turn-metadata": TURN_META,
    "x-openai-subagent": "review",
    "x-openai-memgen-request": "true",
    "x-codex-routing-hint": "model=gpt-6-astra",
}


def wire_headers(mapping):
    return b"".join(f"{name}: {value}\r\n".encode() for name, value in mapping.items())


def split_headers(raw):
    head = raw.split(b"\r\n\r\n", 1)[0].decode()
    found = {}
    for line in head.split("\r\n")[1:]:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        found.setdefault(name.lower(), value[1:] if value.startswith(" ") else value)
    return found


def live_auth(tmp_path, access):
    path = tmp_path / "auth.json"
    install_auth(path, auth_value(access, OLD_REFRESH, stamp(time.time()), jwt_token({"kind": "id"})))
    return path


def test_header_selectors_reject_smuggled_and_unknown_values():
    injected = "ts-1\r\nX-Injected: yes"
    assert select_request_headers([
        ("X-Codex-Turn-State", injected), ("X-Codex-Turn-State", "ts-1")], chatgpt=True) == {}
    assert select_request_headers({"x-codex-turn-state": "ts\x001"}, chatgpt=True) == {}
    assert select_request_headers({"originator": "bad\norigin"}, chatgpt=True) == {}
    assert select_request_headers([
        ("Originator", "codex_cli_rs"), ("originator", "other")], chatgpt=True) == {"originator": "codex_cli_rs"}
    assert select_request_headers([
        ("originator", None), ("originator", "codex_cli_rs")], chatgpt=True) == {}
    assert select_request_headers({"x-codex-installation-id": SESSION_ID}, chatgpt=True) == {}
    guardian = "guardian:" + SESSION_ID
    assert select_request_headers({"session-id": guardian}, chatgpt=False) == {"session-id": guardian}
    consolidated = "memory_consolidation:" + THREAD_ID
    assert select_request_headers({"session-id": consolidated}, chatgpt=True) == {"session-id": consolidated}
    assert select_request_headers({"session-id": "Guardian:" + SESSION_ID}, chatgpt=True) == {}
    hint = "model=gpt-6-astra;tier=priority"
    assert select_request_headers({"x-codex-routing-hint": hint}, chatgpt=True) == {"x-codex-routing-hint": hint}
    assert select_request_headers({"x-codex-routing-hint": hint}, chatgpt=False) == {}
    assert select_request_headers({"x-codex-routing-hint": "model="}, chatgpt=True) == {}
    assert select_request_headers({"x-codex-turn-metadata": "[]"}, chatgpt=True) == {}
    assert select_request_headers({"x-codex-turn-metadata": "x" * 8193}, chatgpt=True) == {}
    assert select_request_headers({"x-openai-internal-codex-responses-lite": "TRUE"}, chatgpt=True) == {}
    assert select_response_headers({
        "Set-Cookie": "a=b", "X-Codex-Turn-State": "ts-1", "X-Evil": "nope"}) == {"x-codex-turn-state": "ts-1"}
    assert select_response_headers({"X-Models-Etag": 'W/"abc"'}) == {"x-models-etag": 'W/"abc"'}
    assert select_response_headers({"x-codex-rate-limit-reached-type": "rate_limit_reached"}) == {
        "x-codex-rate-limit-reached-type": "rate_limit_reached"}
    assert select_response_headers({"x-codex-rate-limit-reached-type": "nope"}) == {}
    assert select_response_headers({"x-codex-credits-has-credits": "TRUE"}) == {"x-codex-credits-has-credits": "TRUE"}
    assert select_response_headers({"x-codex-credits-has-credits": "yes"}) == {}
    assert select_response_headers({"x-codex-primary-used-percent": "12.5"}) == {"x-codex-primary-used-percent": "12.5"}
    assert select_response_headers({"x-codex-primary-used-percent": "1e2"}) == {}
    assert select_response_headers({"x-codex-other-primary-used-percent": "1"}) == {
        "x-codex-other-primary-used-percent": "1"}
    assert select_response_headers({"x--primary-used-percent": "1"}) == {}
    assert select_response_headers({"x-error-json": "eyJ9", "x-openai-authorization-error": "no"}) == {}
    many = [(f"x-lim{index}-primary-used-percent", "1") for index in range(45)]
    assert len(select_response_headers(many)) == 40


def test_proxy_selector_matches_relay():
    namespace = {}
    source = sandbox_proxy(str(sock_path("rules")), port=0, idle=30)
    assert "refresh_token" not in source and "auth.openai.com" not in source
    assert "select_request_headers(self.headers,chatgpt=True)" in source
    exec(compile(source, "proxy-rules", "exec"), namespace)
    proxy = namespace["server"]
    request_cases = [
        (CANONICAL_REQUEST, True),
        (CANONICAL_REQUEST, False),
        ({"X-Codex-Turn-State": "ts-1\r\nX-Injected: yes", "Originator": "codex_cli_rs"}, True),
        ({"Authorization": "Bearer stolen", "Cookie": "a=b", "x-oai-attestation": "device"}, True),
        ({"x-codex-routing-hint": "model=gpt-6-astra", "originator": "Codex Desktop"}, False),
        ({"session-id": "guardian:" + SESSION_ID, "x-codex-beta-features": "Bad"}, True),
        ([(f"x-lim{index}-primary-used-percent", "1") for index in range(5)], True),
    ]
    response_cases = [
        {"X-Codex-Turn-State": "ts-1", "Set-Cookie": "a=b", "X-Models-Etag": 'W/"abc"'},
        {"x-codex-primary-used-percent": "12.5", "x-codex-primary-window-minutes": "60",
         "x-codex-credits-balance": "9.5", "x-evil": "no"},
        [(f"x-lim{index}-primary-used-percent", "1") for index in range(45)],
    ]
    try:
        for headers, chatgpt in request_cases:
            assert namespace["select_request_headers"](headers, chatgpt=chatgpt) == select_request_headers(
                headers, chatgpt=chatgpt)
        for headers in response_cases:
            assert namespace["select_response_headers"](headers) == select_response_headers(headers)
    finally:
        proxy.shutdown()
        proxy.server_close()


def test_upstream_user_agent_is_fixed_and_response_headers_are_filtered(monkeypatch):
    captured = {}

    class Fake:
        def __init__(self, host, timeout=None):
            self.sock = None
            self.status = 200

        def request(self, method, path, body=None, headers=None):
            captured["headers"] = dict(headers)

        def getresponse(self):
            return self

        def getheader(self, name, default=None):
            return "text/event-stream"

        def getheaders(self):
            return [("X-Codex-Turn-State", "ts-1"), ("Set-Cookie", "a=b"), ("X-Models-Etag", 'W/"abc"')]

        def read1(self, size):
            return b""

        def close(self):
            pass

    monkeypatch.setattr("agentic_host.coding_relay.PublicTLS", Fake)
    result = upstream("chatgpt.com", "/backend-api/codex/responses", "POST", b"{}",
                      {"User-Agent": "evil-agent", "Accept": "text/event-stream"})
    try:
        assert captured["headers"]["User-Agent"] == "agentic-home-coding/1"
        assert "evil-agent" not in captured["headers"].values()
        assert result.headers == {"x-codex-turn-state": "ts-1", "x-models-etag": 'W/"abc"'}
    finally:
        result.abort()


def test_codex_headers_reach_upstream_and_cannot_override_relay_auth(tmp_path, monkeypatch):
    patch_owner(monkeypatch)
    access = jwt_token({"exp": int(time.time()) + 30 * 86400})
    seen = []

    def transport(host, route, method, body, headers, maximum=None):
        seen.append((host, dict(headers), body))
        return (200, "text/event-stream", b"data: ok\n\n", {
            "X-Codex-Turn-State": "ts-1", "X-Codex-Primary-Used-Percent": "12.5",
            "X-Codex-Primary-Window-Minutes": "60", "X-Codex-Primary-Reset-At": "1704069000",
            "OpenAI-Model": "gpt-6-astra", "Set-Cookie": "session=secret", "X-Evil": "nope",
            "X-OpenAI-Authorization-Error": "account", "X-Error-Json": "eyJ9"})

    relay = start_relay(tmp_path, monkeypatch, transport, credential_path=live_auth(tmp_path, access))
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(relay.path))
        client.sendall(http_request(extra=wire_headers({
            "X-OpenAI-Internal-Codex-Responses-Lite": "true", "Originator": "codex_cli_rs",
            "X-Codex-Turn-State": "ts-1", "X-Codex-Beta-Features": "analytics_plan_history,remote_compaction_v2",
            "Session-Id": SESSION_ID, "Thread-Id": THREAD_ID, "X-Client-Request-Id": THREAD_ID,
            "X-Codex-Window-Id": WINDOW_ID, "X-Codex-Parent-Thread-Id": PARENT_ID,
            "X-Codex-Turn-Metadata": TURN_META, "X-OpenAI-Subagent": "review",
            "X-OpenAI-Memgen-Request": "true", "X-Codex-Routing-Hint": "model=gpt-6-astra",
            "Authorization": "Bearer stolen", "Cookie": "a=b", "ChatGPT-Account-Id": "evil",
            "OpenAI-Beta": "responses=evil", "User-Agent": "evil-agent", "Accept": "text/plain",
            "Content-Type": "text/plain", "X-Oai-Attestation": "device-secret",
            "X-Not-A-Codex-Header": "nope", "X-Api-Key": "sk-injected",
            "X-Codex-Installation-Id": SESSION_ID,
        })))
        data = recv_rest(client, timeout=2)
        client.close()
    finally:
        relay.close()
    host, headers, body = seen[0]
    assert host == "chatgpt.com" and json.loads(body)["store"] is False
    for name, value in CANONICAL_REQUEST.items():
        assert headers[name] == value
    assert headers["Authorization"] == "Bearer " + access
    assert headers["ChatGPT-Account-Id"] == "acct-1"
    assert headers["OpenAI-Beta"] == "responses=experimental"
    assert headers["Content-Type"] == "application/json" and headers["Accept"] == "text/event-stream"
    forwarded = {key.lower(): value for key, value in headers.items()}
    assert "stolen" not in forwarded["authorization"] and forwarded["chatgpt-account-id"] == "acct-1"
    assert "user-agent" not in forwarded and "x-oai-attestation" not in forwarded
    assert "x-codex-installation-id" not in forwarded and "x-not-a-codex-header" not in forwarded
    assert "sk-injected" not in " ".join(headers.values()) and "device-secret" not in " ".join(headers.values())
    returned = split_headers(data)
    assert returned["x-codex-turn-state"] == "ts-1"
    assert returned["x-codex-primary-used-percent"] == "12.5"
    assert returned["x-codex-primary-window-minutes"] == "60"
    assert returned["x-codex-primary-reset-at"] == "1704069000"
    assert returned["openai-model"] == "gpt-6-astra"
    lowered = data.lower()
    assert b"set-cookie" not in lowered and b"session=secret" not in data
    assert b"x-evil" not in lowered and b"x-error-json" not in lowered and b"authorization-error" not in lowered
    assert data.startswith(b"HTTP/1.1 200") and b"data: ok" in data


def test_invalid_headers_are_dropped_without_failing_the_request(tmp_path, monkeypatch):
    patch_owner(monkeypatch)
    access = jwt_token({"exp": int(time.time()) + 30 * 86400})
    seen = []

    def transport(host, route, method, body, headers, maximum=None):
        seen.append(dict(headers))
        return 200, "text/event-stream", b"data: ok\n\n"

    relay = start_relay(tmp_path, monkeypatch, transport, credential_path=live_auth(tmp_path, access))
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(relay.path))
        client.sendall(http_request(extra=wire_headers({
            "Originator": "codex_cli_rs", "X-Codex-Turn-State": "t" * 2000,
            "X-OpenAI-Internal-Codex-Responses-Lite": "TRUE", "X-Codex-Beta-Features": "Bad-Feature",
            "Authorization": "Bearer stolen", "Cookie": "a=b", "X-Oai-Attestation": "device-secret",
            "X-Unknown-Header": "nope",
        })))
        data = recv_rest(client, timeout=2)
        client.close()
    finally:
        relay.close()
    headers = {key.lower(): value for key, value in seen[0].items()}
    assert headers["originator"] == "codex_cli_rs"
    assert "x-codex-turn-state" not in headers and "x-openai-internal-codex-responses-lite" not in headers
    assert "x-codex-beta-features" not in headers and "x-oai-attestation" not in headers
    assert "cookie" not in headers and headers["authorization"] == "Bearer " + access
    assert data.startswith(b"HTTP/1.1 200") and b"device-secret" not in data and b"stolen" not in data


def test_refresh_retry_repeats_forwarded_headers(tmp_path, monkeypatch):
    patch_owner(monkeypatch)
    access = jwt_token({"exp": int(time.time()) + 30 * 86400})
    path = live_auth(tmp_path, access)
    new_access = jwt_token({"exp": int(time.time()) + 30 * 86400, "kind": "access"})
    new_id = jwt_token({"exp": int(time.time()) + 30 * 86400, "kind": "id"})
    token_reply = (200, "application/json", json.dumps(
        {"access_token": new_access, "id_token": new_id, "refresh_token": NEW_REFRESH}).encode())
    seen = []

    def transport(host, route, method, body, headers, maximum=None):
        seen.append((host, dict(headers)))
        if host == TOKEN_HOST:
            return token_reply
        if headers.get("Authorization") == "Bearer " + access:
            return 401, "application/json", b'{"error":"expired"}'
        return (200, "text/event-stream", b"data: retried\n\n",
                {"x-codex-turn-state": "ts-2", "Set-Cookie": "session=secret"})

    relay = start_relay(tmp_path, monkeypatch, transport, credential_path=path)
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(relay.path))
        client.sendall(http_request(extra=wire_headers({
            "X-OpenAI-Internal-Codex-Responses-Lite": "true", "Originator": "codex_cli_rs",
            "X-Codex-Turn-State": "ts-1", "X-Codex-Routing-Hint": "model=gpt-6-astra",
            "Authorization": "Bearer stolen", "X-Oai-Attestation": "device-secret",
        })))
        data = recv_rest(client, timeout=2)
        client.close()
    finally:
        relay.close()
    model = [headers for host, headers in seen if host == "chatgpt.com"]
    assert [host for host, _headers in seen] == ["chatgpt.com", TOKEN_HOST, "chatgpt.com"]
    assert len(model) == 2
    for headers in model:
        assert headers["originator"] == "codex_cli_rs" and headers["x-codex-turn-state"] == "ts-1"
        assert headers["x-openai-internal-codex-responses-lite"] == "true"
        assert headers["x-codex-routing-hint"] == "model=gpt-6-astra"
        assert "x-oai-attestation" not in {key.lower() for key in headers}
        assert "stolen" not in headers["Authorization"] and headers["ChatGPT-Account-Id"] == "acct-1"
    assert model[0]["Authorization"] == "Bearer " + access
    assert model[1]["Authorization"] == "Bearer " + new_access
    token = [headers for host, headers in seen if host == TOKEN_HOST]
    assert token and "originator" not in token[0] and "x-codex-turn-state" not in token[0]
    assert split_headers(data)["x-codex-turn-state"] == "ts-2"
    assert b"set-cookie" not in data.lower() and b"session=secret" not in data


def test_proactive_refresh_keeps_forwarded_headers(tmp_path, monkeypatch):
    patch_owner(monkeypatch)
    soon = jwt_token({"exp": int(time.time()) + 60})
    path = live_auth(tmp_path, soon)
    new_access = jwt_token({"exp": int(time.time()) + 30 * 86400, "kind": "access"})
    new_id = jwt_token({"exp": int(time.time()) + 30 * 86400, "kind": "id"})
    seen = []

    def transport(host, route, method, body, headers, maximum=None):
        seen.append((host, dict(headers)))
        if host == TOKEN_HOST:
            return 200, "application/json", json.dumps(
                {"access_token": new_access, "id_token": new_id, "refresh_token": NEW_REFRESH}).encode()
        return 200, "text/event-stream", b"data: ok\n\n"

    relay = Relay(tmp_path / "unused.sock", path, time.time() + 30, transport=transport)
    relay._bind_client_headers({
        "originator": "codex_cli_rs", "x-codex-routing-hint": "model=gpt-6-astra",
        "Authorization": "Bearer stolen"})
    try:
        status, _, raw = relay.request("POST", "/v1/responses", REQUEST)
    finally:
        relay._clear_client_headers()
    assert status == 200 and raw == b"data: ok\n\n"
    assert seen[0][0] == TOKEN_HOST and "originator" not in seen[0][1]
    assert seen[1][0] == "chatgpt.com"
    assert seen[1][1]["originator"] == "codex_cli_rs"
    assert seen[1][1]["x-codex-routing-hint"] == "model=gpt-6-astra"
    assert seen[1][1]["Authorization"] == "Bearer " + new_access


def test_api_key_path_forwards_only_the_shared_subset(tmp_path, monkeypatch):
    patch_owner(monkeypatch)
    path = tmp_path / "key.json"
    install_auth(path, auth_value(jwt_token({"exp": int(time.time()) + 60}), OLD_REFRESH, stamp(time.time()),
                                  jwt_token({"kind": "id"}), OPENAI_API_KEY="sk-test-key"))
    seen = []

    def transport(host, route, method, body, headers, maximum=None):
        seen.append((host, route, dict(headers)))
        return 200, "text/event-stream", b"data: ok\n\n"

    relay = start_relay(tmp_path, monkeypatch, transport, credential_path=path)
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(relay.path))
        client.sendall(http_request(extra=wire_headers({
            "X-OpenAI-Internal-Codex-Responses-Lite": "true", "Originator": "codex_cli_rs",
            "X-Codex-Turn-State": "ts-1", "Session-Id": SESSION_ID, "Thread-Id": THREAD_ID,
            "X-Codex-Routing-Hint": "model=gpt-6-astra", "X-Oai-Attestation": "device-secret",
            "Authorization": "Bearer stolen", "ChatGPT-Account-Id": "evil",
            "OpenAI-Beta": "responses=evil",
        })))
        data = recv_rest(client, timeout=2)
        client.close()
    finally:
        relay.close()
    assert len(seen) == 1 and seen[0][0] == "api.openai.com" and seen[0][1] == "/v1/responses"
    headers = seen[0][2]
    assert headers["Authorization"] == "Bearer sk-test-key"
    assert headers["x-openai-internal-codex-responses-lite"] == "true"
    assert headers["originator"] == "codex_cli_rs" and headers["x-codex-turn-state"] == "ts-1"
    assert headers["session-id"] == SESSION_ID and headers["thread-id"] == THREAD_ID
    lowered = {key.lower() for key in headers}
    assert "x-codex-routing-hint" not in lowered and "x-oai-attestation" not in lowered
    assert "openai-beta" not in lowered and "chatgpt-account-id" not in lowered
    assert "stolen" not in headers["Authorization"] and data.startswith(b"HTTP/1.1 200")


def test_fetch_ignores_sandbox_model_headers(tmp_path, monkeypatch):
    seen = []

    def transport(host, route, method, body, headers, maximum=None):
        seen.append((host, dict(headers), body))
        return 200, "text/plain", b"documentation"

    relay = start_relay(tmp_path, monkeypatch, transport)
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(relay.path))
        client.sendall(b"GET /fetch?url=https://docs.python.org/3/ HTTP/1.1\r\nHost: localhost\r\n"
                       b"Originator: codex_cli_rs\r\nAuthorization: Bearer stolen\r\nContent-Length: 0\r\n\r\n")
        data = recv_rest(client, timeout=2)
        client.close()
    finally:
        relay.close()
    assert seen[0][0] == "docs.python.org"
    assert seen[0][1] == {"Accept": "text/plain,application/json,text/html"}
    assert seen[0][2] is None and b"documentation" in data and b"stolen" not in data


def test_sandbox_proxy_forwards_allowlisted_headers_both_ways(tmp_path):
    path = sock_path("proxy-upstream")
    ready, seen = threading.Event(), []

    def upstream_server():
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen(1)
        ready.set()
        server.settimeout(5)
        conn, _ = server.accept()
        try:
            conn.settimeout(2)
            seen.append(recv_request(conn))
            body = b"data: ok\n\n"
            head = (b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    b"X-Codex-Turn-State: ts-1\r\nSet-Cookie: session=secret\r\nX-Evil: nope\r\nContent-Length: "
                    + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n")
            conn.sendall(head + body)
        finally:
            conn.close()
            server.close()

    thread = threading.Thread(target=upstream_server, daemon=True)
    thread.start()
    assert ready.wait(2)
    namespace = {}
    exec(compile(sandbox_proxy(str(path), port=0, idle=30), "proxy-headers", "exec"), namespace)
    proxy = namespace["server"]
    try:
        client = socket.create_connection(("127.0.0.1", proxy.server_address[1]), timeout=2)
        client.sendall(http_request(extra=wire_headers({
            "Originator": "codex_cli_rs", "X-OpenAI-Internal-Codex-Responses-Lite": "true",
            "X-Codex-Turn-State": "ts-1", "X-Codex-Routing-Hint": "model=gpt-6-astra",
            "Session-Id": SESSION_ID, "Authorization": "Bearer " + LEAK, "Cookie": "a=b",
            "X-Oai-Attestation": "device-secret", "X-Unknown": "nope",
        })))
        data = recv_rest(client, timeout=2)
        client.close()
        blocked = socket.create_connection(("127.0.0.1", proxy.server_address[1]), timeout=2)
        blocked.sendall(b"POST /v1/responses HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n"
                        b"Content-Length: 2\r\n\r\n{}")
        rejected = recv_rest(blocked, timeout=2)
        blocked.close()
    finally:
        proxy.shutdown()
        proxy.server_close()
        thread.join(2)
    request = seen[0]
    assert b"originator: codex_cli_rs" in request.lower()
    assert b"x-openai-internal-codex-responses-lite: true" in request.lower()
    assert b"x-codex-turn-state: ts-1" in request.lower()
    assert b"x-codex-routing-hint: model=gpt-6-astra" in request.lower()
    assert b"session-id: " + SESSION_ID.encode() in request.lower()
    assert b"authorization" not in request.lower() and LEAK.encode() not in request
    assert b"cookie" not in request.lower() and b"device-secret" not in request and b"x-unknown" not in request.lower()
    returned = split_headers(data)
    assert returned["x-codex-turn-state"] == "ts-1"
    assert b"set-cookie" not in data.lower() and b"session=secret" not in data and b"x-evil" not in data.lower()
    assert b"chunked" in data.lower() and b"data: ok" in data
    assert b"413" in rejected and b"HTTP/1." in rejected


def _chatgpt_relay(tmp_path, monkeypatch, replies):
    patch_owner(monkeypatch)
    path = tmp_path / "auth.json"
    live_far = jwt_token({"exp": int(time.time()) + 30 * 86400})
    install_auth(path, auth_value(live_far, OLD_REFRESH, stamp(time.time()), jwt_token({"kind": "id"})))
    pending = list(replies)

    def transport(host, route, method, body, headers, maximum=None):
        return pending.pop(0)

    return Relay(tmp_path / "flags.sock", path, time.time() + 30, transport=transport)


def _token_reply():
    new_access = jwt_token({"exp": int(time.time()) + 30 * 86400, "kind": "access"})
    new_id = jwt_token({"exp": int(time.time()) + 30 * 86400, "kind": "id-token"})
    return (200, "application/json", json.dumps({"access_token": new_access, "id_token": new_id,
            "refresh_token": NEW_REFRESH, "token_type": "bearer"}).encode())


@pytest.mark.parametrize("status,flag", [
    (429, "quota_reached"),
    (400, "provider_rejected"),
    (404, "provider_rejected"),
    (422, "provider_rejected"),
    (503, "provider_unavailable"),
    (403, "auth_failed"),
])
def test_final_provider_status_sets_one_flag(tmp_path, monkeypatch, status, flag):
    body = b'{"error":"fixture-status","code":' + str(status).encode() + b'}'
    relay = _chatgpt_relay(tmp_path, monkeypatch, [(status, "application/json", body)])
    assert relay.quota_reached is relay.provider_rejected is relay.provider_unavailable is False
    assert relay.turns_exhausted is relay.auth_failed is False
    got, content_type, raw = relay.request("POST", "/v1/responses", REQUEST)
    assert got == status and content_type == "application/json" and raw == body
    for name in ("quota_reached", "provider_rejected", "provider_unavailable", "turns_exhausted", "auth_failed"):
        assert getattr(relay, name) is (name == flag)


def test_successful_refresh_leaves_provider_flags_clear(tmp_path, monkeypatch):
    relay = _chatgpt_relay(tmp_path, monkeypatch, [
        (401, "application/json", b'{"error":"expired"}'), _token_reply(),
        (200, "text/event-stream", b"data: refreshed\n\n")])
    status, _, raw = relay.request("POST", "/v1/responses", REQUEST)
    assert status == 200 and raw == b"data: refreshed\n\n"
    assert relay.auth_failed is relay.quota_reached is relay.provider_rejected is False
    assert relay.provider_unavailable is relay.turns_exhausted is False


def test_final_401_after_refresh_is_only_reauth(tmp_path, monkeypatch):
    body = b'{"error":"still-expired"}'
    relay = _chatgpt_relay(tmp_path, monkeypatch, [
        (401, "application/json", b'{"error":"expired"}'), _token_reply(),
        (401, "application/json", body)])
    status, _, raw = relay.request("POST", "/v1/responses", REQUEST)
    assert status == 401 and raw == body
    assert relay.auth_failed is True
    assert relay.quota_reached is relay.provider_rejected is relay.provider_unavailable is relay.turns_exhausted is False


def test_call_cap_sets_turns_exhausted_and_keeps_the_message(tmp_path, monkeypatch):
    relay = _chatgpt_relay(tmp_path, monkeypatch, [(200, "application/json", b"{}")] * (MAX_CALLS + 2))
    relay.calls = MAX_CALLS - 1
    status, _, raw = relay.request("POST", "/v1/responses", REQUEST)
    assert status == 200 and raw == b"{}" and relay.calls == MAX_CALLS and relay.turns_exhausted is False
    with pytest.raises(SandboxError, match="Relay authorization expired or exhausted"):
        relay.request("POST", "/v1/responses", REQUEST)
    assert relay.turns_exhausted is True and relay.calls == MAX_CALLS
    expired = Relay(tmp_path / "expired.sock", tmp_path / "missing.json", time.time() - 1)
    with pytest.raises(SandboxError, match="Relay authorization expired or exhausted"):
        expired.request("POST", "/v1/responses", REQUEST)
    assert expired.turns_exhausted is False and expired.calls == 0
