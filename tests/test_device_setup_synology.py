"""Synthetic native Synology flow contracts; no NAS, account or Core acceptance."""
import asyncio
import copy
import json
import time
from types import SimpleNamespace

import pytest
from agentic_controller.device_setup import (
    INPUT_MESSAGES,
    KIND,
    MESSAGES,
    DeviceSetupApprove,
    DeviceSetupCreate,
    DeviceSetupInput,
    DeviceSetupReconcile,
    DeviceSetups,
    SetupRecord,
)
from agentic_controller.device_setup_adapters import ADAPTERS
from agentic_controller.device_setup_chat import DeviceSetupChat
from agentic_controller.discovery import project
from agentic_controller.ha import HAUnreachable
from agentic_controller.models import digest
from fastapi import HTTPException
from test_bridge_controller import call, complete, system  # noqa: F401
from test_device_setup import IPPHA, bridge_request, finish, request
from test_device_setup_shelly import ShellyHA

MAC = "001132AABBCC"
SECRETS = {"username": "synthetic-nas-owner", "password": "SYNTHETIC-NAS-PASSWORD-123"}
OTP = "98765432"
SHARES = [{"value": "/home-backups", "label": "家庭備份"}, {"value": "/spare", "label": "備用資料夾"}]
BACKUP_PATH = "ha_backup_synthetic_synology_qualification"


def form(step="link", error=None):
    schema = [
        {"type": "string", "name": "username", "required": True, "default": ""},
        {"type": "string", "name": "password", "required": True, "default": ""},
        {"type": "string", "name": "port", "required": False, "optional": True, "default": ""},
        {"type": "boolean", "name": "ssl", "required": False, "optional": True, "default": True},
        {"type": "boolean", "name": "verify_ssl", "required": False, "optional": True, "default": False},
    ]
    errors, placeholders = {}, {"name": "NAS", "host": "192.0.2.15"}
    if step == "2sa":
        schema = [{"type": "string", "name": "otp_code", "required": True}]
        errors = {"otp_code": error} if error else {}
        placeholders = None
    elif step == "backup_share":
        schema = [{"name": "backup_share", "required": True, "selector": {"select": {
            "options": copy.deepcopy(SHARES), "mode": "dropdown", "multiple": False, "custom_value": False,
            "sort": False}}},
            {"type": "string", "lengthMin": 1, "name": "backup_path", "required": True, "default": BACKUP_PATH}]
        errors, placeholders = None, None
    else:
        if step == "user":
            schema.insert(0, {"type": "string", "name": "host", "required": True, "default": "192.0.2.15"})
            for field in schema:
                if field["name"] in SECRETS:
                    field["default"] = SECRETS[field["name"]]
            placeholders = {}
        if error:
            errors = {{"invalid_auth": "username", "cannot_connect": "host", "unknown": "base",
                       "missing_data": "base"}[error]: error}
    return {"type": "form", "flow_id": "pending", "handler": "synology_dsm", "step_id": step,
        "data_schema": schema, "errors": errors, "description_placeholders": placeholders,
        "last_step": None, "preview": None}


class SynologyHA(IPPHA):
    def __init__(self):
        super().__init__()
        self.gets, self.events, self.replies = [], [], []
        self.flow.update(handler="synology_dsm", step_id="link")
        self.flow["context"].update(source="ssdp", unique_id=MAC,
                                    title_placeholders={"name": "NAS", "host": "192.0.2.15"})
        self.form = form()
        self.failure = None

    def created(self):
        # Captures retain the public result key set, with credentials omitted by Core.
        entry = {"entry_id": "newentry", "domain": "synology_dsm", "state": "loaded", "title": "NAS",
            "source": self.flow["context"]["source"], "created_at": 1792440000.0, "modified_at": 1792440000.0,
            "disabled_by": None, "error_reason_translation_domain": None, "error_reason_translation_key": None,
            "error_reason_translation_placeholders": None, "num_subentries": 0, "pref_disable_new_entities": False,
            "pref_disable_polling": False, "reason": None, "supported_subentry_types": {}, "supports_options": True,
            "supports_reconfigure": False, "supports_remove_device": False, "supports_unload": True}
        self.entries.append(entry)
        self.devices.append({"id": "nas", "name": "NAS", "config_entries": ["newentry"],
            "identifiers": [["synology_dsm", "different-serial"]], "connections": [["mac", "00:11:32:aa:bb:cc"]]})
        self.entities.append({"entity_id": "sensor.nas", "device_id": "nas", "config_entry_id": "newentry"})
        self.states.append({"entity_id": "sensor.nas", "state": "on"})
        self.flow = None
        return entry

    async def backup(self, name, *, send_guard):
        result = await super().backup(name, send_guard=send_guard)
        self.events.append("backup")
        return result

    async def request(self, method, target, data=None, *, send_guard=None):
        if method == "GET":
            self.gets.append((target, self.flow["step_id"] if self.flow else None))
            if "/flow/" in target:
                assert self.flow and self.flow["step_id"] != "2sa", "GET 2sa crashes upstream"
                if self.form_hook:
                    await self.form_hook()
                return copy.deepcopy(self.form)
            return await super().request(method, target, data, send_guard=send_guard)
        assert target == "/config/config_entries/flow/pending" and send_guard
        if self.post_hook:
            await self.post_hook()
        await send_guard()
        self.posts.append((target, copy.deepcopy(data)))
        self.events.append(self.flow["step_id"])
        self.flow["context"]["dismiss_protected"] = True
        reply = copy.deepcopy(self.replies.pop(0)) if self.replies else {"type": "create_entry", "flow_id": "pending",
            "handler": "synology_dsm", "description_placeholders": None, "result": self.created()}
        if reply.get("type") == "form":
            self.flow["step_id"] = reply["step_id"]
            self.form = copy.deepcopy(reply)
            if reply["step_id"] == "link":
                self.form["data_schema"][0]["default"] = SECRETS["username"]
                self.form["data_schema"][1]["default"] = SECRETS["password"]
                reply = copy.deepcopy(self.form)
        elif reply.get("type") == "abort":
            self.flow = None
        if self.reply_hook:
            await self.reply_hook()
        if self.failure == "cancel":
            raise asyncio.CancelledError
        if self.lose_reply or self.failure == "transport":
            raise HAUnreachable("synthetic transport error " + SECRETS["password"])
        return reply


@pytest.fixture
def synology_service(store):
    store.set_setting("csrf_secret", "11" * 32)
    store.set_setting("owner", "owner")
    ha = SynologyHA()
    session = SimpleNamespace(active=True)
    session.verify = lambda _request: session.active
    return DeviceSetups(SimpleNamespace(state=SimpleNamespace()), store, ha, session), ha, store, session


async def propose(fixture, key="proposal"):
    return await fixture[0].create(request(key), DeviceSetupCreate(candidate_id="pending", integration_domain="synology_dsm"))


async def approve(fixture, proposal, key="approve"):
    return await fixture[0].approve(request(key), proposal.id, DeviceSetupApprove(
        expected_revision=proposal.revision, proposal_digest=proposal.proposal_digest, credentials=SECRETS))


def record(fixture, setup):
    raw = fixture[2].get(KIND, setup.id)
    return fixture[0].record(raw["binding"], setup.id)


