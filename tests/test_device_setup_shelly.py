"""Native Shelly 1 reported-model contract fixtures; no physical relay tests."""
import copy
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Literal

import pytest
from agentic_controller import device_setup
from agentic_controller.device_setup import (
    KIND,
    DeviceSetupCreate,
    DeviceSetupReconcile,
    DeviceSetups,
    SetupRecord,
)
from agentic_controller.store import Store
from fastapi import HTTPException
from pydantic import ValidationError
from test_device_setup import IPPHA, approve, finish, request

MAC = "AABBCCDDEEFF"


class ShellyHA(IPPHA):
    def __init__(self):
        super().__init__()
        self.gets = []
        self.flow.update(handler="shelly", step_id="confirm_discovery")
        self.flow["context"].update(unique_id=MAC, title_placeholders={"name": "Office relay"},
                                    configuration_url="http://192.0.2.25")
        self.form.update(handler="shelly", step_id="confirm_discovery",
                         description_placeholders={"model": "Shelly 1", "host": "192.0.2.25"})
        self.result_handler = "shelly"
        self.entry_domain = "shelly"

    def created(self):
        entry = {"entry_id": "newentry", "domain": self.entry_domain, "state": "loaded",
                 "title": "Office relay", "source": "zeroconf"}
        self.entries.append(entry)
        self.devices.append({"id": "relay", "name": "Office relay", "config_entries": ["newentry"],
            "identifiers": [["shelly", MAC]], "connections": [["mac", "aa:bb:cc:dd:ee:ff"]],
            "model": "Shelly 1", "model_id": "SHSW-1"})
        self.entities.append({"entity_id": "switch.relay", "device_id": "relay", "config_entry_id": "newentry"})
        self.states.append({"entity_id": "switch.relay", "state": "off"})
        self.flow = None
        return entry

    async def request(self, method, target, data=None, *, send_guard=None):
        if method == "GET":
            self.gets.append(target)
        result = await super().request(method, target, data, send_guard=send_guard)
        if method == "POST":
            result["handler"] = self.result_handler
        return result


@pytest.fixture
def shelly_service(store):
    store.set_setting("csrf_secret", "11" * 32)
    store.set_setting("owner", "owner")
    ha = ShellyHA()
    app = SimpleNamespace(state=SimpleNamespace())
    session = SimpleNamespace(active=True)
    session.verify = lambda _request: session.active
    return DeviceSetups(app, store, ha, session), ha, store, session


async def propose(system, key="proposal"):
    return await system[0].create(request(key), DeviceSetupCreate(candidate_id=system[1].form["flow_id"],
                                                                 integration_domain="shelly"))


async def reconcile(system, proposal, key="reconcile"):
    record = system[2].get(KIND, proposal.id)
    return await system[0].reconcile(request(key), proposal.id,
                                    DeviceSetupReconcile(expected_revision=record["revision"]))


@pytest.mark.parametrize("version", ["2026.10.1", "2026.10.2"])
async def test_exact_native_shelly_confirmation_backed_up_and_read_back(shelly_service, version):
    service, ha, store, _ = shelly_service
    ha.config["version"] = version
    before = store.setting("policy")
    proposal = await propose(shelly_service)
    assert proposal.candidate.integration_domain == "shelly" and proposal.can_approve
    assert "HA 回報為 Shelly 1" in proposal.message and "可能設定設備連線" in proposal.message
    assert "備份只涵蓋 HA 設定" in proposal.message and "不切換繼電器" in proposal.message
    assert not ha.posts and not ha.backups
    assert store.get(KIND, proposal.id)["version"] == 2
    await approve(shelly_service, proposal)
    await finish(shelly_service)
    detail = await service.detail(request(), proposal.id)
    assert detail.state == "configured" and detail.integration.domain == "shelly"
    assert (detail.device_count, detail.entity_count, detail.available_entity_count) == (1, 1, 1)
    assert detail.devices[0].name == "Office relay" and detail.devices[0].model == "Shelly 1"
    assert detail.physical_verification == "not_performed" and detail.backup_scope == "home_assistant_configuration"
    assert ha.posts == [("/config/config_entries/flow/pending", {})] and len(ha.backups) == 1
    assert ha.backups[0].startswith("Agentic Home Shelly 1 ")
    assert store.setting("policy") == before
    raw = json.dumps(store.get(KIND, proposal.id)) + detail.model_dump_json()
    assert all(private not in raw for private in (MAC, "192.0.2.25", "private-token", "do-not-persist"))


