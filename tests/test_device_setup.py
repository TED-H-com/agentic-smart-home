"""Native IPP API contract fixtures; no real printer, HAOS, or account acceptance."""
import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from agentic_controller.device_setup import (
    KIND,
    DeviceSetupApprove,
    DeviceSetupCreate,
    DeviceSetupReconcile,
    DeviceSetups,
)
from agentic_controller.ha import HAError, HAUnreachable
from agentic_controller.models import utcnow
from agentic_controller.store import Store
from fastapi import HTTPException
from starlette.requests import Request


class IPPHA:
    token, base, websocket = "private-token", "http://fixture/api", "ws://fixture/websocket"

    def __init__(self):
        self.revoked = False
        self.posts = []
        self.backups = []
        self.lose_reply = False
        self.reject_backup = False
        self.backup_hook = None
        self.form_hook = None
        self.post_hook = None
        self.config = {"version": "2026.10.1"}
        self.flow = {"flow_id": "pending", "handler": "ipp", "step_id": "zeroconf_confirm", "context": {
            "source": "zeroconf", "unique_id": "private-serial", "title_placeholders": {"name": "Office printer"}}}
        self.form = {"type": "form", "flow_id": "pending", "handler": "ipp", "step_id": "zeroconf_confirm",
                     "data_schema": [], "errors": {}, "description_placeholders": {"name": "Office printer"}}
        self.entries = []
        self.devices = []
        self.entities = []
        self.states = []

    async def owner(self, owner):
        return {"id": "owner", "is_owner": True} if owner == "owner" and not self.revoked else None

    async def ws(self, target):
        return copy.deepcopy({"config_entries/flow/progress": [self.flow] if self.flow else [],
            "config_entries/get": self.entries, "config/device_registry/list": self.devices,
            "config/entity_registry/list": self.entities}[target])

    def created(self):
        entry = {"entry_id": "newentry", "domain": "ipp", "state": "loaded", "title": "Office printer",
                 "source": "zeroconf"}
        self.entries.append(entry)
        self.devices.append({"id": "printer", "name": "Office printer", "config_entries": ["newentry"],
                             "identifiers": [["ipp", "private-serial"]]})
        self.entities.append({"entity_id": "sensor.ink", "device_id": "printer", "config_entry_id": "newentry"})
        self.states.append({"entity_id": "sensor.ink", "state": "90"})
        self.flow = None
        return entry

    async def request(self, method, target, data=None, *, send_guard=None):
        if method == "GET":
            if target == "/config":
                return copy.deepcopy(self.config)
            if target == "/states":
                return copy.deepcopy(self.states)
            assert target == "/config/config_entries/flow/" + self.form["flow_id"]
            if self.form_hook:
                await self.form_hook()
            return copy.deepcopy(self.form)
        assert method == "POST" and target == "/config/config_entries/flow/" + self.form["flow_id"] and data == {}
        assert send_guard
        if self.post_hook:
            await self.post_hook()
        await send_guard()
        self.posts.append((target, data))
        entry = self.created()
        if self.lose_reply:
            raise HAUnreachable("private connection detail")
        return {"type": "create_entry", "flow_id": self.form["flow_id"], "handler": "ipp", "result": entry,
                "data": {"secret": "do-not-persist"}}

    async def backup(self, name, *, send_guard):
        await send_guard()
        if self.reject_backup:
            raise HAError("private backup problem")
        self.backups.append(name)
        if self.backup_hook:
            await self.backup_hook()
        return {"id": "fixture-backup", "name": name, "created_at": utcnow()}


@pytest.fixture
def setup_service(store):
    store.set_setting("csrf_secret", "11" * 32)
    store.set_setting("owner", "owner")
    ha = IPPHA()
    app = SimpleNamespace(state=SimpleNamespace())
    session = SimpleNamespace(active=True)
    session.verify = lambda _request: session.active
    return DeviceSetups(app, store, ha, session), ha, store, session


def request(key="one"):
    return Request({"type": "http", "method": "POST", "path": "/api/device-setups", "headers": [
        (b"idempotency-key", key.encode())], "state": {"principal": "owner"}})


async def propose(system, key="one"):
    return await system[0].create(request(key), DeviceSetupCreate(candidate_id="pending"))


async def approve(system, proposal, key="approve"):
    return await system[0].approve(request(key), proposal.id, DeviceSetupApprove(
        expected_revision=proposal.revision, proposal_digest=proposal.proposal_digest))