async def submit(fixture, setup, *, key=None, **values):
    current = record(fixture, setup)
    data = {"expected_revision": current.revision, "step": current.input_step, **values}
    return await fixture[0].input(request(key or "input-" + str(current.revision)), setup.id,
                                 DeviceSetupInput.model_validate(data))


async def reconcile(fixture, setup, key="reconcile"):
    return await fixture[0].reconcile(request(key), setup.id,
        DeviceSetupReconcile(expected_revision=record(fixture, setup).revision))


async def waiting(fixture, step="2sa", error=None):
    fixture[1].replies = [form(step, error)]
    setup = await propose(fixture)
    await approve(fixture, setup)
    await finish(fixture)
    assert record(fixture, setup).state == "needs_input"
    return setup


@pytest.mark.parametrize("version", ["2026.10.1", "2026.10.2"])
@pytest.mark.parametrize("source", ["ssdp", "zeroconf"])
async def test_exact_form_gate_and_versioned_record(synology_service, version, source):
    service, ha, store, _ = synology_service
    ha.config["version"] = version
    ha.flow["context"].update(source=source, unique_id=MAC.lower())
    setup = await propose(synology_service)
    assert setup.candidate.confirmation_step == "link" and setup.can_approve and setup.input is None
    assert store.get(KIND, setup.id)["version"] == 3
    assert setup.message == ADAPTERS["synology_dsm"].proposal_message
    await approve(synology_service, setup)
    await finish(synology_service)
    assert (await service.detail(request(), setup.id)).state == "configured"
    assert record(synology_service, setup).version == 3
    assert ha.events == ["backup", "link"]


@pytest.mark.parametrize("fault,reason", [
    ("extra_field", "device_setup_flow_unsupported"), ("default", "device_setup_flow_unsupported"),
    ("errors", "device_setup_flow_unsupported"), ("step", "device_setup_flow_unsupported"),
    ("source", "device_setup_flow_unsupported"), ("handler", "device_setup_flow_unsupported"),
    ("form_step", "device_setup_flow_unsupported"), ("form_handler", "device_setup_flow_unsupported"),
    ("form_id", "device_setup_flow_unsupported"), ("placeholders", "device_setup_flow_unsupported"),
    ("placeholder_type", "device_setup_flow_unsupported"), ("mac", "device_setup_identity_unavailable"),
    ("core", "device_setup_core_unsupported"),
])
async def test_unsupported_proposal_variants(synology_service, fault, reason):
    _, ha, _, _ = synology_service
    if fault == "extra_field":
        ha.form["data_schema"].append({"name": "extra", "type": "string", "required": True})
    elif fault == "default":
        ha.form["data_schema"][1]["default"] = SECRETS["password"]
    elif fault == "errors":
        ha.form["errors"] = {"username": "invalid_auth"}
    elif fault in {"step", "handler"}:
        ha.flow["step_id" if fault == "step" else "handler"] = "user"
    elif fault.startswith("form_"):
        ha.form[{"form_step": "step_id", "form_handler": "handler", "form_id": "flow_id"}[fault]] = "other"
    elif fault == "source":
        ha.flow["context"]["source"] = "user"
    elif fault == "placeholders":
        ha.form["description_placeholders"]["other"] = "extra"
    elif fault == "placeholder_type":
        ha.form["description_placeholders"]["host"] = 3
    elif fault == "mac":
        ha.flow["context"]["unique_id"] = "00:11:32:aa:bb:cc"
    else:
        ha.config["version"] = "2026.11.0"
    with pytest.raises(HTTPException) as exc:
        await propose(synology_service)
    assert exc.value.detail == reason and not ha.posts and not ha.backups


@pytest.mark.parametrize("domain", ["ipp", "shelly", "synology_dsm"])
@pytest.mark.parametrize("credentials", [False, True])
async def test_credentials_are_required_only_for_synology(synology_service, domain, credentials):
    service, _, store, _ = synology_service
    ha = {"ipp": IPPHA, "shelly": ShellyHA, "synology_dsm": SynologyHA}[domain]()
    service.ha = ha
    setup = await service.create(request(), DeviceSetupCreate(candidate_id="pending", integration_domain=domain))
    values = {"expected_revision": setup.revision, "proposal_digest": setup.proposal_digest}
    if credentials:
        values["credentials"] = SECRETS
    if credentials == (domain == "synology_dsm"):
        assert (await service.approve(request("approve"), setup.id, DeviceSetupApprove(**values))).state == "preparing"
        await finish(synology_service)
        assert store.get(KIND, setup.id)["state"] == "configured"
    else:
        with pytest.raises(HTTPException) as exc:
            await service.approve(request("approve"), setup.id, DeviceSetupApprove(**values))
        assert exc.value.status_code == 409
        assert exc.value.detail == ("device_setup_credentials_required" if domain == "synology_dsm"
                                   else "device_setup_credentials_unexpected")
        assert not ha.posts and not ha.backups


async def test_full_multistep_path_and_secret_free_surfaces(synology_service, caplog):
    service, ha, store, _ = synology_service
    ha.replies = [form("2sa", "otp_failed"), form("2sa"), form("backup_share")]
    setup = await propose(synology_service)
    views = [setup, await approve(synology_service, setup)]
    for step, values in [("2sa", {"otp_code": OTP}), ("2sa", {"otp_code": OTP}),
                         ("backup_share", {"backup_share": SHARES[0]["value"]})]:
        await finish(synology_service)
        view = await service.detail(request(), setup.id)
        views.append(view)
        assert view.state == "needs_input" and view.input.step == step and view.input.can_submit
        assert view.can_reconcile
        views.append(await submit(synology_service, setup, **values))
    await finish(synology_service)
    views.append(await service.detail(request(), setup.id))
    assert views[-1].state == "configured" and views[-1].integration.domain == "synology_dsm"
    assert (views[-1].device_count, views[-1].entity_count, views[-1].available_entity_count) == (1, 1, 1)
    assert ha.events == ["backup", "link", "2sa", "2sa", "backup_share"]
    assert len(ha.backups) == 1 and ha.backups[0].startswith("Agentic Home Synology NAS ")
    assert ha.posts[-1][1] == {"backup_share": SHARES[0]["value"], "backup_path": BACKUP_PATH}
    assert record(synology_service, setup).step_posts == 4
    authority, _ = await service.authorize(request())
    chat = DeviceSetupChat(service.app, store, ha, None)
    projection = chat.projection(authority, record(synology_service, setup), views[-1])
    await store.durable()
    raw = "".join(view.model_dump_json() for view in views) + projection.model_dump_json() + caplog.text
    raw += repr(store.db.execute("SELECT * FROM requests").fetchall())
    blobs = [store.path.read_bytes()]
    wal = store.path.with_name(store.path.name + "-wal")
    if wal.exists():
        blobs.append(wal.read_bytes())
    blobs.extend(path.read_bytes() for path in (store.path.parent / "snapshots").glob("*"))
    for secret in [*SECRETS.values(), OTP]:
        assert secret not in raw and all(secret.encode() not in blob for blob in blobs)


