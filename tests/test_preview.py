import base64
import copy
import hashlib
import http.client
import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
import tomllib
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from agentic_host.fence import fence
from agentic_host.manager import HostError
from agentic_host.preview import WIDTHS, Previews, canonical, png, preview_server, validate


def artifact(files=None):
    files = files or {"index.html": b"<h1>Candidate</h1>", "style.css": b"h1{color:navy}"}
    entries = [{"path": name, "sha256": hashlib.sha256(data).hexdigest(), "content_base64": base64.b64encode(data).decode()}
               for name, data in sorted(files.items())]
    digest = hashlib.sha256(canonical([{k: item[k] for k in ("path", "sha256")} for item in entries])).hexdigest()
    return {"id": "fixture-preview", "artifact_id": "fixture-artifact", "artifact_digest": digest, "files": entries}


@pytest.mark.parametrize("path", ["../index.html", "/index.html", "x/../index.html", ".secret.html", "x//index.html", "index.exe", "x\\index.html"])
def test_preview_rejects_paths_and_executable_types(path):
    with pytest.raises(HostError):
        validate(artifact({path: b"<h1>Candidate</h1>"}))


def test_preview_enforces_bound_content_and_full_manifest_digest():
    body = artifact()
    assert len(validate(body)) == 2
    bad = copy.deepcopy(body)
    bad["files"][0]["content_base64"] = base64.b64encode(b"changed").decode()
    with pytest.raises(HostError, match="file_digest"):
        validate(bad)
    bad = copy.deepcopy(body)
    bad["artifact_digest"] = "0" * 64
    with pytest.raises(HostError, match="artifact_digest"):
        validate(bad)
    with pytest.raises(HostError, match="byte_limit"):
        validate(artifact({"index.html": b"x" * (4 * 1024 * 1024 + 1)}))
    with pytest.raises(HostError, match="file_limit"):
        validate(artifact({f"{i}.html": b"x" for i in range(65)}))


def test_preview_server_requires_separate_host_capability_and_expiry(tmp_path):
    service = Previews(SimpleNamespace(root=tmp_path, run=None))
    cap = "a" * 43
    data = b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + struct.pack(">II", 390, 844) + bytes(9)
    (tmp_path / "previews/fixture-preview").mkdir()
    (tmp_path / "previews/fixture-preview/390.png").write_bytes(data)
    record = {"id": "fixture-preview", "capability": cap, "state": "ready", "expires_at": time.time() + 30,
              "image_digests": {"390": png(data, 390)}}
    service.records[record["id"]] = record
    server = preview_server(service, ("127.0.0.2", 0))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        def request(path, host=None):
            connection = http.client.HTTPConnection("127.0.0.2", server.server_port, timeout=3)
            connection.request("GET", path, headers={"Host": host or f"127.0.0.2:{server.server_port}", "Cookie": "agentic_host=ignored"})
            response = connection.getresponse()
            value = response.status, dict(response.getheaders()), response.read()
            connection.close()
            return value
        status, headers, content = request(f"/p/{cap}/390.png")
        assert status == 200 and content == data
        assert headers["Content-Type"] == "image/png" and headers["Cache-Control"] == "no-store"
        assert "sandbox" in headers["Content-Security-Policy"]
        assert "Set-Cookie" not in headers and "Access-Control-Allow-Origin" not in headers
        for path in ("/p/fixture-preview/390.png", f"/p/{cap}/index.html", f"/p/{cap}/390.png?token=anything"):
            assert request(path)[0] == 404
        assert request(f"/p/{cap}/390.png", "localhost:8099")[0] == 404
        record["expires_at"] = time.time() - 1
        assert request(f"/p/{cap}/390.png")[0] == 404
    finally:
        server.shutdown()
        server.server_close()


def test_restarted_preview_requires_fencing_before_releasing_disk(tmp_path):
    service = Previews(SimpleNamespace(root=tmp_path, run=None))
    record = {"id": "fixture-preview", "capability": "a" * 43, "state": "running", "expires_at": time.time() + 30,
              "container": "agentic-preview-" + "b" * 32}
    service.records[record["id"]] = record
    service.update(record)
    closed = []
    class Volume:
        def __init__(self, *args):
            pass
        def close(self):
            closed.append(True)
    restarted = Previews(SimpleNamespace(root=tmp_path, run=None), volume=Volume)
    assert restarted.records[record["id"]]["state"] == "stop_unconfirmed"
    def uncertain(name):
        raise HostError("container_stop_unconfirmed")
    restarted.fenced = uncertain
    with pytest.raises(HostError, match="stop_unconfirmed"):
        restarted.revoke(record["id"])
    assert not closed
    restarted.fenced = lambda name: None
    assert restarted.revoke(record["id"])["state"] == "expired" and closed


