"""Execute the CI identity shell with valid and invalid source locks."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize("lock_text,passed", [
    (json.dumps({"sha256": "a" * 64}), True),
    (json.dumps({"sha256": "invalid"}), False),
    ("{invalid-json", False),
])
def test_identity_step_stops_before_publishing_invalid_lock(tmp_path, lock_text, passed):
    root = Path(__file__).parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
    step = next(step for job in workflow["jobs"].values() for step in job["steps"]
                if step.get("id") == "hermes-archive")
    hermes = tmp_path / "hermes"
    hermes.mkdir()
    shutil.copyfile(root / "hermes/fetch_upstream.py", hermes / "fetch_upstream.py")
    (hermes / "hermes.lock.json").write_text(lock_text)
    output = tmp_path / "github-output"
    result = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=tmp_path,
                            env={**os.environ, "GITHUB_OUTPUT": str(output)},
                            capture_output=True, text=True, timeout=10, check=False)
    assert (result.returncode == 0) is passed
    if passed:
        assert output.read_text() == "sha256=" + "a" * 64 + "\n"
        assert not result.stderr
    else:
        assert not output.exists()
    assert not (hermes / ".upstream-cache").exists()