@pytest.mark.parametrize("error", ["invalid_auth", "cannot_connect", "unknown", "missing_data"])
async def test_login_errors_are_projected_without_echoed_defaults(synology_service, error, caplog):
    service, ha, store, _ = synology_service
    setup = await waiting(synology_service, "link", error)
    view = await service.detail(request(), setup.id)
    assert view.message == INPUT_MESSAGES[error] and view.input.error == error
    assert view.input.options == []
    assert record(synology_service, setup).expires_at > time.time() + 1790
    authority, _ = await service.authorize(request())
    projection = DeviceSetupChat(service.app, store, ha, None).projection(authority, record(synology_service, setup), view)
    assert projection.setup_state == "needs_input" and projection.input_step == "link"
    assert "帳號、密碼與驗證碼只在設定卡輸入" in projection.limitations[0]
    raw = view.model_dump_json() + projection.model_dump_json() + json.dumps(store.get(KIND, setup.id)) + caplog.text
    assert all(secret not in raw for secret in SECRETS.values())
    await submit(synology_service, setup, **SECRETS)
    await finish(synology_service)
    assert record(synology_service, setup).state == "configured" and len(ha.backups) == 1


@pytest.mark.parametrize("reason,expected", [("already_configured", "device_setup_already_configured"),
    ("reconfigure_successful", "device_setup_existing_updated"), ("reauth_successful", "device_setup_flow_aborted"),
    ("unknown", "device_setup_flow_aborted")])
async def test_aborts_stop_the_operation(synology_service, reason, expected):
    service, ha, _, _ = synology_service
    ha.replies = [{"type": "abort", "flow_id": "pending", "handler": "synology_dsm", "reason": reason}]
    setup = await propose(synology_service)
    await approve(synology_service, setup)
    await finish(synology_service)
    view = await service.detail(request(), setup.id)
    assert view.state == "blocked" and view.reason_code == expected
    assert view.integration is None and record(synology_service, setup).entry_id is None
    if reason in {"already_configured", "reconfigure_successful"}:
        assert view.message == ("這台 NAS 已經接入家庭系統。" if reason == "already_configured" else
            "這台 NAS 原本已接入家庭系統，已用你輸入的帳號更新原有連線。")
    assert len(ha.posts) == 1


@pytest.mark.parametrize("step", ["link", "2sa", "backup_share"])
@pytest.mark.parametrize("failure", ["lost", "transport", "cancel"])
@pytest.mark.parametrize("created", [False, True])
async def test_uncertain_posts_never_replay_and_reconcile_reads_only(synology_service, step, failure, created):
    _, ha, _, _ = synology_service
    setup = await propose(synology_service) if step == "link" else await waiting(synology_service, step)
    prior = len(ha.posts)
    if not created:
        ha.replies = [form(step, "cannot_connect" if step == "link" else None)]
    ha.lose_reply, ha.failure = failure == "lost", failure
    if step == "link":
        await approve(synology_service, setup)
    else:
        await submit(synology_service, setup, **({"otp_code": OTP} if step == "2sa" else {"backup_share": SHARES[0]["value"]}))
    await asyncio.gather(*list(synology_service[0].running.values()), return_exceptions=True)
    assert record(synology_service, setup).state == "needs_reconcile"
    assert record(synology_service, setup).reason_code == "device_setup_outcome_unknown"
    ha.lose_reply, ha.failure = False, None
    view = await reconcile(synology_service, setup)
    assert view.state == ("configured" if created else "needs_input")
    assert len(ha.posts) == prior + 1
    assert not any("/flow/" in target and phase == "2sa" for target, phase in ha.gets)


@pytest.mark.parametrize("phase,state,expected", [("approved", "preparing", "blocked"),
    ("backup_prepared", "preparing", "blocked"), ("backup_completed", "preparing", "blocked"),
    ("confirmation_prepared", "applying", "needs_reconcile"),
    ("confirmation_acknowledged", "applying", "needs_reconcile"), ("input_required", "needs_input", "needs_input")])
async def test_recover_never_resends_at_any_phase(synology_service, phase, state, expected):
    service, ha, store, _ = synology_service
    setup = await propose(synology_service)
    before = service.save(record(synology_service, setup), state=state, phase=phase,
                          input_step="2sa" if state == "needs_input" else None)
    service.recover()
    saved = store.get(KIND, setup.id)
    assert saved["state"] == expected and saved["version"] == 3 and not ha.posts
    if state == "needs_input":
        assert saved == before.model_dump()


@pytest.mark.parametrize("change,reason", [("owner", "discovery_owner_required"),
    ("pause", "device_setup_paused"), ("update", "device_setup_update_in_progress"),
    ("identity", "device_setup_candidate_changed"), ("step", "device_setup_candidate_changed"),
    ("source", "device_setup_flow_unsupported"), ("entry", "device_setup_integrations_changed"),
    ("core", "device_setup_core_unsupported")])
async def test_final_transport_guard_restores_unsent_input(synology_service, change, reason):
    service, ha, store, _ = synology_service
    setup = await waiting(synology_service)
    async def change_before_send():
        if change == "owner":
            ha.revoked = True
        elif change == "pause":
            store.set_setting("paused", True)
        elif change == "update":
            store.set_setting("updater_gate", True)
        elif change == "identity":
            ha.flow["context"]["unique_id"] = "FFEEDDCCBBAA"
        elif change == "step":
            ha.flow["step_id"] = "backup_share"
        elif change == "source":
            ha.flow["context"]["source"] = "zeroconf"
        elif change == "entry":
            ha.entries.append({"entry_id": "other", "domain": "synology_dsm", "source": "ssdp"})
        else:
            ha.config["version"] = "2026.11.0"
    ha.post_hook = change_before_send
    await submit(synology_service, setup, otp_code=OTP)
    await finish(synology_service)
    current = record(synology_service, setup)
    assert current.state == "needs_input" and current.input_step == "2sa" and current.reason_code == reason
    view = service.view(current)
    assert not view.input.can_submit and view.message == "這一步沒有送出。請重新讀取後再繼續。"
    assert len(ha.posts) == 1


async def test_missing_flow_guard_requires_reconcile_without_posting(synology_service):
    service, ha, _, _ = synology_service
    setup = await waiting(synology_service)
    ha.flow = None
    await submit(synology_service, setup, otp_code=OTP)
    await finish(synology_service)
    current = record(synology_service, setup)
    view = await service.detail(request(), setup.id)
    assert current.state == "needs_input" and current.reason_code == "device_setup_candidate_changed"
    assert view.can_reconcile and not view.input.can_submit
    assert view.message == "這一步沒有送出。請重新讀取後再繼續。"
    with pytest.raises(HTTPException) as exc:
        await submit(synology_service, setup, otp_code=OTP)
    assert exc.value.status_code == 409 and exc.value.detail == "device_setup_input_unavailable"
    assert record(synology_service, setup) == current
    view = await reconcile(synology_service, setup)
    assert view.state == "blocked" and view.reason_code == "device_setup_flow_gone" and view.input is None
    assert view.message == "家庭系統已結束這次接入，請重新探索設備後再試。"
    assert len(ha.posts) == 1 and ha.events == ["backup", "link"]


