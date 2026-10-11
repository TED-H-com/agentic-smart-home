"""Prepare only app-owned data, then run the controller as an unprivileged UID."""
import os
import runpy
from pathlib import Path

os.umask(0o077)
data = Path(os.environ.get("AGENTIC_DATA", "/data"))
data.mkdir(parents=True, exist_ok=True)
if os.getuid() == 0:
    os.chown(data, 1000, 1000)
    for name in ("controller.sqlite3", "controller.sqlite3-wal", "controller.sqlite3-shm", "snapshots"):
        path = data / name
        if path.exists() and not path.is_symlink():
            os.chown(path, 1000, 1000)
    os.setgroups([])
    os.setgid(1000)
    os.setuid(1000)
runpy.run_module("agentic_controller.app", run_name="__main__")
