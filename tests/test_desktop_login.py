"""Local desktop handoff never exposes the ticket through process argv."""

import hashlib
import json
import os
import re
import sys
import time
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
