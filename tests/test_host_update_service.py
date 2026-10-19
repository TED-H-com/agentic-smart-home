"""Bounded immutable transport; disk socket fixtures, never the system listener."""
import http.client
import json
import socket
import sys
import threading
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "host"))
from agentic_host import update_service as service
from agentic_host.manager import HostError


@contextmanager
def running(tmp_path, monkeypatch, bundle):
    monkeypatch.setattr(service, "secure", lambda path: None)
    monkeypatch.setattr(service.os, "chown", lambda *args: None)
    monkeypatch.setattr(service, "peer_uid", lambda connection: 1000)
    path = tmp_path / "api.sock"
    server = service.create_server(path, bundle)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield path, server
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def request(path, method="GET", route="/host-updates", body=None, headers=None):
    headers = dict(headers or {})
    payload = body.encode() if body is not None else b""
    if body is not None and "Content-Length" not in headers:
        headers["Content-Length"] = str(len(payload))
    head = f"{method} {route} HTTP/1.0\r\n" + "".join(f"{key}: {value}\r\n" for key, value in headers.items()) + "\r\n"
    with closing(socket.socket(socket.AF_UNIX)) as connection:
        connection.settimeout(3)
        connection.connect(str(path))
        # A single send also covers rejected headers: the server is allowed to
        # close immediately without waiting for an invalid request's body.
        connection.sendall(head.encode() + payload)
        response = http.client.HTTPResponse(connection)
        response.begin()
        return response.status, json.loads(response.read())


def test_owner_peer_and_exact_routes_only(tmp_path, monkeypatch):
    calls = []
    bundle = SimpleNamespace(handle=lambda *args: (calls.append(args) or (200, {"ok": True})))
    with running(tmp_path, monkeypatch, bundle) as (path, server):
        assert request(path) == (200, {"ok": True})
        assert request(path, "POST", "/host-updates/check", "{}") == (200, {"ok": True})
        assert request(path, "POST", "/host-updates/cancel-offer", "{}") == (200, {"ok": True})
        for route in ("/status", "/actions", "/host-updates/../actions", "/host-updates?x=1", "/host-updates/" + "A" * 64):
            assert request(path, route=route)[0] == 404
        assert len(calls) == 3
        monkeypatch.setattr(service, "peer_uid", lambda connection: 0)
        assert request(path)[0] == 403
        assert request(path, "POST", "/host-updates/apply", "{}")[0] == 403
        assert len(calls) == 3
        assert server.request_queue_size == service.MAX_CONNECTIONS


@pytest.mark.parametrize("body,headers,status", [
    ("[]", {}, 400), ("{", {}, 400), ("{}", {"Transfer-Encoding": "chunked"}, 400),
    ("{}", {"Content-Length": "131073"}, 413), ("{}", {"Content-Length": "-1"}, 400),
])
def test_malformed_or_oversized_bodies_never_dispatch(tmp_path, monkeypatch, body, headers, status):
    calls = []
    with running(tmp_path, monkeypatch, SimpleNamespace(handle=lambda *args: calls.append(args))) as (path, _):
        assert request(path, "POST", "/host-updates/stage", body, headers)[0] == status
        assert calls == []


def test_connection_threads_are_bounded_and_recover_after_stalled_peers(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "READ_TIMEOUT", 0.1)
    with running(tmp_path, monkeypatch, SimpleNamespace(handle=lambda *args: (200, {}))) as (path, server):
        peers = []
        try:
            for _ in range(service.MAX_CONNECTIONS):
                peer = socket.socket(socket.AF_UNIX)
                peer.connect(str(path))
                peers.append(peer)
            # Wait for every peer to be accepted using the bounded semaphore,
            # then assert another connection cannot acquire a handler slot.
            for _ in range(100):
                if server.slots._value == 0:
                    break
                threading.Event().wait(0.001)
            assert server.slots._value == 0
        finally:
            for peer in peers:
                peer.close()
        for _ in range(100):
            if server.slots._value == service.MAX_CONNECTIONS:
                break
            threading.Event().wait(0.005)
        assert request(path)[0] == 200


def test_existing_live_listener_and_non_socket_are_never_unlinked(tmp_path, monkeypatch):
    with running(tmp_path, monkeypatch, SimpleNamespace(handle=lambda *args: (200, {}))) as (path, _):
        # Test-owned inode stands in for root-installed listener ownership.
        real_lstat = Path.lstat
        def root_lstat(item):
            info = real_lstat(item)
            return SimpleNamespace(st_mode=info.st_mode, st_uid=0)
        monkeypatch.setattr(Path, "lstat", root_lstat)
        with pytest.raises(HostError, match="already_running"):
            service.create_server(path, None)
        assert request(path)[0] == 200
    path.unlink()
    path.write_text("do not replace")
    with pytest.raises(HostError, match="socket_untrusted"):
        service.create_server(path, None)
    assert path.read_text() == "do not replace"


def test_unit_executes_only_immutable_engine():
    unit = (Path(__file__).parents[1] / "host/systemd/agentic-home-update-api.service").read_text()
    assert "PYTHONPATH=/usr/lib/agentic-home-update/current" in unit
    assert "-m agentic_host.update_service" in unit
    assert "RuntimeDirectoryPreserve=yes" in unit
    assert "ConditionPathExists=/usr/lib/agentic-home-update/current/agentic_host/update_service.py" in unit
    assert "/opt/agentic-home" not in unit