@pytest.mark.parametrize("gate", ["pause", "owner"])
async def test_unsent_gate_rejection_rearms_only_after_read_only_reconcile(synology_service, gate):
    service, ha, store, _ = synology_service
    setup = await waiting(synology_service)
    async def close_gate():
        if gate == "pause":
            store.set_setting("paused", True)
        else:
            ha.revoked = True
    ha.post_hook = close_gate
    await submit(synology_service, setup, otp_code=OTP)
    await finish(synology_service)
    assert not service.view(record(synology_service, setup)).input.can_submit
    store.set_setting("paused", False)
    ha.revoked, ha.post_hook = False, None
    with pytest.raises(HTTPException) as exc:
        await submit(synology_service, setup, otp_code=OTP)
    assert exc.value.status_code == 409 and exc.value.detail == "device_setup_input_unavailable"
    gets = ha.gets.copy()
    view = await reconcile(synology_service, setup)
    assert view.state == "needs_input" and view.input.can_submit and view.reason_code is None
    assert ha.gets[len(gets):] == [("/config", "2sa")] and len(ha.posts) == 1
    await submit(synology_service, setup, otp_code=OTP)
    await finish(synology_service)
    assert record(synology_service, setup).state == "configured" and len(ha.posts) == 2


@pytest.mark.parametrize("expired,limited,expected", [
    (False, False, "這一步沒有送出。請重新讀取後再繼續。"),
    (True, False, "這一步已逾時，請重新讀取後繼續。"),
    (False, True, "嘗試次數過多，這次接入已停止。請稍後重新開始。"),
    (True, True, "嘗試次數過多，這次接入已停止。請稍後重新開始。"),
])
async def test_unsent_input_message_precedence(synology_service, expired, limited, expected):
    service = synology_service[0]
    setup = await waiting(synology_service, "2sa", "otp_failed")
    current = service.save(record(synology_service, setup), reason_code="device_setup_interrupted",
        expires_at=time.time() + (-1 if expired else 1800), step_posts=8 if limited else 1)
    view = service.view(current)
    assert not view.input.can_submit and view.input.error == "otp_failed" and view.message == expected


async def test_unrelated_entry_does_not_block_later_step(synology_service):
    _, ha, _, _ = synology_service
    setup = await waiting(synology_service)
    ha.entries.append({"entry_id": "unrelated", "domain": "ipp", "source": "user"})
    await submit(synology_service, setup, otp_code=OTP)
    await finish(synology_service)
    assert record(synology_service, setup).state == "configured"


async def test_expiry_rearm_revision_step_and_share_validation(synology_service):
    service, ha, _, _ = synology_service
    setup = await waiting(synology_service, "backup_share")
    current = service.save(record(synology_service, setup), expires_at=time.time() - 1)
    view = service.view(current)
    assert not view.input.can_submit and view.message == INPUT_MESSAGES["device_setup_input_expired"]
    with pytest.raises(HTTPException) as exc:
        await submit(synology_service, setup, backup_share=SHARES[0]["value"])
    assert exc.value.detail == "device_setup_input_expired"
    assert (await reconcile(synology_service, setup)).input.can_submit
    with pytest.raises(HTTPException) as exc:
        await submit(synology_service, setup, backup_share="/not-an-option")
    assert exc.value.detail == "device_setup_input_invalid"
    current = record(synology_service, setup)
    with pytest.raises(HTTPException) as exc:
        await service.input(request("wrong-revision"), setup.id, DeviceSetupInput.model_validate({
            "expected_revision": current.revision - 1, "step": "backup_share", "backup_share": SHARES[0]["value"]}))
    assert exc.value.detail == "device_setup_revision_changed"
    with pytest.raises(HTTPException) as exc:
        await submit(synology_service, setup, step="2sa", otp_code=OTP)
    assert exc.value.detail == "device_setup_input_unavailable" and len(ha.posts) == 1


async def exhaust_inputs(fixture, step, error=None):
    setup = await waiting(fixture, step, error)
    values = {"link": SECRETS, "2sa": {"otp_code": OTP}, "backup_share": {"backup_share": SHARES[0]["value"]}}[step]
    for _ in range(7):
        fixture[1].replies = [form(step, error)]
        await submit(fixture, setup, **values)
        await finish(fixture)
    return setup


def assert_input_limit(fixture, setup):
    service = fixture[0]
    current = record(fixture, setup)
    assert current.state == "blocked" and current.reason_code == "device_setup_input_limit"
    assert current.input_step is None and current.input_error is None
    assert current.share_options == [] and current.backup_path is None
    assert current.step_posts == 8
    view = service.view(current)
    assert view.input is None and not view.can_reconcile and not view.can_approve
    assert view.message == "嘗試次數過多，這次接入已停止。請稍後重新開始。"
    return view


async def test_eight_link_posts_block_and_allow_fresh_owner_approval(synology_service):
    service, ha, _, _ = synology_service
    setup = await exhaust_inputs(synology_service, "link", "invalid_auth")
    assert_input_limit(synology_service, setup)
    assert len(ha.posts) == 8 and all(payload == SECRETS for _, payload in ha.posts)
    assert len(ha.backups) == 1 and ha.flow["step_id"] == "link"
    with pytest.raises(HTTPException) as exc:
        await submit(synology_service, setup, step="link", **SECRETS)
    assert exc.value.status_code == 409 and exc.value.detail == "device_setup_input_unavailable"
    with pytest.raises(HTTPException) as exc:
        await reconcile(synology_service, setup)
    assert exc.value.status_code == 409 and exc.value.detail == "device_setup_reconcile_unavailable"
    assert (await propose(synology_service)).id == setup.id
    # Native GET link renders a fresh form, without the POST's defaults/errors.
    ha.form = form()
    restarted = await propose(synology_service, key="restart")
    assert restarted.id != setup.id and restarted.state == "proposed" and restarted.can_approve
    assert record(synology_service, restarted).step_posts == 0
    with pytest.raises(HTTPException) as exc:
        await service.approve(request("missing-credentials"), restarted.id, DeviceSetupApprove(
            expected_revision=restarted.revision, proposal_digest=restarted.proposal_digest))
    assert exc.value.status_code == 409 and exc.value.detail == "device_setup_credentials_required"
    assert len(ha.backups) == 1 and len(ha.posts) == 8
    await approve(synology_service, restarted, key="restart-approve")
    await finish(synology_service)
    assert record(synology_service, restarted).state == "configured"
    assert record(synology_service, restarted).step_posts == 1
    assert len(ha.backups) == len(set(ha.backups)) == 2
    assert len(ha.posts) == 9 and ha.posts[-1][1] == SECRETS and ha.events[-2:] == ["backup", "link"]
    assert_input_limit(synology_service, setup)


@pytest.mark.parametrize("step,error", [("2sa", "otp_failed"), ("backup_share", None)])
async def test_eight_later_posts_block_and_restart_uses_native_fallback(synology_service, step, error):
    _, ha, _, _ = synology_service
    setup = await exhaust_inputs(synology_service, step, error)
    assert_input_limit(synology_service, setup)
    assert len(ha.posts) == 8 and len(ha.backups) == 1
    gets = ha.gets.copy()
    with pytest.raises(HTTPException) as exc:
        await propose(synology_service, key="restart")
    assert exc.value.status_code == 409 and exc.value.detail == "device_setup_flow_unsupported"
    assert ha.gets[len(gets):] == [("/config", step)] and len(ha.posts) == 8
    assert_input_limit(synology_service, setup)


