"""Stopped-checkpoint transport fixtures; no CLI, network or household effects."""
import copy
import hashlib
import time
from types import SimpleNamespace

import pytest
from agentic_controller.hermes import Coordinator
from agentic_controller.work import WorkerStart
from agentic_controller.workers import WorkerError
from test_worker_controller import CodingRunner, owner

BASE_COMMIT = "b" * 40


class HandoffRunner(CodingRunner):
    def __init__(self, worker):
        super().__init__(worker)
        self.observe = lambda method, path, response: None

    async def call(self, method, path, body=None):
        if method == "POST" and path.endswith("/handoff"):
            self.calls.append((method, path, body))
            job = self.jobs[path.split("/")[3]]
            job.update(state="cancelled", stop_confirmed=True)
            response = {**copy.deepcopy(job), "live_session": False, "checkpoint": {
                "base_commit": job.get("base_commit"), "artifact_ids": [a["id"] for a in job["artifacts"]]}}
        else:
            response = copy.deepcopy(await super().call(method, path, body))
        self.observe(method, path, response)
        return response


@pytest.fixture
async def handoff_system(store):
    workers = Coordinator(store).worker_manager
    workers.closing = True
    workers.runners = {"host": HandoffRunner("host-coder"), "haos": HandoffRunner("home-coder")}

    async def start(count=1, worker="host-coder", base_commit=BASE_COMMIT):
        job = await workers.submit(WorkerStart(worker_id=worker, mode="research", goal="Inspect fixture"),
                                   "handoff-first", request=owner())
        await workers.advance()
        runner = workers.runner(worker)
        artifacts = [{"id": hashlib.sha256(f"{job['id']}:{n}".encode()).hexdigest()[:32],
                      "name": f"evidence-{n}.txt", "kind": "withheld" if n == 0 else "evidence",
                      "media_type": "text/plain", "size": 7, "sha256": hashlib.sha256(b"fixture").hexdigest()}
                     for n in range(count)]
        runner.jobs[job["id"]].update(base_commit=base_commit, artifacts=artifacts)
        return SimpleNamespace(job=job, runner=runner, artifacts=artifacts)

    yield SimpleNamespace(workers=workers, store=store, start=start)
    await workers.close()


@pytest.mark.parametrize("worker", ["host-coder", "home-coder"])
@pytest.mark.parametrize("count", [0, 16, 17, 32])
async def test_handoff_exports_every_accepted_artifact_in_order(handoff_system, worker, count):
    system = handoff_system
    started = await system.start(count, worker)
    result = await system.workers.handoff(started.job["id"])
    assert result == {"id": started.job["id"], "lease_epoch": 1, "stop_confirmed": True,
                      "live_session": False, "checkpoint": {"base_commit": BASE_COMMIT,
                          "artifact_ids": [a["id"] for a in started.artifacts]}}
    assert len(system.store.list("worker_artifact")) == count
    assert system.workers.get(started.job["id"])["state"] == "cancelled"
    assert system.store.setting("worker_lease") is None


async def test_handoff_tolerates_additional_checkpoint_metadata(handoff_system):
    started = await handoff_system.start()

    def observe(method, path, response):
        if path.endswith("/handoff"):
            response["checkpoint"]["future_metadata"] = {"version": 2, "opaque": ["fixture"]}

    started.runner.observe = observe
    result = await handoff_system.workers.handoff(started.job["id"])
    assert result["checkpoint"]["artifact_ids"] == [started.artifacts[0]["id"]]
    assert result["live_session"] is False


@pytest.mark.parametrize("checkpoint", [None, [], "invalid", 0, True])
async def test_non_mapping_checkpoint_is_a_protocol_error(handoff_system, checkpoint):
    started = await handoff_system.start()

    def observe(method, path, response):
        if path.endswith("/handoff"):
            response["checkpoint"] = checkpoint

    started.runner.observe = observe
    with pytest.raises(WorkerError, match="invalid_handoff_checkpoint") as error:
        await handoff_system.workers.handoff(started.job["id"])
    assert error.value.status == 502


@pytest.mark.parametrize("artifact_ids", [None, "a" * 32, {"id": "a" * 32},
    [[]], [{}], [None], [1], [True], ["not-an-id"], ["A" * 32], ["1" * 31],
    ["a" * 32, "a" * 32], [f"{n:032x}" for n in range(33)]])
async def test_invalid_checkpoint_ids_are_protocol_errors(handoff_system, artifact_ids):
    started = await handoff_system.start()

    def observe(method, path, response):
        if path.endswith("/handoff"):
            response["checkpoint"]["artifact_ids"] = artifact_ids

    started.runner.observe = observe
    with pytest.raises(WorkerError, match="invalid_handoff_checkpoint") as error:
        await handoff_system.workers.handoff(started.job["id"])
    assert error.value.status == 502


