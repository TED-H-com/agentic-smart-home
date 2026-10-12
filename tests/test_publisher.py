import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from agentic_host.manager import HostError
from agentic_host.publisher import enroll, inspect, public_key


def key():
    return Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def test_publisher_review_binds_key_and_repository_without_enrollment():
    pem = key()
    first = inspect("fixture", "ghcr.io/fixture/controller", pem)
    assert first["review_digest"] != inspect("fixture", "ghcr.io/other/controller", pem)["review_digest"]
    assert first["review_digest"] != inspect("fixture", "ghcr.io/fixture/controller", key())["review_digest"]
    with pytest.raises(HostError, match="ed25519"):
        public_key("-----BEGIN PRIVATE KEY-----\nnot accepted\n-----END PRIVATE KEY-----")


def test_non_root_cannot_install_a_publisher(monkeypatch, tmp_path):
    monkeypatch.setattr("agentic_host.publisher.os.geteuid", lambda: 1000)
    pem = key()
    with pytest.raises(HostError, match="local_root_required"):
        enroll("fixture", "ghcr.io/fixture/controller", pem, inspect("fixture", "ghcr.io/fixture/controller", pem)["review_digest"], path=tmp_path / "never-written")
    assert not (tmp_path / "never-written").exists()


def test_actual_root_cli_trust_enrollment_revocation_in_disposable_container(tmp_path, request):
    if not request.config.getoption("--containers"):
        pytest.skip("requires packaged container environment")
    root = Path(__file__).resolve().parents[1]
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    pem = tmp_path / "public.pem"
    pem.write_text(key())
    script = """import json,os,subprocess,sys
from pathlib import Path
from agentic_host.publisher import TRUST
base=['python','-m','agentic_host.cli']
fields=['--publisher','fixture','--repository','ghcr.io/fixture/controller','--public-key','/public.pem']
review=json.loads(subprocess.check_output(base+['update-publisher-inspect']+fields))
assert not TRUST.exists()
bad=subprocess.run(base+['update-publisher-add']+fields+['--approval','0'*64],capture_output=True)
assert bad.returncode and not TRUST.exists()
added=json.loads(subprocess.check_output(base+['update-publisher-add']+fields+['--approval',review['review_digest']]))
assert added['state']=='trusted' and TRUST.stat().st_uid==0 and TRUST.stat().st_mode&0o777==0o600
assert set(json.loads(TRUST.read_text())['publishers'])=={'fixture'}
removed=json.loads(subprocess.check_output(base+['update-publisher-revoke']+fields+['--approval',review['review_digest']]))
assert removed['state']=='revoked' and json.loads(TRUST.read_text())['publishers']=={}
print(json.dumps({'root_cli_enrollment':'passed','root_cli_revocation':'passed','exact_review_required':True,'trust_file_mode':'0600','development_host_modified':False}))
"""
    result = subprocess.run(["docker", "run", "--rm", "--network", "none", "--user", "0:0", "--cap-drop", "ALL",
                             "--memory", "128m", "--pids-limit", "16", "--entrypoint", "python",
                             "--mount", f"type=bind,src={root / 'host'},dst=/product/host,readonly",
                             "--mount", f"type=bind,src={pem},dst=/public.pem,readonly",
                             "--env", "PYTHONPATH=/product/host", "agentic-home-preview:" + version, "-c", script],
                            check=True, capture_output=True, text=True, timeout=15)
    evidence = json.loads(result.stdout)
    assert evidence["root_cli_enrollment"] == "passed"
    (root / "evidence/publisher-enrollment.json").write_text(json.dumps(evidence, indent=2) + "\n")