@pytest.mark.parametrize("step,error", [("link", "invalid_auth"), ("2sa", "otp_failed"), ("backup_share", None)])
async def test_legacy_exhausted_input_reconcile_blocks_without_posting(synology_service, step, error):
    service, ha, _, _ = synology_service
    setup = await waiting(synology_service, step, error)
    current = service.save(record(synology_service, setup), step_posts=8)
    assert current.state == "needs_input" and not service.view(current).input.can_submit
    assert service.view(current).message == "嘗試次數過多，這次接入已停止。請稍後重新開始。"
    gets = ha.gets.copy()
    view = await reconcile(synology_service, setup)
    assert view == assert_input_limit(synology_service, setup)
    assert len(ha.posts) == 1 and len(ha.backups) == 1
    if step == "2sa":
        assert ha.gets[len(gets):] == [("/config", "2sa")]


@pytest.mark.parametrize("cancel", [False, True])
async def test_last_unsent_attempt_blocks_and_clears_input(synology_service, cancel):
    service, ha, store, _ = synology_service
    setup = await waiting(synology_service, "backup_share")
    service.save(record(synology_service, setup), step_posts=7)
    async def stop_before_send():
        if cancel:
            raise asyncio.CancelledError
        store.set_setting("paused", True)
    ha.post_hook = stop_before_send
    await submit(synology_service, setup, backup_share=SHARES[0]["value"])
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await finish(synology_service)
    else:
        await finish(synology_service)
    assert_input_limit(synology_service, setup)
    assert len(ha.posts) == 1 and len(ha.backups) == 1


@pytest.mark.parametrize("domain", ["ipp", "shelly"])
@pytest.mark.parametrize("reason", ["device_setup_already_configured", "device_setup_flow_gone", "device_setup_existing_updated"])
async def test_non_synology_blocked_messages_stay_generic(synology_service, domain, reason):
    service = synology_service[0]
    service.ha = {"ipp": IPPHA, "shelly": ShellyHA}[domain]()
    setup = await service.create(request(), DeviceSetupCreate(candidate_id="pending", integration_domain=domain))
    current = service.save(record(synology_service, setup), state="blocked", reason_code=reason)
    view = service.view(current)
    assert reason in INPUT_MESSAGES and view.message == MESSAGES["blocked"]
    assert view.input is None and not view.can_reconcile and not view.can_approve


async def test_same_key_replay_ignores_changed_secrets_without_resending(synology_service):
    service, ha, _, _ = synology_service
    setup = await waiting(synology_service)
    before = record(synology_service, setup)
    body = DeviceSetupInput.model_validate({"expected_revision": before.revision, "step": "2sa", "otp_code": OTP})
    await service.input(request("same"), setup.id, body)
    await finish(synology_service)
    replay = DeviceSetupInput.model_validate({"expected_revision": before.revision, "step": "2sa", "otp_code": "123456"})
    assert (await service.input(request("same"), setup.id, replay)).state == "configured"
    assert (await approve(synology_service, setup)).state == "configured" and len(ha.posts) == 2


async def test_link_error_echoes_never_reach_disk_or_hashing(synology_service, monkeypatch, caplog):
    service, ha, store, _ = synology_service
    original = service.private_hash
    def only_public_projection(value):
        text = json.dumps(value)
        assert all(secret not in text for secret in SECRETS.values())
        return original(value)
    monkeypatch.setattr(service, "private_hash", only_public_projection)
    setup = await waiting(synology_service, "link", "invalid_auth")
    view = await reconcile(synology_service, setup)
    assert view.input.error == "invalid_auth"
    await store.durable()
    files = [store.path, store.path.with_name(store.path.name + "-wal")]
    files.extend((store.path.parent / "snapshots").glob("*"))
    text = view.model_dump_json() + caplog.text + repr(store.db.execute("SELECT * FROM requests").fetchall())
    for secret in SECRETS.values():
        assert secret not in text
        assert all(secret.encode() not in path.read_bytes() for path in files if path.exists())


@pytest.mark.parametrize("step", ["link", "2sa", "backup_share"])
def test_response_projection_keeps_only_bounded_public_fields(step):
    raw = form(step)
    raw.update(data=SECRETS, title=SECRETS["username"], description=SECRETS["password"])
    if step == "link":
        raw["data_schema"][0]["default"] = SECRETS["username"]
        raw["data_schema"][1]["default"] = SECRETS["password"]
    result = ADAPTERS["synology_dsm"].response(raw, "pending")
    assert set(result) == {"type", "flow_id", "handler", "step_id", "error", "schema", "options", "backup_path"}
    assert all(set(field) == {"name", "type", "required"} for field in result["schema"])
    assert all(secret not in json.dumps(result) for secret in SECRETS.values())


@pytest.mark.parametrize("errors", [None, {}])
def test_captured_backup_share_null_errors_and_selector_metadata(errors):
    raw = form("backup_share")
    assert raw["description_placeholders"] is None
    assert raw["data_schema"][0]["selector"]["select"]["sort"] is False
    assert raw["data_schema"][1]["lengthMin"] == 1
    raw["errors"] = errors
    result = ADAPTERS["synology_dsm"].response(raw, "pending")
    assert result["step_id"] == "backup_share" and result["error"] is None
    assert result["backup_path"] == BACKUP_PATH and result["options"] == SHARES


async def test_backup_share_reconcile_rereads_validated_path_before_post(synology_service):
    service, ha, _, _ = synology_service
    setup = await waiting(synology_service, "backup_share")
    assert record(synology_service, setup).backup_path == BACKUP_PATH
    service.save(record(synology_service, setup), reason_code="device_setup_interrupted", backup_path="stale_default")
    ha.form["data_schema"][1]["default"] = "ha_backup_reread"
    view = await reconcile(synology_service, setup)
    assert view.input.can_submit and view.reason_code is None
    assert record(synology_service, setup).backup_path == "ha_backup_reread"
    assert ha.gets[-1] == ("/config/config_entries/flow/pending", "backup_share")
    await submit(synology_service, setup, backup_share=SHARES[1]["value"])
    await finish(synology_service)
    assert ha.posts[-1][1] == {"backup_share": SHARES[1]["value"], "backup_path": "ha_backup_reread"}
    assert record(synology_service, setup).state == "configured" and len(ha.backups) == 1


async def test_backup_share_can_return_to_2sa(synology_service):
    _, ha, _, _ = synology_service
    setup = await waiting(synology_service, "backup_share")
    ha.replies = [form("2sa")]
    await submit(synology_service, setup, backup_share=SHARES[0]["value"])
    await finish(synology_service)
    assert record(synology_service, setup).input_step == "2sa"
    assert record(synology_service, setup).backup_path is None
    await submit(synology_service, setup, otp_code=OTP)
    await finish(synology_service)
    assert record(synology_service, setup).state == "configured" and len(ha.backups) == 1


async def test_input_barrier_is_durable_before_each_post(synology_service, monkeypatch):
    service, ha, store, _ = synology_service
    setup = await waiting(synology_service)
    original, checkpoints = store.durable, []
    async def checkpoint():
        await original()
        checkpoints.append(record(synology_service, setup))
    monkeypatch.setattr(store, "durable", checkpoint)
    async def verify_before_transport():
        assert checkpoints[-1].phase == "confirmation_prepared"
        assert checkpoints[-1].post_step == "2sa" and checkpoints[-1].step_posts == 2
    ha.post_hook = verify_before_transport
    await submit(synology_service, setup, otp_code=OTP)
    await finish(synology_service)
    assert record(synology_service, setup).state == "configured" and not service.running