async def test_duplicate_accepted_artifact_is_not_a_valid_checkpoint(handoff_system):
    started = await handoff_system.start()

    def observe(method, path, response):
        if path.endswith("/handoff"):
            response["checkpoint"]["artifact_ids"] *= 2

    started.runner.observe = observe
    with pytest.raises(WorkerError, match="invalid_handoff_checkpoint"):
        await handoff_system.workers.handoff(started.job["id"])


@pytest.mark.parametrize("same_case", [False, True])
async def test_checkpoint_cannot_reference_another_jobs_artifact(handoff_system, same_case):
    system = handoff_system
    started = await system.start()
    foreign = {**started.artifacts[0], "id": "f" * 32, "job_id": "e" * 32,
               "case_id": started.job["case_id"] if same_case else "d" * 32}
    system.store.put("worker_artifact", foreign)

    def observe(method, path, response):
        if path.endswith("/handoff"):
            response["checkpoint"]["artifact_ids"] = [foreign["id"]]

    started.runner.observe = observe
    with pytest.raises(WorkerError, match="invalid_handoff_checkpoint"):
        await system.workers.handoff(started.job["id"])
    assert system.store.get("worker_artifact", foreign["id"]) == foreign


async def test_checkpoint_base_must_match_the_accepted_job(handoff_system):
    started = await handoff_system.start()

    def observe(method, path, response):
        if path.endswith("/handoff"):
            response["checkpoint"]["base_commit"] = "c" * 40

    started.runner.observe = observe
    with pytest.raises(WorkerError, match="invalid_handoff_checkpoint"):
        await handoff_system.workers.handoff(started.job["id"])


@pytest.mark.parametrize("phase", ["before", "post", "get"])
@pytest.mark.parametrize("worker", ["host-coder", "home-coder"])
async def test_peer_replacement_fences_each_handoff_await(handoff_system, phase, worker):
    system = handoff_system
    started = await system.start(worker=worker)
    peer_key = "runner" if worker == "host-coder" else "runner_haos"

    def rotate():
        system.store.set_setting(peer_key, {"host": "replacement"})

    def observe(method, path, response):
        if (phase == "post" and path.endswith("/handoff")) or (phase == "get" and method == "GET"):
            rotate()

    started.runner.calls.clear()
    started.runner.observe = observe
    if phase == "before":
        rotate()
    with pytest.raises(WorkerError, match="runner_identity_changed"):
        await system.workers.handoff(started.job["id"])
    assert [call[0] for call in started.runner.calls] == {
        "before": [], "post": ["POST"], "get": ["POST", "GET"]}[phase]
    assert system.store.setting("worker_lease")["job_id"] == started.job["id"]
    assert system.store.list("worker_artifact") == []


@pytest.mark.parametrize("field,value", [("stop_confirmed", False), ("live_session", True),
    ("id", "f" * 32), ("lease_epoch", 2)])
async def test_handoff_requires_exact_stop_proof(handoff_system, field, value):
    system = handoff_system
    started = await system.start()

    def observe(method, path, response):
        if path.endswith("/handoff"):
            response[field] = value

    started.runner.calls.clear()
    started.runner.observe = observe
    with pytest.raises(WorkerError, match="handoff_stop_unconfirmed"):
        await system.workers.handoff(started.job["id"])
    assert [call[0] for call in started.runner.calls] == ["POST"]
    assert system.store.setting("worker_lease")["job_id"] == started.job["id"]


@pytest.mark.parametrize("stopped", [False, True])
async def test_second_receipt_must_confirm_a_terminal_stop(handoff_system, stopped):
    system = handoff_system
    started = await system.start()

    def observe(method, path, response):
        if method == "GET":
            response.update(state="running" if stopped else "stop_unconfirmed", stop_confirmed=stopped)

    started.runner.observe = observe
    with pytest.raises(WorkerError, match="invalid_handoff_checkpoint"):
        await system.workers.handoff(started.job["id"])
    assert system.store.setting("worker_lease")["job_id"] == started.job["id"]


@pytest.mark.parametrize("phase", ["post", "get"])
@pytest.mark.parametrize("count", [0, 1])
async def test_retention_expiry_during_handoff_cannot_return_a_checkpoint(handoff_system, phase, count):
    system = handoff_system
    started = await system.start(count=count, base_commit=None if count == 0 else BASE_COMMIT)

    def observe(method, path, response):
        if (phase == "post" and path.endswith("/handoff")) or (phase == "get" and method == "GET"):
            job = system.store.get("work_job", started.job["id"])
            system.store.put("work_job", {**job, "created_epoch": time.time() - 2 * 86400})
            system.store.set_setting("retention", {"context_days": 1, "audit_days": 30})

    started.runner.observe = observe
    with pytest.raises(WorkerError, match="worker_job_expired") as error:
        await system.workers.handoff(started.job["id"])
    assert error.value.status == 410
    job = system.workers.get(started.job["id"])
    assert job["content_expired"] and job["artifacts"] == [] and "base_commit" not in job
    assert system.store.list("worker_artifact") == []
