"""Fixed host initial setup, using disk fixtures and no real host changes."""

import ast
import io
import json
import sqlite3
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from agentic_host.bootstrap import (  # noqa: E402
    ARCHIVE_BASE,
    HEAD_URL,
    InitialSetup,
    SetupError,
    initial_state,
    latest_source,
    native_ha_status,
    write_environment,
)
from agentic_host.daemon import Jobs  # noqa: E402
from agentic_host.manager import Manager, atomic  # noqa: E402

COMMIT = "a" * 40
ORIGINAL = "sha256:" + "1" * 64
CANDIDATE = "sha256:" + "2" * 64


def test_real_installer_environment_accepts_only_comments_and_locked_image_values(tmp_path):
    # Use the exact fresh-install literal, not a hand-maintained lookalike.
    tree = ast.parse((Path(__file__).parents[1] / "host/install.py").read_text())
    values = [node.args[0].value for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "write_text" and node.args and isinstance(node.args[0], ast.Constant)
              and isinstance(node.args[0].value, str) and node.args[0].value.startswith("# Initial Hermes")]
    assert len(values) == 1
    path = tmp_path / "runtime.env"
    path.write_text(values[0] + "\n  # permitted standalone comment\n\t\n")
    write_environment(path, CANDIDATE)
    assert path.read_text() == "AGENTIC_HERMES_IMAGE=" + CANDIDATE + "\n"
    # The updater imports and uses the same writer for Controller activation.
    from agentic_host.updater import write_environment as updater_write_environment
    assert updater_write_environment is write_environment
    updater_write_environment(path, ORIGINAL, "AGENTIC_CONTROLLER_IMAGE")
    assert path.read_text() == "AGENTIC_CONTROLLER_IMAGE=" + ORIGINAL + "\nAGENTIC_HERMES_IMAGE=" + CANDIDATE + "\n"
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("content", [
    "UNKNOWN_IMAGE=" + ORIGINAL + "\n",
    "AGENTIC_HERMES_IMAGE=" + ORIGINAL + "\nAGENTIC_HERMES_IMAGE=" + ORIGINAL + "\n",
    "AGENTIC_CONTROLLER_IMAGE=" + ORIGINAL + "\nAGENTIC_CONTROLLER_IMAGE=" + CANDIDATE + "\n",
    "AGENTIC_HERMES_IMAGE=untrusted:latest\n",
    "AGENTIC_HERMES_IMAGE=" + ORIGINAL + " # inline data is not accepted\n",
    "not-an-environment-assignment\n",
])
def test_environment_writer_still_rejects_unknown_duplicates_and_invalid_digests(tmp_path, content):
    path = tmp_path / "runtime.env"
    path.write_text(content)
    with pytest.raises(SetupError, match="candidate_environment_invalid"):
        write_environment(path, CANDIDATE)
    assert path.read_text() == content