async def finish(system):
    await asyncio.gather(*list(system[0].running.values()))


async def test_proposal_has_no_effect_then_exact_approval_backup_post_and_readback(setup_service):
    service, ha, store, _ = setup_service
    policy = store.setting("policy")
    proposal = await propose(setup_service)
    assert proposal.state == "proposed" and proposal.can_approve
    assert ha.backups == [] and ha.posts == []
    assert (await approve(setup_service, proposal)).state == "preparing"
    await finish(setup_service)
    result = await service.detail(request(), proposal.id)
    assert result.state == "configured" and result.integration.id == "newentry"
    assert (result.device_count, result.entity_count, result.available_entity_count) == (1, 1, 1)
    assert result.physical_verification == "not_performed"
    assert result.backup_scope == "home_assistant_configuration" and result.backup_id == "fixture-backup"
    assert len(ha.posts) == len(ha.backups) == 1
    assert store.setting("policy") == policy
    assert (store.path.parent / "snapshots" / (proposal.id + ".sqlite3")).is_file()
    raw = json.dumps(store.get(KIND, proposal.id)) + result.model_dump_json()
    assert all(secret not in raw for secret in ("private-token", "private-serial", "do-not-persist", "fixture/api"))
    assert [item.id for item in (await service.listing(request())).setups] == [proposal.id]


@pytest.mark.parametrize("change,reason", [
    ("version", "device_setup_core_unsupported"),
    ("source", "device_setup_flow_unsupported"),
    ("handler", "device_setup_flow_unsupported"),
    ("step", "device_setup_flow_unsupported"),
    ("identity", "device_setup_identity_unavailable"),
    ("fields", "device_setup_flow_unsupported"),
    ("errors", "device_setup_flow_unsupported"),
])
async def test_unknown_or_nonempty_flow_cannot_be_proposed(setup_service, change, reason):
    _, ha, _, _ = setup_service
    if change == "version":
        ha.config["version"] = "2026.11.0"
    elif change == "source":
        ha.flow["context"]["source"] = "user"
    elif change == "handler":
        ha.flow["handler"] = "other"
    elif change == "step":
        ha.flow["step_id"] = "user"
    elif change == "identity":
        ha.flow["context"].pop("unique_id")
    elif change == "fields":
        ha.form["data_schema"] = [{"name": "password", "required": True}]
    else:
        ha.form["errors"] = {"base": "cannot_connect"}
    with pytest.raises(HTTPException) as exc:
        await propose(setup_service)
    assert exc.value.detail == reason
    assert not ha.posts and not ha.backups


async def test_exact_approval_and_idempotency_prevent_duplicate_effects(setup_service):
    service, ha, _, _ = setup_service
    proposal = await propose(setup_service)
    assert (await propose(setup_service, "other-key")).id == proposal.id
    with pytest.raises(HTTPException):
        await service.approve(request("bad"), proposal.id,
            DeviceSetupApprove(expected_revision=1, proposal_digest="00" * 32))
    await approve(setup_service, proposal)
    await finish(setup_service)
    assert (await approve(setup_service, proposal)).state == "configured"
    with pytest.raises(HTTPException):
        await approve(setup_service, proposal, "another-approval")
    assert len(ha.posts) == 1


@pytest.mark.parametrize("change", ["identity", "title", "form", "owner", "token", "session", "update", "entries"])
async def test_backup_await_changes_fence_confirmation(setup_service, change):
    _, ha, store, session = setup_service
    proposal = await propose(setup_service)
    async def change_after_backup():
        if change == "identity":
            ha.flow["context"]["unique_id"] = "replacement"
        elif change == "title":
            ha.flow["context"]["title_placeholders"]["name"] = "Replacement printer"
        elif change == "form":
            ha.form["description_placeholders"]["name"] = "Replacement printer"
        elif change == "owner":
            ha.revoked = True
        elif change == "token":
            ha.token = "replacement-token"
        elif change == "session":
            session.active = False
        elif change == "update":
            store.set_setting("updater_gate", True)
        else:
            ha.entries.append({"entry_id": "concurrent", "domain": "ipp", "state": "loaded"})
    ha.backup_hook = change_after_backup
    await approve(setup_service, proposal)
    await finish(setup_service)
    assert not ha.posts
    assert store.get(KIND, proposal.id)["state"] == "blocked"


