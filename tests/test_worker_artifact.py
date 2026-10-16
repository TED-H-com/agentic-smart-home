"""Artifact transport fences with disk Store fixtures; no real worker or network."""
import asyncio
import base64
import time
from types import SimpleNamespace

import pytest
from agentic_controller.hermes import Coordinator
from agentic_controller.work import WorkerStart
from agentic_controller.workers import WorkerError
from test_worker_controller import CodingRunner, artifact, owner


@pytest.fixture
async def artifact_system(store):
    workers = Coordinator(store).worker_manager
    workers.closing = True
    workers.runners = {"host": CodingRunner("host-coder"), "haos": CodingRunner("home-coder")}

    async def completed(worker="host-coder"):
        job = await workers.submit(WorkerStart(worker_id=worker, mode="research", goal="Read fixture evidence"),
                                   "fixture-artifact", request=owner())
        await workers.advance()
        runner = workers.runner(worker)
        content = b"fixed fixture artifact"
        metadata = artifact(content, name="evidence.txt")
        runner.jobs[job["id"]].update(state="succeeded", stop_confirmed=True, artifacts=[metadata])
        runner.artifacts[metadata["id"]] = base64.b64encode(content).decode()
        await workers.advance()
        assert store.setting("worker_lease") is None
        return SimpleNamespace(store=store, workers=workers, runner=runner, job=job,
                               metadata=metadata, content=content,
                               peer_key="runner" if worker == "host-coder" else "runner_haos")

    yield completed
    await workers.close()


async def read_artifact(case):
    return await case.workers.artifact(case.job["id"], case.metadata["id"])


async def change_during_fetch(case, change):
    """Hold a real artifact call while an independent setting change commits."""
    started, release = asyncio.Event(), asyncio.Event()
    original = case.runner.call

    async def delayed(method, path, body=None):
        value = await original(method, path, body)
        assert method == "GET" and path.endswith("/artifacts/" + case.metadata["id"])
        started.set()
        await release.wait()
        return value

    case.runner.call = delayed
    task = asyncio.create_task(read_artifact(case))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        change()
        await case.store.durable()
        release.set()
        return await task
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("worker", ["host-coder", "home-coder"])
async def test_unchanged_peer_returns_verified_artifact(artifact_system, worker):
    case = await artifact_system(worker)
    result = await read_artifact(case)
    assert base64.b64decode(result["content_base64"]) == case.content
    assert result["sha256"] == case.metadata["sha256"]
    assert result["job_id"] == case.job["id"] and result["case_id"] == case.job["case_id"]


@pytest.mark.parametrize("worker", ["host-coder", "home-coder"])
async def test_replaced_peer_is_rejected_before_fetch(artifact_system, worker):
    case = await artifact_system(worker)
    before = len(case.runner.calls)
    case.store.set_setting(case.peer_key, {"host": "replacement"})
    with pytest.raises(WorkerError, match="runner_identity_changed"):
        await read_artifact(case)
    assert len(case.runner.calls) == before


@pytest.mark.parametrize("worker", ["host-coder", "home-coder"])
@pytest.mark.parametrize("replacement", [{"host": "replacement", "fingerprint": "f" * 64}, None],
                         ids=["repaired", "unpaired"])
async def test_same_worker_peer_changes_during_fetch_fence_delivery(artifact_system, worker, replacement):
    case = await artifact_system(worker)
    original_job = case.store.get("work_job", case.job["id"])
    with pytest.raises(WorkerError, match="runner_identity_changed"):
        await change_during_fetch(case, lambda: case.store.set_setting(case.peer_key, replacement))
    assert case.store.get("work_job", case.job["id"]) == original_job
    assert case.store.get("worker_artifact", case.metadata["id"])["sha256"] == case.metadata["sha256"]
    assert case.store.setting("worker_lease") is None


@pytest.mark.parametrize("worker", ["host-coder", "home-coder"])
async def test_other_workers_peer_change_does_not_block_artifact(artifact_system, worker):
    case = await artifact_system(worker)
    other_key = "runner_haos" if case.peer_key == "runner" else "runner"
    result = await change_during_fetch(case, lambda: case.store.set_setting(other_key, {"host": "other"}))
    assert base64.b64decode(result["content_base64"]) == case.content
    assert result["sha256"] == case.metadata["sha256"]


@pytest.mark.parametrize("change", ["nonobject", "wrong_artifact", "wrong_job", "base64"])
async def test_malformed_artifact_receipt_is_rejected(artifact_system, change):
    case = await artifact_system()
    original = case.runner.call

    async def malformed(method, path, body=None):
        value = await original(method, path, body)
        if change == "nonobject":
            return None
        field, replacement = {"wrong_artifact": ("id", "f" * 32), "wrong_job": ("job_id", "e" * 32),
                              "base64": ("content_base64", "not-base64%%")}[change]
        return {**value, field: replacement}

    case.runner.call = malformed
    with pytest.raises(WorkerError, match="invalid_artifact") as error:
        await read_artifact(case)
    assert error.value.status == 502


@pytest.mark.parametrize("change", ["size", "digest"])
async def test_changed_artifact_bytes_are_rejected(artifact_system, change):
    case = await artifact_system()
    changed = b"short" if change == "size" else bytes([case.content[0] ^ 1]) + case.content[1:]
    case.runner.artifacts[case.metadata["id"]] = base64.b64encode(changed).decode()
    with pytest.raises(WorkerError, match="artifact_digest_mismatch") as error:
        await read_artifact(case)
    assert error.value.status == 502


@pytest.mark.parametrize("phase", ["before", "during"])
@pytest.mark.parametrize("policy_key", ["retention", "retention_pending"])
async def test_retention_expiry_fences_artifact_and_keeps_tombstone(artifact_system, phase, policy_key):
    case = await artifact_system()

    def expire():
        job = case.store.get("work_job", case.job["id"])
        case.store.put("work_job", {**job, "created_epoch": time.time() - 2 * 86400})
        case.store.set_setting(policy_key, {"context_days": 1, "audit_days": 30})

    before = len(case.runner.calls)
    with pytest.raises(WorkerError, match="worker_job_expired") as error:
        if phase == "before":
            expire()
            await read_artifact(case)
        else:
            await change_during_fetch(case, expire)
    assert error.value.status == 410
    if phase == "before":
        assert len(case.runner.calls) == before
    job = case.workers.get(case.job["id"])
    assert job["content_expired"] and job["artifacts"] == []
    assert case.store.get("worker_artifact", case.metadata["id"]) is None
    case.store.set_settings({policy_key: {"context_days": 7, "audit_days": 30}})
    with pytest.raises(WorkerError, match="worker_job_expired"):
        await read_artifact(case)