async def test_unknown_get_form_after_sent_post_stays_uncertain(synology_service):
    _, ha, _, _ = synology_service
    setup = await waiting(synology_service, "link", "cannot_connect")
    ha.form["data_schema"].append({"name": "unqualified", "type": "string", "required": True})
    view = await reconcile(synology_service, setup)
    assert view.state == "needs_reconcile" and view.reason_code == "device_setup_outcome_unknown"
    assert len(ha.posts) == 1


async def test_synology_v3_fences_legacy_record_reader(synology_service, monkeypatch):
    from typing import Literal

    from agentic_controller import device_setup

    service, _, store, _ = synology_service
    setup = await propose(synology_service)
    class LegacyRecord(SetupRecord):
        version: Literal[1, 2] = 1
    monkeypatch.setattr(device_setup, "SetupRecord", LegacyRecord)
    with pytest.raises(HTTPException) as exc:
        service.records(store.get(KIND, setup.id)["binding"], for_write=True)
    assert exc.value.detail == "device_setup_record_contract_changed"


async def test_legacy_approve_digest_stays_byte_identical(synology_service):
    service = synology_service[0]
    body = DeviceSetupApprove(expected_revision=2, proposal_digest="a" * 64)
    receipt, _ = service.receipt(request("legacy"), "binding", "approve", body)
    assert receipt[2] == digest({"expected_revision": 2, "proposal_digest": "a" * 64})


@pytest.mark.parametrize("fault", ["extra", "type", "error", "errors_missing", "errors_false", "errors_list", "errors_string",
    "step", "flow_id", "handler", "share_path", "share_label", "share_count", "backup_path",
    "multiple", "custom_value", "mode", "otp_default"])
async def test_unexpected_response_shapes_stay_uncertain(synology_service, fault):
    _, ha, _, _ = synology_service
    reply = form("backup_share" if fault.startswith("share_") or fault in {
        "backup_path", "multiple", "custom_value", "mode"} else "2sa")
    if fault == "extra":
        reply["data_schema"].append({"name": "extra", "type": "string", "required": True})
    elif fault == "type":
        reply["data_schema"][0]["type"] = "integer"
    elif fault == "error":
        reply["errors"] = {"base": SECRETS["password"]}
    elif fault == "errors_missing":
        reply.pop("errors")
    elif fault.startswith("errors_"):
        reply["errors"] = {"errors_false": False, "errors_list": [], "errors_string": ""}[fault]
    elif fault == "step":
        reply["step_id"] = "user"
    elif fault in {"flow_id", "handler"}:
        reply[fault] = "other"
    elif fault == "share_path":
        reply["data_schema"][0]["selector"]["select"]["options"][0]["value"] = "relative"
    elif fault == "share_label":
        reply["data_schema"][0]["selector"]["select"]["options"][0]["label"] = "x" * 161
    elif fault == "share_count":
        reply["data_schema"][0]["selector"]["select"]["options"] = SHARES * 33
    elif fault == "backup_path":
        reply["data_schema"][1]["default"] = "../../bad"
    elif fault in {"multiple", "custom_value", "mode"}:
        reply["data_schema"][0]["selector"]["select"][fault] = "list" if fault == "mode" else True
    else:
        reply["data_schema"][0]["default"] = OTP
    ha.replies = [reply]
    setup = await propose(synology_service)
    await approve(synology_service, setup)
    await finish(synology_service)
    assert record(synology_service, setup).state == "needs_reconcile" and len(ha.posts) == 1


@pytest.mark.parametrize("step", ["2sa", "backup_share"])
async def test_unexpected_user_form_never_persists_defaults_or_gets_reconciled(synology_service, step, monkeypatch, caplog):
    service, ha, store, _ = synology_service
    original = service.private_hash
    def only_public_projection(value):
        assert all(secret not in json.dumps(value) for secret in SECRETS.values())
        return original(value)
    monkeypatch.setattr(service, "private_hash", only_public_projection)
    setup = await waiting(synology_service, step)
    ha.replies = [form("user", "invalid_auth")]
    await submit(synology_service, setup, **({"otp_code": OTP} if step == "2sa" else {"backup_share": SHARES[0]["value"]}))
    await finish(synology_service)
    view = await service.detail(request(), setup.id)
    assert view.state == "needs_reconcile" and view.reason_code == "device_setup_outcome_unknown"
    assert ha.flow["step_id"] == "user"
    assert next(field for field in ha.form["data_schema"] if field["name"] == "password")["default"] == SECRETS["password"]
    gets = ha.gets.copy()
    result = await reconcile(synology_service, setup)
    assert result.state == "blocked" and result.reason_code == "device_setup_flow_unsupported"
    assert ha.gets[len(gets):] == [("/config", "user")] and len(ha.posts) == 2
    await store.durable()
    raw = view.model_dump_json() + result.model_dump_json() + caplog.text
    raw += repr(store.db.execute("SELECT * FROM records").fetchall()) + repr(store.db.execute("SELECT * FROM requests").fetchall())
    files = [store.path, store.path.with_name(store.path.name + "-wal")]
    files.extend((store.path.parent / "snapshots").glob("*"))
    for secret in [*SECRETS.values(), OTP]:
        assert secret not in raw and all(secret.encode() not in path.read_bytes() for path in files if path.exists())


@pytest.mark.parametrize("fault", ["source", "domain", "entry_id", "flow_id", "handler"])
async def test_create_entry_requires_exact_response_identity(synology_service, fault):
    _, ha, _, _ = synology_service
    entry = {"entry_id": "newentry", "domain": "synology_dsm", "source": "ssdp"}
    if fault == "entry_id":
        ha.entries = [{**entry, "entry_id": "oldentry"}]
        entry["entry_id"] = "oldentry"
    elif fault in {"source", "domain"}:
        entry[fault] = "other"
    reply = {"type": "create_entry", "flow_id": "pending", "handler": "synology_dsm", "result": entry}
    if fault in {"flow_id", "handler"}:
        reply[fault] = "other"
    ha.replies = [reply]
    setup = await propose(synology_service)
    await approve(synology_service, setup)
    await finish(synology_service)
    assert record(synology_service, setup).state == "needs_reconcile"


@pytest.mark.parametrize("proof", ["missing", "wrong_mac", "other_entry", "correct"])
async def test_acknowledged_entry_requires_its_discovered_mac(synology_service, proof):
    _, ha, _, _ = synology_service
    async def registry_proof():
        if proof == "missing":
            ha.devices = []
        elif proof == "wrong_mac":
            ha.devices[0]["connections"] = [["mac", "11:22:33:44:55:66"]]
        elif proof == "other_entry":
            ha.devices[0]["config_entries"] = ["otherentry"]
    ha.reply_hook = registry_proof
    setup = await propose(synology_service)
    await approve(synology_service, setup)
    await finish(synology_service)
    current = record(synology_service, setup)
    assert current.entry_id == "newentry" and len(ha.posts) == 1
    assert current.state == ("configured" if proof == "correct" else "needs_reconcile")
    assert current.reason_code == (None if proof == "correct" else "device_setup_entry_not_verified")