async def test_failed_backup_does_not_send_or_claim_restore_point(setup_service):
    _, ha, store, _ = setup_service
    ha.reject_backup = True
    proposal = await propose(setup_service)
    await approve(setup_service, proposal)
    await finish(setup_service)
    record = store.get(KIND, proposal.id)
    assert record["state"] == "blocked" and record["backup_id"] is None
    assert ha.posts == [] and "private backup problem" not in json.dumps(record)


async def test_lost_reply_reconciles_only_exact_registry_identity_without_reposting(setup_service):
    service, ha, store, _ = setup_service
    ha.lose_reply = True
    proposal = await propose(setup_service)
    await approve(setup_service, proposal)
    await finish(setup_service)
    record = store.get(KIND, proposal.id)
    assert record["state"] == "needs_reconcile" and record["entry_id"] is None
    result = await service.reconcile(request("reconcile"), proposal.id,
                                    DeviceSetupReconcile(expected_revision=record["revision"]))
    assert result.state == "configured"
    assert store.get(KIND, proposal.id)["entry_id"] == "newentry"
    assert len(ha.posts) == 1


@pytest.mark.parametrize("change", ["no_device", "other_identity", "ambiguous", "preexisting"])
async def test_unknown_outcome_cannot_guess_from_new_entry_title(setup_service, change):
    service, ha, store, _ = setup_service
    ha.lose_reply = True
    if change == "preexisting":
        ha.entries = [{"entry_id": "old", "domain": "ipp", "state": "loaded", "source": "zeroconf"}]
    proposal = await propose(setup_service)
    await approve(setup_service, proposal)
    await finish(setup_service)
    if change == "no_device":
        ha.devices = []
    elif change == "other_identity":
        ha.devices[0]["identifiers"] = [["ipp", "someone-else"]]
    elif change == "ambiguous":
        ha.entries.append({"entry_id": "second", "domain": "ipp", "state": "loaded", "source": "zeroconf"})
        ha.devices[0]["config_entries"].append("second")
    else:
        ha.devices[0]["config_entries"] = ["old"]
    record = store.get(KIND, proposal.id)
    result = await service.reconcile(request("reconcile"), proposal.id,
                                    DeviceSetupReconcile(expected_revision=record["revision"]))
    assert result.state == "needs_reconcile" and result.integration is None
    assert len(ha.posts) == 1


async def test_restart_preserves_intent_and_never_replays_confirmation(setup_service):
    service, ha, store, _ = setup_service
    ha.lose_reply = True
    proposal = await propose(setup_service)
    await approve(setup_service, proposal)
    await finish(setup_service)
    record = service.record(store.get(KIND, proposal.id)["binding"], proposal.id)
    service.save(record, state="applying")
    await store.durable()
    reopened = Store(store.path)
    try:
        recovered = DeviceSetups(service.app, reopened, ha, service.host_session)
        recovered.recover()
        assert reopened.get(KIND, proposal.id)["state"] == "needs_reconcile"
        assert not recovered.running and len(ha.posts) == 1
    finally:
        reopened.close()


async def test_background_acceptance_returns_before_long_backup(setup_service):
    service, ha, _, _ = setup_service
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow_backup():
        entered.set()
        await release.wait()
    ha.backup_hook = slow_backup
    proposal = await propose(setup_service)
    accepted = await asyncio.wait_for(approve(setup_service, proposal), 2)
    assert accepted.state == "preparing"
    await asyncio.wait_for(entered.wait(), 2)
    assert service.running and not ha.posts
    release.set()
    await finish(setup_service)


async def test_changed_connection_hides_private_setup(setup_service):
    service, ha, _, _ = setup_service
    proposal = await propose(setup_service)
    ha.token = "new-token"
    assert (await service.listing(request())).setups == []
    with pytest.raises(HTTPException) as exc:
        await service.detail(request(), proposal.id)
    assert exc.value.status_code == 404


async def test_new_flow_cannot_bypass_an_uncertain_earlier_device_identity(setup_service):
    service, ha, store, _ = setup_service
    ha.lose_reply = True
    proposal = await propose(setup_service)
    old_flow = copy.deepcopy(ha.flow)
    await approve(setup_service, proposal)
    await finish(setup_service)
    ha.flow = {**old_flow, "flow_id": "rediscovered"}
    ha.form["flow_id"] = "rediscovered"
    resumed = await service.create(request("fresh-key"), DeviceSetupCreate(candidate_id="rediscovered"))
    assert resumed.id == proposal.id and resumed.state == "needs_reconcile"
    assert resumed.candidate.id == "pending" and resumed.candidate_aliases == ["rediscovered"]
    assert store.get(KIND, proposal.id)["phase"] == "confirmation_prepared"
    assert len(ha.posts) == 1


