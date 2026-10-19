import json
import subprocess
import uuid
from pathlib import Path

import pytest


def test_real_ubuntu_appliance_generators(request, tmp_path):
    if not request.config.getoption("--containers"):
        pytest.skip("Use --containers; builds a bounded Ubuntu utility fixture, no HAOS")
    root = Path(__file__).resolve().parents[1]
    name = "agentic-appliance-test-" + uuid.uuid4().hex[:12]
    tag = "agentic-home-appliance-test:20261019"
    # CI builds this small dedicated image; local cached builds reuse its layers.
    subprocess.run(["docker", "build", "-t", tag, "-f", str(root / "tests/appliance.Dockerfile"),
                    str(root)], capture_output=True, text=True, check=True, timeout=300)
    try:
        result = subprocess.run(["docker", "run", "--rm", "--name", name, "--network", "none",
                                 "--cpus", "1", "--memory", "512m",
                                 "-v", f"{root}:/source:ro", "-v", f"{tmp_path}:/scratch", tag],
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads(result.stdout.strip().splitlines()[-1])
        assert report["dconf"] == "compiled_defaults_and_locks"
        assert set(report) == {"networkd", "NetworkManager", "dconf", "scope", "versions"}
    finally:
        subprocess.run(["docker", "rm", "-fv", name], capture_output=True, timeout=30)