def archive(name=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as stream:
        member = tarfile.TarInfo(name or "hermes-agent-" + COMMIT + "/run_agent.py")
        member.size = 4
        stream.addfile(member, io.BytesIO(b"pass"))
    return output.getvalue()


def test_resolve_immutable_official_source_and_reject_bad_archive(tmp_path):
    calls = []
    payload = archive()

    def opener(url):
        calls.append(url)
        return io.BytesIO(json.dumps({"sha": COMMIT}).encode() if url == HEAD_URL else payload)

    result = latest_source(tmp_path, opener)
    assert calls == [HEAD_URL, ARCHIVE_BASE + COMMIT]
    assert result["commit"] == COMMIT and len(result["sha256"]) == 64
    (tmp_path / "source.tar.gz").unlink()
    payload = archive("hermes-agent-" + COMMIT + "/../../root")
    with pytest.raises(SetupError, match="archive_path"):
        latest_source(tmp_path, opener)
    with pytest.raises(SetupError, match="revision_invalid"):
        latest_source(tmp_path, lambda url: io.BytesIO(b'{"sha":"../../somewhere"}'))


def test_native_onboarding_requires_real_complete_typed_steps():
    steps = [{"step": name, "done": False} for name in ("user", "core_config", "integration", "analytics")]
    def observe(body):
        return native_ha_status(lambda *args, **kwargs: io.BytesIO(json.dumps(body).encode()))
    assert observe(steps)["onboarding_available"] is True
    assert observe(steps)["onboarding_complete"] is False
    for item in steps:
        item["done"] = True
    assert observe(steps)["onboarding_complete"] is True
    for invalid in ([], {}, [{"step": "user", "done": True}], [{"step": "user", "done": "true"}], steps + steps):
        assert observe(invalid)["reachable"] is False
        assert observe(invalid)["onboarding_complete"] is False


def test_initial_install_never_replaces_account_or_logged_out_used_runtime(tmp_path):
    initial_state(tmp_path)
    (tmp_path / "provider.json").write_text('{"state":"configured"}')
    with pytest.raises(SetupError, match="existing_hermes_account"):
        initial_state(tmp_path)
    (tmp_path / "provider.json").unlink()
    with sqlite3.connect(tmp_path / "runtime.sqlite") as db:
        db.executescript("CREATE TABLE records(kind TEXT); CREATE TABLE requests(id TEXT); INSERT INTO requests VALUES ('used');")
    with pytest.raises(SetupError, match="existing_hermes_sessions"):
        initial_state(tmp_path)


@pytest.fixture
def installer(tmp_path, monkeypatch):
    product = tmp_path / "product"
    (product / "hermes").mkdir(parents=True)
    (product / "hermes/hermes.lock.json").write_text(json.dumps({"commit": "b" * 40, "python": "3.14"}))
    (product / "images.lock.json").write_text(json.dumps({"agentic-home-hermes:0.5.1": ORIGINAL}))
    calls = []
    flags = {"probe_fails": False, "activation_fails": False, "fallback_fails": False, "race_account": False}
    manager = Manager(tmp_path / "host", product=product)

    def run(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("ip", "-json"):
            return SimpleNamespace(stdout='[{"dst":"default","dev":"eth0","flags":[]}]', returncode=0)
        if args[:2] == ("docker", "info"):
            return SimpleNamespace(stdout=json.dumps({"MemoryLimit": True, "SwapLimit": True, "PidsLimit": True,
                                   "CpuCfsQuota": True, "CgroupVersion": "2", "SecurityOptions": ["name=seccomp", "name=apparmor"]}), returncode=0)
        if args[:3] == ("docker", "compose", "version"):
            return SimpleNamespace(stdout='{"version":"future-compatible"}', returncode=0)
        if args == ("docker", "compose", "up", "--help"):
            return SimpleNamespace(stdout="--wait --no-deps", returncode=0)
        if args[:3] == ("docker", "image", "inspect"):
            return SimpleNamespace(stdout=flags.get("inspected_image", CANDIDATE), returncode=0)
        if args[:2] == ("docker", "run"):
            if flags["probe_fails"]:
                raise RuntimeError("sensitive build detail must never escape")
            return SimpleNamespace(stdout=flags.get("probe_output", json.dumps({
                "compatible": True, "adapter_contract": 1,
                "observed_from": "runtime_container", "cloud_account_verified": False})), returncode=0)
        if "stop" in args and flags["race_account"]:
            data = tmp_path / "hermes"
            data.mkdir()
            (data / "provider.json").write_text('{"state":"configured"}')
        if "up" in args:
            value = (tmp_path / "host/runtime.env").read_text()
            if (CANDIDATE in value and flags["activation_fails"]) or (ORIGINAL in value and flags["fallback_fails"]):
                raise RuntimeError("sensitive activation detail must never escape")
        return SimpleNamespace(stdout="", returncode=0)

    manager.run = run
    monkeypatch.setattr("agentic_host.bootstrap.os.chown", lambda *args: None)
    instance = InitialSetup(manager, resolver=lambda path: {"commit": COMMIT, "archive_url": ARCHIVE_BASE + COMMIT, "sha256": "c" * 64})
    return instance, calls, flags


def test_candidate_is_probed_without_credentials_and_activated_once(installer):
    setup, calls, flags = installer
    result = setup.install()
    assert result["state"] == "ready" and result["commit"] == COMMIT
    assert CANDIDATE in setup.environment.read_text()
    probe = next(c for c in calls if c[:2] == ("docker", "run"))
    assert probe[probe.index("--network") + 1] == "none"
    assert "--read-only" in probe and "--cap-drop" in probe
    assert not any("/data" in arg or "hermes-link" in arg for arg in probe)
    before = list(calls)
    assert setup.install() == result and calls == before
    assert not list(setup.root.glob("hermes-install-*"))
    assert setup.environment.stat().st_mode & 0o777 == 0o600


def test_production_onboarding_uses_shipped_digest_without_upstream_discovery(installer, monkeypatch):
    setup, calls, flags = installer
    flags["inspected_image"] = ORIGINAL
    def forbidden(*args, **kwargs):
        raise AssertionError("installed onboarding must not fetch upstream HEAD")
    monkeypatch.setattr("agentic_host.bootstrap.latest_source", forbidden)
    monkeypatch.setattr("agentic_host.bootstrap.open_public", forbidden)
    release = InitialSetup(setup.manager)
    result = release.install()
    assert result == {"state": "ready", "commit": "b" * 40, "source": "release"}
    assert ORIGINAL in release.environment.read_text()
    assert not any(c[:2] == ("docker", "build") for c in calls)
    assert not any(c[:3] == ("docker", "image", "rm") for c in calls)
    probe = next(c for c in calls if c[:2] == ("docker", "run"))
    assert ORIGINAL in probe and "--read-only" in probe and "none" in probe
    before = list(calls)
    assert release.install() == result and calls == before


def test_production_rejects_missing_or_mismatched_release_image_before_stop(installer):
    setup, calls, flags = installer
    with pytest.raises(SetupError, match="bundled_image_invalid"):
        InitialSetup(setup.manager).install()
    assert not any("stop" in c or c[:2] == ("docker", "build") for c in calls)
    assert setup.state()["state"] == "failed"


@pytest.mark.parametrize("failure", ["probe_fails", "activation_fails", "race_account"])
def test_failed_candidate_preserves_original_and_rechecks_auth(installer, failure):
    setup, calls, flags = installer
    flags[failure] = True
    with pytest.raises(SetupError) as error:
        setup.install()
    assert "sensitive" not in str(error.value)
    assert setup.state()["state"] == "failed"
    assert ORIGINAL in setup.environment.read_text()
    assert not list(setup.root.glob("hermes-install-*"))
    assert any(c[:3] == ("docker", "rm", "-f") for c in calls)
    if failure == "probe_fails":
        assert not any("stop" in c or ("up" in c and "--help" not in c) for c in calls)


@pytest.mark.parametrize("output", ["", "[]", "null", "{" , "x" * 65537,
    json.dumps({"compatible": True}),
    *[json.dumps({"compatible": True, "adapter_contract": version,
        "observed_from": "runtime_container", "cloud_account_verified": False}) for version in (True, 2)],
    json.dumps({"compatible": True, "adapter_contract": 1,
        "observed_from": "runtime_container", "cloud_account_verified": True})])
def test_success_exit_without_valid_adapter_contract_never_activates(installer, output):
    setup, calls, flags = installer
    flags["probe_output"] = output
    with pytest.raises(SetupError, match="hermes_adapter_contract_unavailable"):
        setup.install()
    assert setup.state()["state"] == "failed"
    assert ORIGINAL in setup.environment.read_text()
    assert not any("stop" in call or ("up" in call and "--help" not in call) for call in calls)


def test_failed_fallback_and_restart_never_blindly_replay(installer):
    setup, calls, flags = installer
    flags.update(activation_fails=True, fallback_fails=True)
    with pytest.raises(SetupError, match="fallback_unconfirmed"):
        setup.install()
    assert setup.state()["state"] == "needs_reconcile"
    before = list(calls)
    with pytest.raises(SetupError, match="needs_reconcile"):
        setup.install()
    assert calls == before
    atomic(setup.path, {"state": "installing", "source": "latest", "commit": COMMIT})
    atomic(setup.root / "jobs.json", {"fixture-id": {"state": "running", "request": {"action": "bootstrap_hermes"}}})
    jobs = Jobs(setup.manager)
    assert jobs.items["fixture-id"]["state"] == "needs_reconcile"
    assert setup.state()["state"] == "needs_reconcile"


def test_image_override_cannot_inject_environment_or_shell(tmp_path):
    with pytest.raises(SetupError, match="image_invalid"):
        write_environment(tmp_path / "runtime.env", "evil\nANOTHER_VAR=1")
    assert not (tmp_path / "runtime.env").exists()