@pytest.mark.parametrize("model", ["Shelly Plus 1", "Shelly 1 Gen3", "Shelly 1 Mini Gen4", "Shelly Pro 1",
    "Shelly 1PM", "Shelly 2.5", "SHSW-1", "shelly 1", "Shelly 1 ", "unknown", None, {"model": "Shelly 1"}])
async def test_exact_reported_model_gate_excludes_unqualified_models(shelly_service, model):
    _, ha, _, _ = shelly_service
    ha.form["description_placeholders"]["model"] = model
    with pytest.raises(HTTPException) as exc:
        await propose(shelly_service)
    assert exc.value.detail == "device_setup_model_unsupported"
    assert not ha.posts and not ha.backups


@pytest.mark.parametrize("fault", ["credentials", "bluetooth", "user", "reauth", "provision_done", "handler", "mismatch"])
async def test_unknown_or_non_discovery_steps_are_rejected_before_form_get(shelly_service, fault):
    service, ha, _, _ = shelly_service
    if fault in {"credentials", "provision_done"}:
        ha.flow["step_id"] = fault
    elif fault in {"bluetooth", "user", "reauth"}:
        ha.flow["context"]["source"] = fault
    elif fault == "handler":
        ha.flow["handler"] = "synology_dsm"
    with pytest.raises(HTTPException) as exc:
        if fault == "mismatch":
            await service.create(request(), DeviceSetupCreate(candidate_id="pending", integration_domain="ipp"))
        else:
            await propose(shelly_service)
    assert exc.value.detail == "device_setup_flow_unsupported"
    assert all("/flow/" not in target for target in ha.gets) and not ha.posts and not ha.backups


@pytest.mark.parametrize("fault", ["credentials", "error", "step", "domain", "no_model", "invalid_placeholders"])
async def test_only_empty_error_free_exact_confirmation_form_is_supported(shelly_service, fault):
    _, ha, _, _ = shelly_service
    if fault == "credentials":
        ha.form["data_schema"] = [{"name": "password", "required": True, "type": "string"}]
    elif fault == "error":
        ha.form["errors"] = {"base": "cannot_connect"}
    elif fault == "step":
        ha.form["step_id"] = "credentials"
    elif fault == "domain":
        ha.form["handler"] = "ipp"
    elif fault == "no_model":
        ha.form["description_placeholders"].pop("model")
    else:
        ha.form["description_placeholders"] = "Shelly 1"
    with pytest.raises(HTTPException) as exc:
        await propose(shelly_service)
    assert exc.value.detail == ("device_setup_model_unsupported" if fault in {"no_model", "invalid_placeholders"}
                                else "device_setup_flow_unsupported")
    assert not ha.posts and not ha.backups


@pytest.mark.parametrize("mac", [None, "", "private-serial", "aa:bb:cc:dd:ee:ff", "AABBCCDDEEF", "AABBCCDDEEFF00",
    "AABBCCDDEEFG", 42])
async def test_shelly_identity_is_exact_native_mac_format_before_any_form_get(shelly_service, mac):
    _, ha, _, _ = shelly_service
    ha.flow["context"]["unique_id"] = mac
    with pytest.raises(HTTPException) as exc:
        await propose(shelly_service)
    assert exc.value.detail == "device_setup_identity_unavailable"
    assert all("/flow/" not in target for target in ha.gets)


@pytest.mark.parametrize("change", ["model", "credentials", "flow_step", "identity", "owner", "token", "session", "update", "paused"])
async def test_post_backup_drift_or_revocation_blocks_final_send(shelly_service, change):
    _, ha, store, session = shelly_service
    proposal = await propose(shelly_service)
    async def hook():
        if change == "model":
            ha.form["description_placeholders"]["model"] = "Shelly Plus 1"
        elif change == "credentials":
            ha.form["data_schema"] = [{"name": "password", "type": "string"}]
        elif change == "flow_step":
            ha.flow["step_id"] = "credentials"
        elif change == "identity":
            ha.flow["context"]["unique_id"] = "001122334455"
        elif change == "owner":
            ha.revoked = True
        elif change == "token":
            ha.token = "rotated"
        elif change == "session":
            session.active = False
        elif change == "update":
            store.set_setting("host_bundle_update_gate", True)
        else:
            store.set_setting("paused", True)
    ha.backup_hook = hook
    await approve(shelly_service, proposal)
    await finish(shelly_service)
    assert not ha.posts and store.get(KIND, proposal.id)["state"] == "blocked"


