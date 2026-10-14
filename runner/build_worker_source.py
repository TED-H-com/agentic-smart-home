"""Image-build-only preparation of the fixed public source used by Home coding."""
import json
import os
import re
import subprocess
from pathlib import Path

REPOSITORY = "https://github.com/TED-H-com/agentic-smart-home.git"


def source_lock(path):
    value = json.loads(path.read_text())
    if (set(value) != {"repo_id", "repository", "commit"} or value["repo_id"] != "agentic-home"
            or value["repository"] != REPOSITORY or not isinstance(value["commit"], str)
            or not re.fullmatch(r"[a-f0-9]{40}", value["commit"])):
        raise ValueError("Invalid fixed Home worker source")
    return value


def build(lock_path, destination):
    lock = source_lock(lock_path)
    destination.mkdir(parents=True, mode=0o755)
    target = destination / "source"
    environment = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"}

    def git(*args):
        return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=",
                                        *args], env=environment, timeout=180, stderr=subprocess.PIPE).decode().strip()

    git("init", "--bare", str(target))
    git("-C", str(target), "fetch", "--depth=1", "--no-tags", REPOSITORY, lock["commit"])
    if git("-C", str(target), "rev-parse", "FETCH_HEAD") != lock["commit"]:
        raise ValueError("Home worker source revision differs")
    git("-C", str(target), "update-ref", "refs/heads/release", lock["commit"])
    git("-C", str(target), "symbolic-ref", "HEAD", "refs/heads/release")
    git("-C", str(target), "fsck", "--full")
    (destination / "source.json").write_text(json.dumps(lock, indent=2) + "\n")
    os.chmod(target, 0o700)


if __name__ == "__main__":
    build(Path("/build/coding-source.lock.json"), Path("/opt/agentic-worker"))
