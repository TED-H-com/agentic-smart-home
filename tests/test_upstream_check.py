import hashlib
import importlib.util
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/check_upstream.py"
spec = importlib.util.spec_from_file_location("candidate_check", SCRIPT)
candidate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(candidate)

GOOD = {"compatible": True, "adapter_contract": 1, "observed_from": "runtime_container",
        "cloud_account_verified": False}


@pytest.mark.parametrize("output,code", [
    ({**GOOD, "compatible": False}, 0), ({**GOOD, "adapter_contract": 2}, 0),
    ({**GOOD, "adapter_contract": True}, 0), ({**GOOD, "cloud_account_verified": True}, 0),
    ([], 0), (GOOD, 1), ("x" * 65537, 0), ("not JSON", 0),
])
def test_changed_or_failed_candidate_gate_cannot_be_accepted(output, code):
    result = subprocess.CompletedProcess([], code, output if isinstance(output, str) else json.dumps(output), "")
    with pytest.raises(candidate.CompatibilityFailure):
        candidate.validate_gate(result)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "product"
    (root / "hermes").mkdir(parents=True)
    lock = {"repository": "https://github.com/NousResearch/hermes-agent", "commit": "a" * 40,
            "sha256": "b" * 64, "base_image": "fixed"}
    (root / "hermes/hermes.lock.json").write_text(json.dumps(lock))
    return root, tmp_path / "scratch"


@pytest.mark.parametrize("failure", [None, "build", "probe", "timeout", "cleanup"])
@pytest.mark.parametrize("selection", ["locked", "latest"])
def test_candidate_is_never_activated_and_all_owned_resources_are_cleaned(source, failure, selection):
    root, scratch = source
    original = (root / "hermes/hermes.lock.json").read_bytes()
    calls = []
    built_context = {}
    def run(args, **kwargs):
        calls.append(args)
        if args[:2] == ["docker", "build"]:
            built_context.update({"hermes/" + p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in Path(args[-1]).iterdir()})
            return subprocess.CompletedProcess(args, int(failure == "build"), "", "")
        if args[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(args, 0, "sha256:" + "d" * 64, "")
        if args[:2] == ["docker", "run"]:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(args, 180)
            kwargs["stdout"].write(json.dumps(GOOD))
            assert "--network=none" in args and "--user=10003:10003" in args
            return subprocess.CompletedProcess(args, int(failure == "probe"))
        return subprocess.CompletedProcess(args, int(failure == "cleanup"), "", "cleanup unavailable" if failure == "cleanup" else "")
    report = candidate.check(selection, run=run, root=root, scratch=scratch,
                             resolver=lambda work: {"commit": "c" * 40, "sha256": "d" * 64})
    assert report["state"] == ("passed" if failure is None else "failed")
    assert report["activation"] == "never"
    assert [c[:3] for c in calls[-3:]] == [["docker", "rm", "-fv"], ["docker", "volume", "rm"], ["docker", "image", "rm"]]
    assert (root / "hermes/hermes.lock.json").read_bytes() == original
    assert report["source_hashes"]["hermes/hermes.lock.json"] == hashlib.sha256(original).hexdigest()
    assert report["candidate_context_hashes"] == built_context
    assert (report["source_hashes"] == built_context) == (selection == "locked")
    assert not list(scratch.iterdir())
    assert not any("compose" in c for c in calls)


@pytest.mark.parametrize("failure", [candidate.SetupError("private"), tarfile.ReadError("private"),
    AttributeError("private"), OSError("private")])
def test_failed_resolver_still_returns_safe_evidence_and_cleanup(source, failure):
    root, scratch = source
    calls = []
    def resolver(work):
        raise failure
    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")
    report = candidate.check("latest", run=run, resolver=resolver, root=root, scratch=scratch)
    assert report["state"] == "failed" and report["error_code"] == "candidate_check_unavailable"
    assert report["cleanup_completed"] is True and "private" not in json.dumps(report)
    assert len(calls) == 3 and not list(scratch.iterdir())


def test_candidate_context_symlink_is_rejected_before_build(source, tmp_path):
    root, scratch = source
    target = tmp_path / "outside"
    target.write_text("not a product source file")
    (root / "hermes/untrusted").symlink_to(target)
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")
    report = candidate.check("locked", run=run, root=root, scratch=scratch)
    assert report["error_code"] == "candidate_context_invalid"
    assert not any(c[:2] == ["docker", "build"] for c in calls)
