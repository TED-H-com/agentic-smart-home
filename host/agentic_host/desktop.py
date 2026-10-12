"""One-use local desktop handoff without a management bearer in process argv."""

import hashlib
import json
import os
import pwd
import secrets
import stat
import subprocess
import time
from pathlib import Path

CONTROLLER = Path("/var/lib/agentic-home/controller")
URL = "http://localhost:8099/"


def issue_ticket(root=CONTROLLER):
    # The installed Controller and primary desktop owner share UID 1000.
    # Other OS users must use the existing explicit access-key login.
    if os.geteuid() != 1000:
        return None
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = None
    try:
        info = os.fstat(directory)
        if info.st_uid != 1000 or stat.S_IMODE(info.st_mode) != 0o700:
            return None
        ticket = secrets.token_urlsafe(32)
        record = {"ticket_hash": hashlib.sha256(ticket.encode()).hexdigest(), "expires": time.time() + 120}
        temporary = ".desktop-ticket-" + secrets.token_hex(16)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                     dir_fd=directory)
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, "desktop-ticket.json", src_dir_fd=directory, dst_dir_fd=directory)
        temporary = None
        os.fsync(directory)
        return ticket
    finally:
        if temporary is not None:
            os.unlink(temporary, dir_fd=directory)
        os.close(directory)


def schedule_cleanup(path, inode):
    # A detached, finite cleanup cannot delete a later launcher opened in the
    # meantime. Test callers replace this scheduler; no test sleeper is left.
    if os.fork() == 0:
        try:
            os.setsid()
            os.closerange(0, 1024)
            time.sleep(120)
            if path.lstat().st_ino == inode:
                path.unlink()
        except OSError:
            pass
        finally:
            os._exit(0)


def private_launcher(ticket, directory=None, cleanup=schedule_cleanup):
    # Use an ordinary owner-home directory so the Ubuntu browser can read it.
    # Other local users can see its path in argv, but cannot read its content.
    directory = directory or Path(pwd.getpwuid(1000).pw_dir) / "Agentic-Home-Login"
    directory = Path(directory)
    directory.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = ".start-" + secrets.token_hex(16)
    try:
        info = os.fstat(fd)
        if info.st_uid != 1000 or stat.S_IMODE(info.st_mode) != 0o700:
            raise PermissionError("Unsafe launcher directory")
        file = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with os.fdopen(file, "w") as stream:
            stream.write('<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
                         '<meta http-equiv="refresh" content="0;url=' + URL + '#desktop-login=' + ticket + '">')
            stream.flush()
            os.fsync(stream.fileno())
            inode = os.fstat(stream.fileno()).st_ino
        os.replace(temporary, "start.html", src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass
        os.close(fd)
    path = directory / "start.html"
    cleanup(path, inode)
    return path.as_uri()


def open_desktop(root=CONTROLLER, opener=subprocess.run, launcher_directory=None, cleanup=schedule_cleanup):
    try:
        ticket = issue_ticket(root)
        url = private_launcher(ticket, launcher_directory, cleanup) if ticket else URL
    except OSError:
        url = URL
    # No shell, query string, process-argv bearer or raw-token output.
    result = opener(["xdg-open", url], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode:
        raise SystemExit("Unable to open the browser. Open http://localhost:8099 manually.")