@pytest.mark.parametrize("restarted", [False, True])
async def test_acknowledged_entry_reconciles_when_device_appears(synology_service, restarted):
    service, ha, _, _ = synology_service
    setup = await propose(synology_service)
    # Seed the durable acknowledgement at the crash boundary, without replaying a POST.
    ha.created()
    device, ha.devices = ha.devices[0], []
    current = service.save(record(synology_service, setup), state="applying", entry_id="newentry",
        phase="confirmation_acknowledged", post_step="link", step_posts=1)
    if restarted:
        service.recover()
        assert record(synology_service, setup).state == "needs_reconcile"
    else:
        authority, _ = await service.authorize(request())
        await service.reconcile_record(authority, current)
    result = await reconcile(synology_service, setup, "missing-device")
    assert result.state == "needs_reconcile" and result.reason_code == "device_setup_entry_not_verified"
    assert record(synology_service, setup).entry_id == "newentry"
    ha.devices = [device]
    assert (await reconcile(synology_service, setup, "device-appeared")).state == "configured"
    assert not ha.posts and not ha.backups


@pytest.mark.parametrize("fault", ["not_list", "oversized", "not_object", "connections", "associations",
    "incomplete_tail"])
async def test_acknowledged_entry_incomplete_registry_preserves_record(synology_service, fault):
    service, ha, store, _ = synology_service
    setup = await propose(synology_service)
    ha.created()
    service.save(record(synology_service, setup), state="needs_reconcile", entry_id="newentry",
        phase="confirmation_acknowledged", post_step="link", step_posts=1)
    before = store.get(KIND, setup.id)
    if fault == "not_list":
        ha.devices = None
    elif fault == "oversized":
        ha.devices *= 1001
    elif fault == "not_object":
        ha.devices = [None]
    elif fault == "incomplete_tail":
        ha.devices.append({"connections": None})
    else:
        ha.devices[0]["connections" if fault == "connections" else "config_entries"] = None
    with pytest.raises(HTTPException) as exc:
        await reconcile(synology_service, setup)
    assert exc.value.status_code == 503 and exc.value.detail == "device_setup_source_incomplete"
    assert store.get(KIND, setup.id) == before and before["state"] != "configured"
    assert not ha.posts and not ha.backups


@pytest.mark.parametrize("mac", [MAC, MAC.lower(), "00:11:32:AA:BB:CC", "00-11-32-aa-bb-cc"])
async def test_gone_flow_reconciles_only_exact_normalized_mac(synology_service, mac):
    _, ha, _, _ = synology_service
    ha.lose_reply = True
    setup = await propose(synology_service)
    await approve(synology_service, setup)
    await finish(synology_service)
    ha.devices[0]["connections"] = [["mac", mac]]
    assert (await reconcile(synology_service, setup)).state == "configured" and len(ha.posts) == 1


@pytest.mark.parametrize("fault", ["wrong_mac", "serial_only", "wrong_source", "wrong_domain", "ambiguous", "gone"])
async def test_gone_flow_never_guesses_entry_identity(synology_service, fault):
    _, ha, _, _ = synology_service
    ha.lose_reply = True
    setup = await propose(synology_service)
    await approve(synology_service, setup)
    await finish(synology_service)
    if fault == "wrong_mac":
        ha.devices[0]["connections"] = [["mac", "11:22:33:44:55:66"]]
    elif fault == "serial_only":
        ha.devices[0]["connections"] = []
        ha.devices[0]["identifiers"] = [["synology_dsm", MAC]]
    elif fault in {"wrong_source", "wrong_domain"}:
        ha.entries[0]["source" if fault == "wrong_source" else "domain"] = "other"
    elif fault == "ambiguous":
        ha.entries.append({**ha.entries[0], "entry_id": "second"})
        ha.devices[0]["config_entries"].append("second")
    else:
        ha.entries, ha.devices = [], []
    result = await reconcile(synology_service, setup)
    assert result.state == ("blocked" if fault in {"gone", "wrong_domain"} else "needs_reconcile")
    assert result.reason_code == ("device_setup_flow_gone" if result.state == "blocked" else "device_setup_entry_not_verified")
    assert len(ha.posts) == 1


@pytest.mark.parametrize("step", ["link", "2sa", "backup_share", "user"])
async def test_reconcile_resynchronizes_known_steps_without_getting_2sa(synology_service, step):
    _, ha, _, _ = synology_service
    setup = await waiting(synology_service)
    ha.flow["step_id"], ha.form = step, form(step)
    result = await reconcile(synology_service, setup)
    if step == "user":
        assert result.state == "blocked" and result.reason_code == "device_setup_flow_unsupported"
    else:
        assert result.state == "needs_input" and result.input.step == step
    assert len(ha.posts) == 1


@pytest.mark.parametrize("field,value", [("username", ""), ("username", "x" * 129), ("username", "name\n"),
    ("password", ""), ("password", "x" * 257), ("otp_code", "12345"), ("otp_code", "123456789"),
    ("otp_code", "１２３４５６"), ("backup_share", "relative")])
def test_input_bounds(field, value):
    data = {"expected_revision": 1, "step": "link", **SECRETS}
    if field == "otp_code":
        data = {"expected_revision": 1, "step": "2sa"}
    elif field == "backup_share":
        data = {"expected_revision": 1, "step": "backup_share"}
    with pytest.raises(HTTPException) as exc:
        DeviceSetupInput.model_validate({**data, field: value})
    assert exc.value.status_code == 409 and exc.value.detail == "device_setup_input_invalid"


@pytest.mark.parametrize("before_start", [False, True])
async def test_bridge_stop_before_later_post_restores_input(synology_service, before_start):
    service, ha, _, _ = synology_service
    service.app.state.ingress_bridge = SimpleNamespace(identity=lambda value: getattr(value.state, "bridge_identity", None))
    setup = await waiting(synology_service)
    await service.writer.acquire()
    try:
        current = record(synology_service, setup)
        await service.input(bridge_request("bridge-input", "pairing"), setup.id, DeviceSetupInput.model_validate({
            "expected_revision": current.revision, "step": "2sa", "otp_code": OTP}))
        if not before_start:
            await asyncio.sleep(0)
        assert service.bridge_active("pairing") and not service.bridge_active("other")
        await service.stop_bridge("pairing")
        assert record(synology_service, setup).state == "needs_input" and len(ha.posts) == 1
        view = service.view(record(synology_service, setup))
        assert not view.input.can_submit and view.message == "這一步沒有送出。請重新讀取後再繼續。"
        with pytest.raises(HTTPException) as exc:
            await submit(synology_service, setup, otp_code=OTP)
        assert exc.value.status_code == 409 and exc.value.detail == "device_setup_input_unavailable"
        assert not service.bridge_active("pairing")
    finally:
        service.writer.release()
        await finish(synology_service)


@pytest.fixture
def nas(ha):
    nas = SynologyHA()
    ha.base, ha.websocket = nas.base, nas.websocket
    ha.ws, ha.request, ha.backup = nas.ws, nas.request, nas.backup
    return nas