async def test_waiting_writer_cancellation_settles_accepted_operation(setup_service):
    service, ha, store, _ = setup_service
    proposal = await propose(setup_service)
    await service.writer.acquire()
    try:
        await approve(setup_service, proposal)
        await asyncio.sleep(0)
        task = service.running[proposal.id]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    finally:
        service.writer.release()
    assert store.get(KIND, proposal.id)["state"] == "blocked"
    assert not ha.posts and not ha.backups


async def test_approval_revocation_during_disk_wait_never_leaves_preparing(setup_service, monkeypatch):
    service, ha, store, _ = setup_service
    proposal = await propose(setup_service)
    original = store.durable
    async def revoke():
        await original()
        ha.revoked = True
    monkeypatch.setattr(store, "durable", revoke)
    with pytest.raises(HTTPException):
        await approve(setup_service, proposal)
    assert store.get(KIND, proposal.id)["state"] == "blocked"
    assert not service.running and not ha.posts and not ha.backups


async def test_shared_device_does_not_inflate_created_entry_entity_readback(setup_service):
    service, ha, _, _ = setup_service
    proposal = await propose(setup_service)
    await approve(setup_service, proposal)
    await finish(setup_service)
    ha.entities.append({"entity_id": "sensor.other", "config_entry_id": "other", "device_id": "printer"})
    ha.states.append({"entity_id": "sensor.other", "state": "on"})
    result = await service.detail(request(), proposal.id)
    assert result.entity_count == 1 and result.available_entity_count == 1
    assert result.devices[0].entity_count == 1


async def test_retention_redacts_labels_but_keeps_uncertain_write_fence(setup_service):
    service, ha, store, _ = setup_service
    proposal = await propose(setup_service)
    record = service.record(store.get(KIND, proposal.id)["binding"], proposal.id)
    old = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    record = service.save(record, state="needs_reconcile", created_at=old, phase="confirmation_prepared")
    service.prune()
    saved = store.get(KIND, proposal.id)
    assert saved["identity_digest"] == record.identity_digest
    assert saved["state"] == "needs_reconcile"
    assert "Office printer" not in json.dumps(saved)
    assert (await propose(setup_service, "new-key")).id == proposal.id
    assert not ha.posts


async def test_final_transport_guard_proves_unsent_and_allows_new_review(setup_service):
    service, ha, store, _ = setup_service
    proposal = await propose(setup_service)
    async def pause_before_transport():
        store.set_setting("paused", True)
    ha.post_hook = pause_before_transport
    await approve(setup_service, proposal)
    await finish(setup_service)
    assert not ha.posts
    assert store.get(KIND, proposal.id)["state"] == "blocked"
    store.set_setting("paused", False)
    assert (await propose(setup_service, "new-review")).id != proposal.id


@pytest.mark.parametrize("phase", ["approved", "backup_prepared", "backup_completed"])
async def test_restart_of_durable_preconfirmation_is_safe_to_review_again(setup_service, phase):
    service, ha, store, _ = setup_service
    proposal = await propose(setup_service)
    record = service.record(store.get(KIND, proposal.id)["binding"], proposal.id)
    service.save(record, state="preparing", phase=phase)
    service.recover()
    assert store.get(KIND, proposal.id)["state"] == "blocked"
    assert (await propose(setup_service, "new-review")).id != proposal.id
    assert not ha.posts


async def test_completed_then_removed_integration_can_be_reviewed_with_new_flow(setup_service):
    service, ha, _, _ = setup_service
    proposal = await propose(setup_service)
    flow = copy.deepcopy(ha.flow)
    await approve(setup_service, proposal)
    await finish(setup_service)
    ha.entries, ha.devices, ha.entities, ha.states = [], [], [], []
    ha.flow = {**flow, "flow_id": "rediscovered"}
    ha.form["flow_id"] = "rediscovered"
    replacement = await service.create(request("new-review"), DeviceSetupCreate(candidate_id="rediscovered"))
    assert replacement.id != proposal.id and replacement.state == "proposed"
    assert len(ha.posts) == 1