def test_fence_never_kills_an_unowned_or_unconfirmed_container():
    calls = []
    def run(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=json.dumps([{"Id": "c" * 64, "Config": {"Labels": {}}, "State": {"Running": True}}]))
    with pytest.raises(HostError, match="ownership_unconfirmed"):
        fence(run, "agentic-preview-" + "a" * 32, "agentic.preview")
    assert not any("kill" in call or "rm" in call for call in calls)


def test_broker_umask_still_allows_renderer_to_read_nested_assets(tmp_path, monkeypatch):
    work = tmp_path / "volume"
    work.mkdir()
    class Volume:
        def __init__(self, *args):
            pass
        def create(self):
            return work
        def close(self):
            pass
    def execute(*args, **kwargs):
        assert args[:2] == ("docker", "run")
        for relative in ("input", "input/assets", "input/assets/css"):
            assert (work / relative).stat().st_mode & 0o777 == 0o755
        assert (work / "input/assets/css/style.css").stat().st_mode & 0o777 == 0o444
        for width, height in WIDTHS.items():
            (work / "work" / f"{width}.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + struct.pack(">II", width, height) + bytes(9))
    service = Previews(SimpleNamespace(root=tmp_path, run=execute), volume=Volume)
    service.fenced = lambda _: None
    monkeypatch.setattr("agentic_host.preview.os.chown", lambda *args: None)
    record = {"id": "fixture-preview", "image_id": "sha256:" + "1" * 64, "state": "queued"}
    service.records[record["id"]] = record
    original = os.umask(0o077)
    try:
        service.render(record, {"index.html": b"<h1>Preview</h1>", "assets/css/style.css": b"h1{color:red}"})
    finally:
        os.umask(original)
    assert record["state"] == "ready"


@pytest.mark.parametrize("render_fails", [False, True])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_renderer_slot_stays_reserved_until_cleanup(tmp_path, monkeypatch, render_fails, cleanup_fails):
    work = tmp_path / "volume"
    work.mkdir()
    cleaning, release = threading.Event(), threading.Event()

    class Volume:
        def __init__(self, *args):
            pass

        def create(self):
            return work

        def close(self):
            cleaning.set()
            assert release.wait(60), "test cleanup release was not signalled"
            if cleanup_fails:
                raise HostError("volume_cleanup_unconfirmed")

    def execute(*args, **kwargs):
        if render_fails:
            raise HostError("renderer_failed")
        for width, height in WIDTHS.items():
            (work / "work" / f"{width}.png").write_bytes(
                b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + struct.pack(">II", width, height) + bytes(9))

    service = Previews(SimpleNamespace(root=tmp_path, run=execute), volume=Volume)
    service.fenced = lambda _: None
    service.image = lambda: "sha256:" + "1" * 64
    monkeypatch.setattr("agentic_host.preview.ready", lambda *args: None)
    monkeypatch.setattr("agentic_host.preview.os.chown", lambda *args: None)
    record = {**{k: v for k, v in artifact().items() if k != "files"},
              "state": "queued", "image_id": service.image(), "expires_at": time.time() + 30}
    service.records[record["id"]] = record
    worker = threading.Thread(target=service.render, args=(record, validate(artifact())))
    worker.start()
    try:
        assert cleaning.wait(60), "renderer did not reach cleanup"
        assert service.submit(artifact())["state"] == "running"
        assert json.loads(service.path.read_text())[record["id"]]["state"] == "running"
        with pytest.raises(HostError, match="preview_busy_or_unfenced"):
            service.submit({**artifact(), "id": "second-preview"})
        with pytest.raises(HostError, match="preview_render_in_progress"):
            service.revoke(record["id"])
    finally:
        release.set()
        worker.join(60)
    assert not worker.is_alive()
    expected = "stop_unconfirmed" if cleanup_fails else "failed" if render_fails else "ready"
    assert record["state"] == expected
    assert json.loads(service.path.read_text())[record["id"]]["state"] == expected
    if cleanup_fails:
        with pytest.raises(HostError, match="preview_busy_or_unfenced"):
            service.submit({**artifact(), "id": "second-preview"})
    else:
        # A completed cleanup releases the slot; don't launch a second renderer.
        monkeypatch.setattr("agentic_host.preview.threading.Thread.start", lambda self: None)
        assert service.submit({**artifact(), "id": "second-preview"})["state"] == "queued"


def test_actual_offline_preview_renderer(tmp_path, request):
    if not request.config.getoption("--containers"):
        pytest.skip("requires packaged offline preview image")
    root = Path(__file__).resolve().parents[1]
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    tag = "agentic-home-preview:" + version
    image = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", tag], check=True, capture_output=True, text=True).stdout.strip()
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", image)
    source, scratch = tmp_path / "input", tmp_path / "work"
    source.mkdir(mode=0o755)
    scratch.mkdir(mode=0o777)
    scratch.chmod(0o777)
    calls = []
    class Receiver(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            calls.append(self.path)
            self.send_response(200)
            self.end_headers()
    receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    threading.Thread(target=receiver.serve_forever, daemon=True).start()
    (source / "index.html").write_text(f"""<!doctype html><meta charset=utf-8><style>body{{font:24px sans-serif;background:#edf2f6;color:#122335;padding:32px}}h1{{font-size:36px}}</style><h1>Candidate preview</h1><p>Isolated artifact. No household data.</p><p id=result>Checking isolation</p><script>Promise.allSettled([fetch('http://127.0.0.1:{receiver.server_port}/should-never-arrive'),fetch('http://192.168.177.2:80/api/'),fetch('https://example.com/')]).then(()=>document.querySelector('#result').textContent='Offline render complete');</script>""")
    name = "agentic-preview-test-" + uuid.uuid4().hex
    try:
        subprocess.run(["docker", "run", "--name", name, "--network", "none", "--read-only", "--cap-drop", "ALL",
                        "--security-opt", "no-new-privileges:true", "--user", "10004:10004", "--memory", "768m", "--memory-swap", "768m",
                        "--pids-limit", "128", "--cpus", "1", "--ipc", "none", "--log-driver", "none",
                        "--mount", f"type=bind,src={source},dst=/input,readonly", "--mount", f"type=bind,src={scratch},dst=/work",
                        "--mount", f"type=bind,src={scratch},dst=/tmp", image], check=True, capture_output=True, timeout=60)
        inspection = json.loads(subprocess.run(["docker", "inspect", name], check=True, capture_output=True, text=True).stdout)[0]
        assert inspection["HostConfig"]["NetworkMode"] == "none"
        assert inspection["HostConfig"]["ReadonlyRootfs"] is True
        assert inspection["Config"]["User"] == "10004:10004"
        assert not calls
        hashes = {str(width): png((scratch / f"{width}.png").read_bytes(), width) for width in WIDTHS}
        evidence = root / "evidence/ui"
        evidence.mkdir(exist_ok=True)
        for width in WIDTHS:
            (evidence / f"offline-preview-{width}.png").write_bytes((scratch / f"{width}.png").read_bytes())
        (root / "evidence/preview-renderer.json").write_text(json.dumps({"image_id": image, "network": "none", "uid": 10004,
            "memory_bytes": inspection["HostConfig"]["Memory"], "pids": inspection["HostConfig"]["PidsLimit"], "cpu_nano": inspection["HostConfig"]["NanoCpus"],
            "screenshots": hashes, "loopback_management_requests": len(calls), "separate_origin_http_test": "passed",
            "disk_quota": "separate DiskVolume tests; this render test uses disposable host disk directory",
            "real_cloud": False, "real_household": False}, indent=2) + "\n")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=120)
        # Chromium caches use the renderer UID. Repair only this disposable
        # directory before pytest-disktmp removes it; no leftover root/UID files.
        subprocess.run(["docker", "run", "--rm", "--network", "none", "--user", "0:0", "--entrypoint", "python",
                        "--mount", f"type=bind,src={scratch},dst=/cleanup", image, "-c",
                        "import os; from pathlib import Path; p=Path('/cleanup'); "
                        f"[(os.chown(x,{os.getuid()},{os.getgid()}),os.chmod(x,0o700) if x.is_dir() else None) for x in [p,*p.rglob('*')]]"],
                       check=True, capture_output=True, timeout=120)
        receiver.shutdown()
        receiver.server_close()