async def sidebar_waiting(fixture, nas):
    app, _, _, host, _, _ = fixture
    nas.replies = [form("2sa")]
    response = await host.post("/api/device-setups", json={"candidate_id": "pending", "integration_domain": "synology_dsm"},
                               headers={"Idempotency-Key": "nas-proposal"})
    assert response.status_code == 200, response.text
    setup = response.json()
    path = "/api/device-setups/" + setup["id"]
    response = await call(fixture, path + "/approve", "POST", {"expected_revision": setup["revision"],
        "proposal_digest": setup["proposal_digest"], "credentials": SECRETS})
    assert response.status_code == 202, response.text
    await complete(app)
    view = (await host.get(path)).json()
    assert view["state"] == "needs_input"
    return path, {"expected_revision": view["revision"], "step": "2sa", "otp_code": OTP}


async def test_sidebar_input_route_and_same_key_replay(system, nas):  # noqa: F811
    app, _, _, host, _, _ = system
    path, body = await sidebar_waiting(system, nas)
    response = await call(system, path + "/input", "POST", body)
    assert response.status_code == 202, response.text
    await complete(app)
    assert (await host.get(path)).json()["state"] == "configured"
    assert (await call(system, path + "/input", "POST", body)).status_code == 202
    await complete(app)
    assert len(nas.posts) == 2 and len(nas.backups) == 1


@pytest.mark.parametrize("lease_expired", [False, True])
async def test_sidebar_revocation_or_expiry_fences_later_input(system, nas, lease_expired):  # noqa: F811
    app, bridge, _, host, options, _ = system
    path, body = await sidebar_waiting(system, nas)
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold_before_send():
        entered.set()
        await release.wait()
    nas.post_hook = hold_before_send
    assert (await call(system, path + "/input", "POST", body)).status_code == 202
    await asyncio.wait_for(entered.wait(), 5)
    assert app.state.device_setups.bridge_active(options["pairing_id"])
    if lease_expired:
        bridge.owner_valid_until = time.time() - 1
        release.set()
    else:
        await bridge.revoke()
        release.set()
    await complete(app)
    view = (await host.get(path)).json()
    assert view["state"] == "needs_input" and view["input"]["step"] == "2sa" and len(nas.posts) == 1


async def test_sidebar_watcher_refreshes_lease_during_input(system, nas):  # noqa: F811
    app, bridge, _, host, _, _ = system
    path, body = await sidebar_waiting(system, nas)
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold_before_send():
        entered.set()
        await release.wait()
    nas.post_hook = hold_before_send
    assert (await call(system, path + "/input", "POST", body)).status_code == 202
    await asyncio.wait_for(entered.wait(), 5)
    authority = next(iter(app.state.device_setups.authorities.values()))
    assert not hasattr(authority.request, "_body") and not hasattr(authority.request, "_json")
    bridge.owner_valid_until = time.time() + 1
    watcher = asyncio.create_task(bridge.watch_owner())
    try:
        async with asyncio.timeout(10):
            while bridge.owner_valid_until < time.time() + 5:
                await asyncio.sleep(.05)
        release.set()
        await complete(app)
        assert (await host.get(path)).json()["state"] == "configured" and len(nas.posts) == 2
    finally:
        release.set()
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


async def test_background_approval_never_retains_cached_credential_request(system, nas):  # noqa: F811
    app = system[0]
    entered, release = asyncio.Event(), asyncio.Event()
    async def held_backup():
        entered.set()
        await release.wait()
    nas.backup_hook = held_backup
    task = asyncio.create_task(sidebar_waiting(system, nas))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        authority = next(iter(app.state.device_setups.authorities.values()))
        assert not hasattr(authority.request, "_body") and not hasattr(authority.request, "_json")
    finally:
        release.set()
        await task


def test_discovery_projects_link_for_synology_only():
    ha = SynologyHA()
    for source in ["ssdp", "zeroconf"]:
        ha.flow["context"]["source"] = source
        assert project("ha_discovery", ha.flow)["confirmation_step"] == "link"
    ha.flow["step_id"] = "2sa"
    assert project("ha_discovery", ha.flow)["confirmation_step"] is None


async def test_v3_record_cannot_be_downgraded(synology_service):
    service, _, store, _ = synology_service
    setup = await propose(synology_service)
    current = service.save(record(synology_service, setup), reason_code="fixture")
    service.commit_receipt(current, ("fixture", "migration", "a" * 64))
    assert SetupRecord.model_validate(store.get(KIND, setup.id)).version == 3


V2_RECORD_KEYS = {"version", "id", "binding", "home_binding", "revision", "proposal_digest", "candidate",
    "candidate_aliases", "flow_digest", "form_digest", "identity_digest", "baseline_entry_ids", "state",
    "reason_code", "created_at", "updated_at", "expires_at", "approved_at", "backup_id", "entry_id", "phase"}
SYNOLOGY_RECORD_KEYS = {"input_step", "input_error", "share_options", "backup_path", "post_step", "step_posts"}


@pytest.mark.parametrize("domain", ["ipp", "shelly"])
@pytest.mark.parametrize("persistence", ["save", "commit_receipt"])
async def test_v2_persistence_keeps_exact_549b409_key_set(synology_service, domain, persistence):
    service, _, store, _ = synology_service
    service.ha = IPPHA() if domain == "ipp" else ShellyHA()
    setup = await service.create(request(), DeviceSetupCreate(candidate_id="pending", integration_domain=domain))
    current = service.record(store.get(KIND, setup.id)["binding"], setup.id)
    if persistence == "save":
        service.save(current, reason_code="fixture")
    else:
        service.commit_receipt(current, ("fixture", "v2", "a" * 64))
    raw = json.loads(store.db.execute("SELECT body FROM records WHERE kind=? AND id=?", (KIND, setup.id)).fetchone()[0])
    assert raw["version"] == 2 and set(raw) == V2_RECORD_KEYS
    assert SetupRecord.model_validate(raw).candidate.integration_domain == domain


@pytest.mark.parametrize("persistence", ["save", "commit_receipt"])
async def test_v3_persistence_keeps_synology_fields(synology_service, persistence):
    service, _, store, _ = synology_service
    setup = await waiting(synology_service, "backup_share")
    current = record(synology_service, setup)
    if persistence == "save":
        service.save(current, reason_code="fixture")
    else:
        service.commit_receipt(current, ("fixture", "v3", "a" * 64))
    raw = store.get(KIND, setup.id)
    assert raw["version"] == 3 and set(raw) == V2_RECORD_KEYS | SYNOLOGY_RECORD_KEYS
    assert raw["input_step"] == "backup_share" and raw["input_error"] is None
    assert raw["share_options"] == SHARES and raw["backup_path"] == BACKUP_PATH
    assert raw["post_step"] == "link" and raw["step_posts"] == 1


@pytest.mark.parametrize("domain", ["ipp", "shelly"])
async def test_v2_development_rows_with_synology_extras_remain_readable(synology_service, domain):
    service, _, store, _ = synology_service
    service.ha = IPPHA() if domain == "ipp" else ShellyHA()
    setup = await service.create(request(), DeviceSetupCreate(candidate_id="pending", integration_domain=domain))
    current = service.record(store.get(KIND, setup.id)["binding"], setup.id)
    store.put(KIND, current.model_dump())
    assert set(store.get(KIND, setup.id)) == V2_RECORD_KEYS | SYNOLOGY_RECORD_KEYS
    assert service.record(current.binding, setup.id) == current
    assert service.records(current.binding, for_write=True) == [current]