async def test_last_transport_guard_checks_shelly_model_and_can_prove_unsent(shelly_service):
    _, ha, store, _ = shelly_service
    proposal = await propose(shelly_service)
    async def change_model():
        ha.form["description_placeholders"]["model"] = "Shelly Plus 1"
    ha.post_hook = change_model
    await approve(shelly_service, proposal)
    await finish(shelly_service)
    record = store.get(KIND, proposal.id)
    assert not ha.posts and record["state"] == "blocked" and record["reason_code"] == "device_setup_model_unsupported"


@pytest.mark.parametrize("registry_mac", [MAC, MAC.lower()])
async def test_lost_post_reconciles_exact_domain_and_normalized_mac_without_replay(shelly_service, registry_mac):
    _, ha, store, _ = shelly_service
    ha.flow["context"]["unique_id"] = MAC.lower()
    ha.lose_reply = True
    proposal = await propose(shelly_service)
    await approve(shelly_service, proposal)
    await finish(shelly_service)
    assert store.get(KIND, proposal.id)["state"] == "needs_reconcile"
    ha.devices[0]["identifiers"] = [["shelly", registry_mac]]
    result = await reconcile(shelly_service, proposal)
    assert result.state == "configured" and len(ha.posts) == 1


@pytest.mark.parametrize("fault", ["no_device", "wrong_domain", "only_mac_connection", "wrong_identity", "other_entry",
    "ambiguous", "wrong_source", "preexisting"])
async def test_unknown_outcome_never_guesses_from_title_address_or_shared_mac(shelly_service, fault):
    _, ha, _, _ = shelly_service
    ha.lose_reply = True
    if fault == "preexisting":
        ha.entries.append({"entry_id": "old", "domain": "shelly", "source": "zeroconf", "state": "loaded"})
    proposal = await propose(shelly_service)
    await approve(shelly_service, proposal)
    await finish(shelly_service)
    if fault == "no_device":
        ha.devices.clear()
    elif fault == "wrong_domain":
        ha.devices[0]["identifiers"] = [["ipp", MAC]]
    elif fault == "only_mac_connection":
        ha.devices[0]["identifiers"] = []
    elif fault == "wrong_identity":
        ha.devices[0]["identifiers"] = [["shelly", "001122334455"]]
    elif fault == "other_entry":
        ha.entries[0]["domain"] = "ipp"
    elif fault == "wrong_source":
        ha.entries[0]["source"] = "user"
    elif fault == "ambiguous":
        ha.entries.append({**ha.entries[0], "entry_id": "second"})
        ha.devices[0]["config_entries"].append("second")
    else:
        ha.devices[0]["config_entries"] = ["old"]
    result = await reconcile(shelly_service, proposal)
    assert result.state == "needs_reconcile" and len(ha.posts) == 1


async def test_restart_and_new_credential_recover_same_operation_without_retry(shelly_service):
    service, ha, store, session = shelly_service
    ha.lose_reply = True
    proposal = await propose(shelly_service)
    initial_flow = copy.deepcopy(ha.flow)
    await approve(shelly_service, proposal)
    await finish(shelly_service)
    record = service.record(store.get(KIND, proposal.id)["binding"], proposal.id)
    service.save(record, state="applying")
    await store.durable()
    reopened = Store(store.path)
    try:
        recovered = DeviceSetups(service.app, reopened, ha, session)
        recovered.recover()
        assert reopened.get(KIND, proposal.id)["state"] == "needs_reconcile" and not recovered.running
        ha.token = "replacement"
        ha.flow = {**initial_flow, "flow_id": "rediscovered"}
        ha.form["flow_id"] = "rediscovered"
        resumed = await propose((recovered, ha, reopened, session), "resume")
        assert resumed.id == proposal.id and resumed.candidate_aliases == ["rediscovered"]
        result = await reconcile((recovered, ha, reopened, session), resumed)
        assert result.state == "configured" and len(ha.posts) == 1
    finally:
        reopened.close()


