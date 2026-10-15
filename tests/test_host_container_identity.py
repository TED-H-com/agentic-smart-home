"""Exact Host writer identity survives missing metadata and reusable-name races."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from agentic_host.coding_sandbox import CodingSandbox, SandboxError

JOB = "identity-test-0001"
NAME = "agentic-coding-" + JOB
ORIGINAL = "1" * 64
REPLACEMENT = "2" * 64
IMAGE = "sha256:" + "3" * 64
MUTATIONS = {"start", "kill", "wait", "rm"}


class DockerFixture:
    def __init__(self, *, named=ORIGINAL, labeled=None, inventory=None, running=True):
        self.named = named
        self.labeled = [ORIGINAL] if labeled is None else labeled
        self.inventory = [ORIGINAL] if inventory is None else inventory
        self.running = {ORIGINAL: running, REPLACEMENT: True}
        self.calls = []
        self.failure = None
        self.after_inspect = None
        self.before_mutation = None

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        operation = args[1]
        if self.failure == operation or (self.failure == "inventory" and operation == "ps" and "--filter" not in args):
            return SimpleNamespace(returncode=1, stdout="")
        if operation == "inspect":
            identity = self.named
            if identity is None:
                return SimpleNamespace(returncode=1, stdout="")
            info = {"Id": identity, "Config": {"Labels": {"agentic.coding": NAME}},
                    "State": {"Running": self.running[identity], "Paused": False,
                              "Status": "running" if self.running[identity] else "exited", "ExitCode": 137}}
            if self.after_inspect:
                self.after_inspect()
            return SimpleNamespace(returncode=0, stdout=json.dumps([info]))
        if operation == "ps":
            assert "--no-trunc" in args and args[-2:] == ("--format", "{{.ID}}")
            identities = self.labeled if "--filter" in args else self.inventory
            return SimpleNamespace(returncode=0, stdout="".join(value + "\n" for value in identities))
        if operation in MUTATIONS:
            if self.before_mutation:
                self.before_mutation(operation, args[-1])
            if operation == "kill":
                self.running[args[-1]] = False
        return SimpleNamespace(returncode=0, stdout=ORIGINAL + "\n" if operation == "create" else "")


def sandbox_at(tmp_path, docker, *, original=ORIGINAL, state="running"):
    sandbox = CodingSandbox(tmp_path, IMAGE, {"product": tmp_path}, execute=docker)
    job = {"job_id": JOB, "container": NAME, "state": state}
    if original != "missing":
        job["container_id"] = original
    sandbox.save(job)
    return sandbox


def assert_fenced(sandbox, docker):
    assert sandbox.cancel(JOB)["state"] == "stop_unconfirmed"
    with pytest.raises(SandboxError, match="release"):
        sandbox.dispose(JOB)
    assert not any(call[0] == "umount" or call[1] in MUTATIONS for call in docker.calls)


@pytest.mark.parametrize("labeled", [[REPLACEMENT], [ORIGINAL, REPLACEMENT]])
def test_replaced_label_cannot_adopt_stop_or_release_foreign_writer(tmp_path, labeled, monkeypatch):
    docker = DockerFixture(named=REPLACEMENT, labeled=labeled, inventory=[REPLACEMENT])
    sandbox = sandbox_at(tmp_path, docker)
    monkeypatch.setattr(sandbox, "_unmount_workspace", lambda _: pytest.fail("workspace must remain held"))
    assert sandbox.status(JOB)["state"] == "stop_unconfirmed"
    assert_fenced(sandbox, docker)
    assert sandbox.record(JOB)["container_id"] == ORIGINAL
    assert docker.running[REPLACEMENT]
    assert not any(call[1] == "ps" and "--filter" not in call for call in docker.calls)


@pytest.mark.parametrize("inventory", [
    [ORIGINAL], [ORIGINAL, REPLACEMENT], [REPLACEMENT[:12]], [REPLACEMENT, REPLACEMENT],
    ["not-an-id"], [""], [" " + REPLACEMENT],
])
def test_empty_labels_do_not_prove_original_absence(tmp_path, inventory):
    # The original may still exist under a different name or without the expected label.
    docker = DockerFixture(named=None, labeled=[], inventory=inventory)
    sandbox = sandbox_at(tmp_path, docker)
    assert_fenced(sandbox, docker)
    assert sandbox.record(JOB)["container_id"] == ORIGINAL
    assert any(call[1] == "ps" and "--filter" not in call for call in docker.calls)


@pytest.mark.parametrize("labels", [{}, {"agentic.coding": "other-name"}])
def test_original_with_missing_or_changed_label_remains_fenced(tmp_path, labels, monkeypatch):
    docker = DockerFixture(labeled=[], inventory=[ORIGINAL])
    sandbox = sandbox_at(tmp_path, docker)
    execute = docker.__call__

    def changed_label(*args, **kwargs):
        result = execute(*args, **kwargs)
        if args[:2] == ("docker", "inspect"):
            info = json.loads(result.stdout)
            info[0]["Config"]["Labels"] = labels
            result.stdout = json.dumps(info)
        return result

    sandbox.execute = changed_label
    monkeypatch.setattr(sandbox, "_unmount_workspace", lambda _: pytest.fail("workspace must remain held"))
    assert_fenced(sandbox, docker)
    assert sandbox.record(JOB)["container_id"] == ORIGINAL


@pytest.mark.parametrize("inventory", [[], [REPLACEMENT]])
def test_successful_complete_inventory_can_prove_known_original_absent(tmp_path, inventory):
    docker = DockerFixture(named=None, labeled=[], inventory=inventory)
    sandbox = sandbox_at(tmp_path, docker)
    assert sandbox.cancel(JOB)["state"] == "cancelled"
    assert sandbox.record(JOB)["container_id"] == ORIGINAL
    assert not any(call[1] in MUTATIONS for call in docker.calls)


@pytest.mark.parametrize("inventory", [[], [REPLACEMENT]])
def test_proven_absent_original_can_release_workspace_without_removing_a_name(tmp_path, inventory, monkeypatch):
    docker = DockerFixture(named=None, labeled=[], inventory=inventory)
    sandbox = sandbox_at(tmp_path, docker)
    unmounted = []
    monkeypatch.setattr(sandbox, "_unmount_workspace", unmounted.append)
    assert sandbox.dispose(JOB)["state"] == "disposed"
    assert unmounted == [JOB]
    assert sandbox.record(JOB)["container_id"] == ORIGINAL
    assert not any(call[1] in MUTATIONS for call in docker.calls)


@pytest.mark.parametrize("change", ["replacement", "original_present", "malformed", "duplicate", "silent"])
def test_terminal_cancel_record_still_reconfirms_absence_before_release(tmp_path, change, monkeypatch):
    docker = DockerFixture(named=None, labeled=[], inventory=[])
    sandbox = sandbox_at(tmp_path, docker)
    assert sandbox.cancel(JOB)["state"] == "cancelled"
    if change == "replacement":
        docker.named, docker.labeled = REPLACEMENT, [REPLACEMENT]
    elif change == "original_present":
        docker.inventory = [ORIGINAL]
    elif change == "malformed":
        docker.inventory = ["truncated"]
    elif change == "duplicate":
        docker.inventory = [REPLACEMENT, REPLACEMENT]
    else:
        docker.failure = "version"
    monkeypatch.setattr(sandbox, "_unmount_workspace", lambda _: pytest.fail("workspace must remain held"))
    with pytest.raises(SandboxError):
        sandbox.dispose(JOB)
    assert sandbox.record(JOB)["container_id"] == ORIGINAL
    assert not any(call[1] in MUTATIONS for call in docker.calls)


@pytest.mark.parametrize("failure", ["version", "ps", "inventory"])
def test_unavailable_inventory_preserves_original_fence(tmp_path, failure):
    docker = DockerFixture(named=None, labeled=[], inventory=[])
    docker.failure = failure
    sandbox = sandbox_at(tmp_path, docker)
    assert_fenced(sandbox, docker)
    assert sandbox.record(JOB)["container_id"] == ORIGINAL


@pytest.mark.parametrize("original", [None, "", "1" * 12, "bad-id", True])
def test_malformed_persisted_identity_is_never_treated_as_partial_create(tmp_path, original):
    docker = DockerFixture(named=REPLACEMENT, labeled=[REPLACEMENT])
    sandbox = sandbox_at(tmp_path, docker, original=original)
    assert_fenced(sandbox, docker)
    assert sandbox.record(JOB)["container_id"] == original
    assert not docker.calls


@pytest.mark.parametrize("observed", [None, "", ORIGINAL[:12], "missing"])
def test_missing_or_truncated_inspect_identity_cannot_authorize_stop(tmp_path, observed):
    docker = DockerFixture()
    sandbox = sandbox_at(tmp_path, docker)
    execute = docker.__call__

    def inspect_without_identity(*args, **kwargs):
        result = execute(*args, **kwargs)
        if args[:2] == ("docker", "inspect"):
            info = json.loads(result.stdout)
            if observed == "missing":
                info[0].pop("Id")
            else:
                info[0]["Id"] = observed
            result.stdout = json.dumps(info)
        return result

    sandbox.execute = inspect_without_identity
    assert_fenced(sandbox, docker)
    assert sandbox.record(JOB)["container_id"] == ORIGINAL


@pytest.mark.parametrize("labeled", [["bad-id"], [ORIGINAL[:12]], [ORIGINAL, ORIGINAL]])
def test_malformed_label_inventory_cannot_supply_a_missing_identity(tmp_path, labeled):
    docker = DockerFixture(labeled=labeled)
    sandbox = sandbox_at(tmp_path, docker, original="missing")
    assert_fenced(sandbox, docker)
    assert "container_id" not in sandbox.record(JOB)


def test_missing_identity_requires_label_absence_or_durable_exact_recovery(tmp_path):
    docker = DockerFixture(named=None, labeled=[], inventory=[REPLACEMENT])
    sandbox = sandbox_at(tmp_path, docker, original="missing")
    assert sandbox.cancel(JOB)["state"] == "cancelled"
    assert not any(call[1] == "ps" and "--filter" not in call for call in docker.calls)
    sandbox.save({"job_id": JOB, "container": NAME, "state": "preparing"})
    docker.named, docker.labeled = ORIGINAL, [ORIGINAL]

    def verify_persisted(operation, identity):
        assert identity == ORIGINAL
        assert sandbox.record(JOB)["container_id"] == ORIGINAL

    docker.before_mutation = verify_persisted
    assert sandbox.cancel(JOB)["state"] == "cancelled"
    assert sandbox.record(JOB)["container_id"] == ORIGINAL
    assert not docker.running[ORIGINAL] and docker.running[REPLACEMENT]


def test_partial_create_identity_survives_uncertain_stop_and_next_replacement(tmp_path):
    docker = DockerFixture(named=None, labeled=[ORIGINAL])
    sandbox = sandbox_at(tmp_path, docker, original="missing")
    assert sandbox.cancel(JOB)["state"] == "stop_unconfirmed"
    assert sandbox.record(JOB)["container_id"] == ORIGINAL
    docker.named, docker.labeled = REPLACEMENT, [REPLACEMENT]
    assert_fenced(sandbox, docker)
    assert sandbox.record(JOB)["container_id"] == ORIGINAL


def test_same_identity_fallback_still_confirms_and_stops_original(tmp_path):
    docker = DockerFixture()
    sandbox = sandbox_at(tmp_path, docker)
    initial = {"failed": False}
    execute = docker.__call__

    def once(*args, **kwargs):
        if args[:2] == ("docker", "inspect") and not initial["failed"]:
            initial["failed"] = True
            return SimpleNamespace(returncode=1, stdout="")
        return execute(*args, **kwargs)

    sandbox.execute = once
    assert sandbox._confirm_writer(sandbox.record(JOB)) == "stopped"
    assert any(call[1] == "ps" for call in docker.calls)
    assert [call[-1] for call in docker.calls if call[1] in MUTATIONS] == [ORIGINAL, ORIGINAL]


@pytest.mark.parametrize("partial_create", [False, True])
def test_name_replacement_between_inspect_and_stop_cannot_mutate_replacement(tmp_path, partial_create):
    docker = DockerFixture()
    sandbox = sandbox_at(tmp_path, docker, original="missing" if partial_create else ORIGINAL)

    def replace():
        docker.named, docker.labeled = REPLACEMENT, [REPLACEMENT]

    docker.after_inspect = replace
    assert sandbox.cancel(JOB)["state"] == "stop_unconfirmed"
    assert sandbox.record(JOB)["container_id"] == ORIGINAL
    assert not docker.running[ORIGINAL] and docker.running[REPLACEMENT]
    mutations = [call for call in docker.calls if call[1] in MUTATIONS]
    assert [call[1] for call in mutations] == ["kill", "wait"]
    assert all(call[-1] == ORIGINAL for call in mutations)
    with pytest.raises(SandboxError, match="release"):
        sandbox.dispose(JOB)
    assert not any(call[1] == "rm" for call in docker.calls)


def test_dispose_after_name_replacement_removes_only_original_id(tmp_path, monkeypatch):
    docker = DockerFixture(running=False)
    sandbox = sandbox_at(tmp_path, docker, state="cancelled")
    monkeypatch.setattr(sandbox, "_unmount_workspace", lambda _: None)
    docker.after_inspect = lambda: setattr(docker, "named", REPLACEMENT)
    assert sandbox.dispose(JOB)["state"] == "disposed"
    assert [call for call in docker.calls if call[1] == "rm"] == [("docker", "rm", ORIGINAL)]
    assert docker.running[REPLACEMENT]


def test_launch_starts_created_persisted_id_even_if_name_is_reused(tmp_path, monkeypatch):
    docker = DockerFixture(named=REPLACEMENT, labeled=[REPLACEMENT])
    sandbox = CodingSandbox(tmp_path, IMAGE, {"product": tmp_path}, execute=docker)
    monkeypatch.setattr(sandbox, "_require_work_root", lambda: None)
    monkeypatch.setattr(sandbox, "preflight", lambda: True)
    monkeypatch.setattr(sandbox, "prepare", lambda *_: tmp_path / "workspace")

    def check(operation, identity):
        assert operation == "start" and identity == ORIGINAL
        assert sandbox.record(JOB)["container_id"] == ORIGINAL

    docker.before_mutation = check
    assert sandbox.launch(JOB, 1, "product", "a" * 40, "pass")["state"] == "running"
    assert [call for call in docker.calls if call[1] == "start"] == [("docker", "start", ORIGINAL)]


@pytest.mark.parametrize("created_id", ["", ORIGINAL[:12]])
def test_launch_with_unconfirmed_create_identity_never_starts_by_name(tmp_path, monkeypatch, created_id):
    docker = DockerFixture(named=REPLACEMENT, labeled=[REPLACEMENT])

    def execute(*args, **kwargs):
        result = docker(*args, **kwargs)
        if args[:2] == ("docker", "create"):
            result.stdout = created_id
        return result

    sandbox = CodingSandbox(tmp_path, IMAGE, {"product": tmp_path}, execute=execute)
    monkeypatch.setattr(sandbox, "_require_work_root", lambda: None)
    monkeypatch.setattr(sandbox, "preflight", lambda: True)
    monkeypatch.setattr(sandbox, "prepare", lambda *_: tmp_path / "workspace")
    with pytest.raises(SandboxError, match="identity"):
        sandbox.launch(JOB, 1, "product", "a" * 40, "pass")
    assert sandbox.record(JOB)["state"] == "stop_unconfirmed"
    assert not any(call[1] in MUTATIONS for call in docker.calls)
