"""Real Runner API/SQLite lifecycle tests; no cloud credentials."""
import hashlib
import time

import httpx
import pytest
from agentic_runner.app import Broker, create_api


def test_reconciliation_requires_kernel_proof_not_different_container(monkeypatch):
    from agentic_runner import reconcile
    previous = {"boot_id": "boot-a", "pid_namespace": 100, "init_start": 99}
    monkeypatch.setattr(reconcile, "provider_processes_absent", lambda uids: False)
    assert not reconcile.local_proof(previous, previous, {1001})
    monkeypatch.setattr(reconcile, "provider_processes_absent", lambda uids: True)
    assert reconcile.local_proof(previous, previous, {1001})
    assert not reconcile.local_proof(previous, {**previous, "pid_namespace": 101}, {1001})
    assert reconcile.local_proof(previous, {**previous, "boot_id": "boot-b"}, {1001})
    assert not reconcile.local_proof(None, previous, {1001})


async def test_quarantine_reconciliation_never_reruns_job(broker, monkeypatch):
    from agentic_runner import providers
    broker.providers.quarantine("fixture_uncertain")
    broker.save_job({"id": "b" * 32, "digest": "stable", "state": "stop_unconfirmed", "provider": "codex",
                     "created": time.time()})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_api(broker)), base_url="https://runner",
                                 headers={"Authorization": "Bearer fixture-token"}) as client:
        monkeypatch.setattr(providers, "local_proof", lambda *args: False)
        assert (await client.post("/reconcile")).status_code == 409
        assert broker.job("b" * 32)["state"] == "stop_unconfirmed"
        monkeypatch.setattr(providers, "local_proof", lambda *args: True)
        response = await client.post("/reconcile")
        assert response.json() == {"execution_state": "idle", "stop_confirmed": True, "replayed": False}
        assert broker.job("b" * 32)["state"] == "failed" and not broker.tasks


@pytest.fixture
def broker(tmp_path):
    broker = Broker(tmp_path / "runner")
    broker.settings({"token_hash": hashlib.sha256(b"fixture-token").hexdigest()})
    class LocalCodingFixture:
        def handle(self, method, path, body=None):
            assert method == "POST" and path == "/coding/retention"
            return 200, body
    broker.home_coding = LocalCodingFixture()
    yield broker
    broker.db.close()


def completed(broker, *, age_days=8):
    job = {"id": "a" * 32, "digest": "fixture-digest", "state": "succeeded", "provider": "codex",
           "created": time.time() - age_days * 86400, "result": {"private": "fixture-household-content"}}
    # Simulate an older installed ledger, before retained_until was introduced.
    import json
    with broker.db:
        broker.db.execute("INSERT INTO jobs VALUES (?,?,?)", (job["id"], job["digest"], json.dumps(job)))
    return job


async def test_idle_get_expires_context_and_keeps_identity_without_new_job(broker):
    original = completed(broker)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_api(broker)), base_url="https://runner",
                                 headers={"Authorization": "Bearer fixture-token"}) as client:
        result = await client.get("/jobs/" + original["id"])
        assert result.status_code == 200
        item = result.json()
        assert item["state"] == "expired" and item["content_expired"] and item["stop_confirmed"]
        assert item["digest"] == original["digest"] and "result" not in item
        assert "fixture-household-content" not in str(broker.db.execute("SELECT body FROM jobs").fetchall())


async def test_lowering_retention_is_immediate_and_increasing_cannot_resurrect(broker):
    original = completed(broker, age_days=2)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_api(broker)), base_url="https://runner",
                                 headers={"Authorization": "Bearer fixture-token"}) as client:
        assert (await client.get("/jobs/" + original["id"])).json()["state"] == "succeeded"
        assert (await client.post("/retention", json={"context_days": 1})).status_code == 200
        erased = (await client.get("/jobs/" + original["id"])).json()
        assert erased["state"] == "expired"
        await client.post("/retention", json={"context_days": 7})
        assert (await client.get("/jobs/" + original["id"])).json() == erased
        broker.save_job({**original, "state": "succeeded", "result": {"private": "late-result"}})
        assert broker.job(original["id"])["state"] == "expired"
        assert "result" not in broker.job(original["id"])


def test_retention_deadline_and_tombstone_survive_runner_restart(tmp_path):
    root = tmp_path / "runner"
    first = Broker(root)
    completed(first, age_days=2)
    first.settings({"context_days": 1})
    first.prune()
    expiry = first.job("a" * 32)["retained_until"]
    first.db.close()
    second = Broker(root)
    try:
        assert second.setting("context_days") == 1
        assert second.job("a" * 32)["state"] == "expired"
        second.settings({"context_days": 7})
        assert second.job("a" * 32)["retained_until"] == expiry
    finally:
        second.db.close()


async def test_retention_requires_paired_authority_and_valid_range(broker):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_api(broker)), base_url="https://runner") as client:
        assert (await client.post("/retention", json={"context_days": 1})).status_code == 401
        client.headers["Authorization"] = "Bearer fixture-token"
        for value in (0, 8, True):
            assert (await client.post("/retention", json={"context_days": value})).status_code == 422