async def test_domain_bound_identity_does_not_alias_ipp_and_shelly(shelly_service):
    service, ha, store, session = shelly_service
    shelly = await propose(shelly_service)
    ipp = IPPHA()
    ipp.flow["context"]["unique_id"] = MAC
    ipp.flow["flow_id"] = ipp.form["flow_id"] = "ipp_pending"
    service.ha = ipp
    printer = await service.create(request("ipp"), DeviceSetupCreate(candidate_id="ipp_pending"))
    assert printer.id != shelly.id and not printer.candidate_aliases and not shelly.candidate_aliases
    assert store.get(KIND, printer.id)["identity_digest"] != store.get(KIND, shelly.id)["identity_digest"]
    assert len(store.list(KIND)) == 2 and not ha.posts and not ipp.posts and session.active


@pytest.mark.parametrize("field", ["handler", "entry_domain"])
async def test_cross_domain_post_reply_never_confirms_registration(shelly_service, field):
    _, ha, store, _ = shelly_service
    setattr(ha, "result_handler" if field == "handler" else "entry_domain", "ipp")
    proposal = await propose(shelly_service)
    await approve(shelly_service, proposal)
    await finish(shelly_service)
    assert store.get(KIND, proposal.id)["state"] == "needs_reconcile" and len(ha.posts) == 1


async def test_shelly_retention_uses_truthful_label_and_keeps_write_fence(shelly_service):
    service, _, store, _ = shelly_service
    proposal = await propose(shelly_service)
    raw = store.get(KIND, proposal.id)
    raw.update(state="needs_reconcile", phase="confirmation_prepared",
               created_at=(datetime.now(UTC) - timedelta(days=8)).isoformat())
    store.put(KIND, raw)
    service.prune()
    saved = store.get(KIND, proposal.id)
    assert saved["candidate"]["title"] == "Shelly 1 設備（名稱已依保存設定移除）"
    assert saved["identity_digest"] == raw["identity_digest"] and saved["state"] == "needs_reconcile"
    assert (await propose(shelly_service, "again")).id == proposal.id


@pytest.mark.parametrize("version,domain", [(1, "shelly"), (2, "synology_dsm"), (3, "ipp")])
async def test_unknown_or_incompatible_record_versions_fence_new_writes(shelly_service, version, domain):
    _, ha, store, _ = shelly_service
    proposal = await propose(shelly_service)
    raw = store.get(KIND, proposal.id)
    raw["version"] = version
    raw["candidate"]["integration_domain"] = domain
    store.put(KIND, raw)
    with pytest.raises(ValidationError):
        SetupRecord.model_validate(raw)
    with pytest.raises(HTTPException) as exc:
        await propose(shelly_service, "new")
    assert exc.value.detail == "device_setup_record_contract_changed" and not ha.posts


async def test_legacy_ipp_migrates_storage_without_changing_approval_or_replaying(shelly_service):
    service, _, store, session = shelly_service
    ha = IPPHA()
    service.ha = ha
    system = service, ha, store, session
    proposal = await service.create(request("ipp"), DeviceSetupCreate(candidate_id="pending"))
    raw = store.get(KIND, proposal.id)
    assert raw["version"] == 2
    raw["version"] = 1
    raw["candidate"].pop("confirmation_step", None)
    store.put(KIND, raw)
    assert SetupRecord.model_validate(raw).version == 1
    approved_digest = raw["proposal_digest"]
    await approve(system, proposal)
    await finish(system)
    migrated = store.get(KIND, proposal.id)
    assert migrated["version"] == 2 and migrated["proposal_digest"] == approved_digest
    assert migrated["state"] == "configured" and len(ha.posts) == 1
    assert (await approve(system, proposal)).state == "configured" and len(ha.posts) == 1


async def test_legacy_version_reader_refuses_new_domain_writes_with_v2_record_present(shelly_service, monkeypatch):
    service, _, store, _ = shelly_service
    proposal = await propose(shelly_service)
    assert store.get(KIND, proposal.id)["version"] == 2
    class LegacyRecord(SetupRecord):
        version: Literal[1] = 1
    # The pre-upgrade records(..., for_write=True) contract is unchanged:
    # unsupported version/schema fails closed rather than ignoring a fence.
    monkeypatch.setattr(device_setup, "SetupRecord", LegacyRecord)
    ha = IPPHA()
    service.ha = ha
    with pytest.raises(HTTPException) as exc:
        await service.create(request("old-binary"), DeviceSetupCreate(candidate_id="pending"))
    assert exc.value.detail == "device_setup_record_contract_changed" and not ha.posts and not ha.backups
