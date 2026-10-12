"""Local desktop handoff never exposes the ticket through process argv."""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from agentic_host.desktop import issue_ticket, open_desktop  # noqa: E402


def test_desktop_ticket_private_hashed_rotating_and_fragment_only(tmp_path):
    if os.geteuid() != 1000:
        pytest.skip("Installed desktop handoff belongs to UID 1000")
    root = tmp_path / "controller"
    root.mkdir(mode=0o700)
    first = issue_ticket(root)
    calls = []

    def opener(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    cleanups = []
    launcher = tmp_path / "launcher"
    open_desktop(root, opener, launcher, lambda path, inode: cleanups.append((path, inode)))
    argv, kwargs = calls.pop()
    file = launcher / "start.html"
    assert argv == ["xdg-open", file.as_uri()]
    second = re.search(r"#desktop-login=([A-Za-z0-9_-]{43})", file.read_text())[1]
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", second) and first != second
    assert second not in repr(argv) and first not in repr(argv)
    assert file.stat().st_mode & 0o777 == 0o600
    assert launcher.stat().st_mode & 0o777 == 0o700
    assert cleanups == [(file, file.stat().st_ino)]
    file = root / "desktop-ticket.json"
    data = json.loads(file.read_text())
    assert data["ticket_hash"] == hashlib.sha256(second.encode()).hexdigest()
    assert time.time() < data["expires"] <= time.time() + 120
    assert second not in file.read_text() and first not in file.read_text()
    assert file.stat().st_mode & 0o777 == 0o600
    assert list(root.iterdir()) == [file]
    assert kwargs["check"] is False and "shell" not in kwargs


def test_desktop_other_user_unsafe_directory_and_symlink_use_manual_login(tmp_path, monkeypatch):
    root = tmp_path / "controller"
    root.mkdir(mode=0o755)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert issue_ticket(root) is None
    assert not list(root.iterdir())

    root.chmod(0o700)
    monkeypatch.setattr(os, "geteuid", lambda: 1001)
    assert issue_ticket(root) is None
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    calls = []

    def opener(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0)

    open_desktop(link, opener)
    assert calls == [["xdg-open", "http://localhost:8099/"]]
    assert not list(root.iterdir())


def test_packaged_controller_accepts_real_host_ticket_once(request):
    if not request.config.getoption("--containers"):
        pytest.skip("Use --containers for the real host writer to Controller handoff")
    name = "agentic-desktop-handoff-" + uuid.uuid4().hex[:12]
    host = Path(__file__).resolve().parents[1] / "host"
    try:
        subprocess.run(["docker", "run", "-d", "--name", name, "--network", "none", "--memory", "256m",
                        "--pids-limit", "64", "-e", "AGENTIC_HOST_MODE=1", "-v", str(host) + ":/host:ro",
                        "agentic-home-controller:0.6.0"], check=True, capture_output=True)
        for _ in range(40):
            result = subprocess.run(["docker", "exec", name, "python", "-c",
                "import urllib.request; urllib.request.urlopen('http://localhost:8099/health',timeout=1)"],
                capture_output=True)
            if result.returncode == 0:
                break
            time.sleep(0.1)
        assert result.returncode == 0
        script = '''
import http.cookiejar,json,os,sys,urllib.error,urllib.request
sys.path.insert(0,'/host')
from agentic_host.desktop import issue_ticket
assert os.geteuid()==1000
ticket=issue_ticket('/data')
assert ticket
base='http://localhost:8099'
opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
def login():
    request=urllib.request.Request(base+'/api/host/desktop-login',data=json.dumps({'ticket':ticket}).encode(),headers={'Origin':base,'Content-Type':'application/json'})
    return opener.open(request)
with login() as response:
    assert response.status==200 and json.load(response)['authenticated']
with opener.open(base+'/api/session') as response:
    assert json.load(response)['principal']=='host-owner'
try:
    login()
except urllib.error.HTTPError as error:
    assert error.code==403
else:
    raise AssertionError('Ticket replay accepted')
print(json.dumps({'login':200,'session':'host-owner','replay':403}))
'''
        result = subprocess.run(["docker", "exec", "--user", "1000:1000", name, "python", "-c", script],
                                capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, "Packaged host/Controller ticket handoff failed"
        assert json.loads(result.stdout) == {"login": 200, "session": "host-owner", "replay": 403}
    finally:
        subprocess.run(["docker", "rm", "-fv", name], capture_output=True)
