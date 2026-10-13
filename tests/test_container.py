"""Exercise packaged binaries and TLS inside the actual Runner image."""
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest


def test_packaged_linejs_gateway_boundary(request, tmp_path):
    if not request.config.getoption("--containers"):
        pytest.skip("Use --containers after building LINE.js")
    root = Path(__file__).parents[1]
    name = "agentic-linejs-test-" + uuid.uuid4().hex[:12]
    for folder in ("data", "config"):
        (tmp_path / folder).mkdir(mode=0o700)
    try:
        subprocess.run(["docker", "run", "-d", "--name", name, "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}", "--memory", "512m",
                        "-v", f"{tmp_path / 'data'}:/data", "-v", f"{tmp_path / 'config'}:/config",
                        "-v", f"{root / 'tests/linejs_probe.mjs'}:/probe.mjs:ro",
                        "agentic-home-linejs:0.8.0"], check=True, capture_output=True)
        result = subprocess.run(["docker", "exec", name, "node", "/probe.mjs"],
                                capture_output=True, text=True, timeout=45)
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads(result.stdout.strip())
        report["image"] = subprocess.check_output(
            ["docker", "image", "inspect", "--format", "{{.Id}}", "agentic-home-linejs:0.8.0"], text=True).strip()
        (root / "evidence/linejs-container.json").write_text(json.dumps(report, indent=2) + "\n")
    finally:
        subprocess.run(["docker", "rm", "-fv", name], capture_output=True)


def test_packaged_runner_cli_and_tls(request):
    if not request.config.getoption("--containers"):
        pytest.skip("Use --containers after building the Runner image")
    root = Path(__file__).parents[1]
    name = "agentic-runner-test-" + uuid.uuid4().hex[:12]
    try:
        result = subprocess.run(["docker", "run", "--rm", "--name", name, "--memory", "1536m", "--cpus", "2",
            "--entrypoint", "python", "-v", f"{root / 'tests/container_probe.py'}:/app/container_probe.py:ro",
            "agentic-home-runner:0.8.0", "/app/container_probe.py"], capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads(result.stdout.strip().splitlines()[-1])
        report["image"] = json.loads(subprocess.run(["docker", "image", "inspect", "agentic-home-runner:0.8.0"],
                                capture_output=True, check=True, text=True).stdout)[0]["Id"]
        (root / "evidence/runner-container.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    finally:
        subprocess.run(["docker", "rm", "-fv", name], capture_output=True)


@pytest.mark.parametrize("app,uid", [("controller", "1000"), ("runner", "0"), ("bridge", "10003")])
def test_packaged_app_start_restart_and_stop(request, app, uid):
    if not request.config.getoption("--containers"):
        pytest.skip("Use --containers after building both App images")
    name = "agentic-lifecycle-test-" + uuid.uuid4().hex[:12]
    try:
        subprocess.run(["docker", "run", "-d", "--name", name, "--memory", "768m", "--cpus", "1",
                        f"agentic-home-{app}:0.8.0"], check=True, capture_output=True)
        for restart in (False, True):
            if restart:
                subprocess.run(["docker", "restart", "--timeout", "8", name], check=True, capture_output=True)
            for _ in range(20):
                result = subprocess.run(["docker", "exec", name, "python", "-c",
                    "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8099/health',timeout=2).status)"],
                    capture_output=True, text=True)
                if result.returncode == 0:
                    assert result.stdout.strip() == "200"
                    break
                time.sleep(0.25)
            else:
                raise AssertionError(f"{app} did not start or restart")
        uid_result = subprocess.run(["docker", "exec", name, "python", "-c",
            "from pathlib import Path; print(next(x for x in Path('/proc/1/status').read_text().splitlines() if x.startswith('Uid:')).split()[1])"],
            check=True, capture_output=True, text=True)
        assert uid_result.stdout.strip() == uid
        subprocess.run(["docker", "stop", "--time", "8", name], check=True, capture_output=True)
        state = json.loads(subprocess.run(["docker", "inspect", name], capture_output=True, text=True, check=True).stdout)[0]["State"]
        assert state["ExitCode"] in {0, 143} and not state["OOMKilled"], state
    finally:
        subprocess.run(["docker", "rm", "-fv", name], capture_output=True)


def test_official_supervisor_app_schema_and_backup_filter(request):
    if not request.config.getoption("--containers"):
        pytest.skip("Use --containers to validate against the official Supervisor package")
    root = Path(__file__).parents[1]
    name = "agentic-supervisor-test-" + uuid.uuid4().hex[:12]
    try:
        result = subprocess.run(["docker", "run", "--rm", "--name", name, "--memory", "768m", "--cpus", "1",
            "--entrypoint", "python", "-v", f"{root}:/source:ro",
            "ghcr.io/home-assistant/amd64-hassio-supervisor:2026.09.3", "/source/tests/supervisor_probe.py"],
            capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads(result.stdout.strip().splitlines()[-1])
        (root / "evidence/supervisor-package.json").write_text(json.dumps(report, indent=2) + "\n")
    finally:
        subprocess.run(["docker", "rm", "-fv", name], capture_output=True)
