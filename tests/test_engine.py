import asyncio
from agentic_controller.models import DeviceAction
import time

import pytest
from agentic_controller.engine import Rejected
from agentic_controller.ha import HAError
from agentic_controller.models import (
    AutomationIntent,
    EntityPermission,
    compile_automation,
    digest,
    provider_schema,
    utcnow,
)
from agentic_controller.store import Conflict, Store
from conftest import SOURCE, TARGET, proposal
from pydantic import ValidationError


def test_compiler_is_deterministic_and_restart_enabled(intent):
    first = compile_automation(intent, "agentic_" + "a" * 32)
    assert digest(first) == digest(compile_automation(intent, "agentic_" + "a" * 32))
    assert "initial_state" not in first
    assert first["actions"][0]["target"] == {"entity_id": TARGET}


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(extra_yaml="!include secrets.yaml"),
    lambda d: d["actions"][0].update(entity_id="{{ states.sensor.target }}"),
    lambda d: d["actions"][0].update(action="toggle"),
    lambda d: d["actions"][0].update(entity_id=SOURCE),
    lambda d: d["trigger"].update(from_state="unknown"),
])
def test_intent_rejects_unsafe_shapes(intent, mutate):
    data = intent.model_dump()
    mutate(data)
    with pytest.raises(ValidationError):
        AutomationIntent.model_validate(data)


@pytest.mark.parametrize("id,classification", [("lock.front_door", "lighting"),
    ("input_boolean.other", "dedicated_helper"), ("climate.heater", "low_risk_switch")])
def test_device_classification_cannot_expand_domains(id, classification):
    with pytest.raises(ValidationError):
        EntityPermission(entity_id=id, read=True, write=classification, purpose="test")


async def test_no_write_before_approval_and_deploy_is_staged(engine, intent, ha):
    p = await proposal(engine, intent)
    assert engine.store.get("task", p["task_id"])["message"] == "請先看這份提案；批准前不會改家庭。"
    assert not ha.writes and not ha.calls
    await engine.deploy(p, "owner")
    assert not ha.writes
    # A failed direct execution cannot consume a legitimate subsequent approval.
    engine.store.transition(engine.store.get("task", p["task_id"]), "awaiting_approval")
    assert engine.approve(p, "owner", p["digest"], "approve-1")
    await engine.deploy(p, "owner")
    assert len(ha.writes) == 2
    assert ha.writes[0]["initial_state"] is False
    assert "initial_state" not in ha.writes[1]
    assert engine.store.get("task", p["task_id"])["state"] == "verifying"
    assert engine.store.get("owned", p["resource_id"])["last_hash"] == digest(ha.configs[p["resource_id"]])


async def test_failed_backup_changes_nothing_and_says_so(engine, intent, ha):
    p = await proposal(engine, intent)

    async def refused(name):
        raise HAError("HA supervisor/api: unknown_error")
    ha.backup = refused
    assert engine.approve(p, "owner", p["digest"], "key")
    await engine.deploy(p, "owner")
    task = engine.store.get("task", p["task_id"])
    assert (task["state"], task["message"]) == ("failed", "Home Assistant 備份沒有完成，沒有變更家庭；請稍後再試一次。")
    assert not ha.writes and not ha.calls
    assert engine.store.get("operation", p["id"])["phase"] == "backup_prepared"


async def test_replaying_approval_does_not_repeat_writes(engine, intent, ha):
    p = await proposal(engine, intent)
    engine.approve(p, "owner", p["digest"], "key")
    await engine.deploy(p, "owner")
    assert not engine.approve(p, "owner", p["digest"], "key")
    assert len(ha.writes) == 2
    with pytest.raises(Conflict):
        engine.store.request("approval", "key", "different")
    with pytest.raises(Conflict):
        engine.approve(p, "owner", "0" * 64, "key")


async def test_rollback_restores_the_prior_intent_and_config(engine, intent, ha):
    first = await proposal(engine, intent)
    engine.approve(first, "owner", first["digest"], "first")
    await engine.deploy(first, "owner")
    modified = intent.model_copy(deep=True)
    modified.resource_id = first["resource_id"]
    modified.actions[0].action = "turn_off"
    second = await proposal(engine, modified)
    engine.approve(second, "owner", second["digest"], "second")
    await engine.deploy(second, "owner")
    task = await engine.propose(engine.task("rollback", "controller"), mode="rollback", resource_id=first["resource_id"])
    rollback = engine.store.get("proposal", task["proposal_id"])
    assert rollback["intent"]["actions"][0]["action"] == "turn_on"
    assert rollback["final"] == first["final"]
    engine.approve(rollback, "owner", rollback["digest"], "rollback")
    await engine.deploy(rollback, "owner")
    assert ha.configs[first["resource_id"]] == first["final"]
    assert engine.store.get("owned", first["resource_id"])["intent"] == first["intent"]


async def test_reconcile_cannot_claim_a_proposal_without_a_write_journal(engine, intent, ha):
    p = await proposal(engine, intent)
    ha.configs[p["resource_id"]] = p["final"]
    with pytest.raises(Rejected):
        await engine.reconcile(p)
    assert engine.store.get("owned", p["resource_id"]) is None


@pytest.mark.parametrize("reason", ["policy", "owner", "metadata", "expired", "external_edit"])
async def test_freshness_failures_prevent_writes(engine, intent, ha, reason):
    p = await proposal(engine, intent)
    engine.approve(p, "owner", p["digest"], "key")
    if reason == "policy":
        engine.store.set_setting("policy", {"entities": []})
    elif reason == "owner":
        ha.revoked = True
    elif reason == "metadata":
        ha.changed_metadata = True
    elif reason == "expired":
        a = engine.store.get("approval", p["id"])
        engine.store.put("approval", {**a, "expires": time.time() - 1})
    else:
        ha.configs[p["resource_id"]] = {"external": "changed"}
    await engine.deploy(p, "owner")
    assert not ha.writes
    assert engine.store.get("task", p["task_id"])["state"] == "failed"


async def test_lost_ack_reconciles_exact_id_without_retry(engine, intent, ha):
    p = await proposal(engine, intent)
    ha.lose_response_at = 2
    engine.approve(p, "owner", p["digest"], "key")
    await engine.deploy(p, "owner")
    assert engine.store.get("task", p["task_id"])["state"] == "needs_reconcile"
    await engine.reconcile(p)
    assert len(ha.writes) == 2
    assert engine.store.get("task", p["task_id"])["state"] == "verifying"


async def test_staging_ack_loss_recovers_ownership_but_does_not_activate(engine, intent, ha):
    p = await proposal(engine, intent)
    ha.lose_response_at = 1
    engine.approve(p, "owner", p["digest"], "key")
    await engine.deploy(p, "owner")
    await engine.reconcile(p)
    assert len(ha.writes) == 1 and not ha.calls
    assert engine.store.get("owned", p["resource_id"])["status"] == "staged"


async def test_same_behavior_does_not_create_second_automation(engine, intent, ha):
    p = await proposal(engine, intent)
    engine.approve(p, "owner", p["digest"], "key")
    await engine.deploy(p, "owner")
    task = engine.task("same request with a different title", "fixture")
    intent.title = "重新描述同一規則"
    result = await engine.propose(task, intent)
    assert result["state"] == "succeeded" and result["resource_id"] == p["resource_id"]
    assert len(ha.configs) == 1


async def test_rollback_refuses_external_changes(engine, intent, ha):
    p = await proposal(engine, intent)
    engine.approve(p, "owner", p["digest"], "key")
    await engine.deploy(p, "owner")
    ha.configs[p["resource_id"]]["alias"] = "human edit"
    task = engine.task("rollback", "controller")
    with pytest.raises(Rejected):
        await engine.propose(task, mode="rollback", resource_id=p["resource_id"])
    assert ha.configs[p["resource_id"]]["alias"] == "human edit"


async def test_prefix_does_not_grant_ownership(engine, intent, ha):
    intent.resource_id = "agentic_" + "b" * 32
    ha.configs[intent.resource_id] = compile_automation(intent, intent.resource_id)
    with pytest.raises(Rejected):
        await proposal(engine, intent)


async def test_unknown_or_unavailable_entity_blocks_proposal(engine, intent, ha):
    del ha.states[TARGET]
    with pytest.raises(Rejected):
        await proposal(engine, intent)


async def test_helper_downstream_automation_is_not_ignored(engine, intent, ha):
    ha.conflicts = ["automation.unrelated_heater"]
    with pytest.raises(Rejected):
        await proposal(engine, intent)


def test_restart_preserves_unknown_side_effect_and_sources(store):
    store.put("task", {"id": "a", "state": "executing", "sources": [SOURCE]})
    store.recover()
    task = store.get("task", "a")
    assert task["state"] == "needs_reconcile"
    store.transition({"id": "a"}, "verifying", message="readback")
    assert store.get("task", "a")["sources"] == [SOURCE]


async def test_snapshot_contains_wal_commits(store, tmp_path):
    store.set_setting("marker", "committed")
    path = tmp_path / "backup.sqlite3"
    await store.snapshot(path)
    restored = Store(path)
    assert restored.setting("marker") == "committed"
    restored.close()


async def test_verification_requires_source_and_correlated_target(engine, intent, ha):
    p = await proposal(engine, intent)
    p["deployed_at"] = utcnow()
    def event(id, old, new, context, attributes=None):
        return {"time_fired": utcnow(), "context": context, "data": {"entity_id": id,
            "old_state": {"state": old}, "new_state": {"state": new, "context": context, "attributes": attributes or {}}}}
    source = event(SOURCE, "off", "on", {"id": "source"})
    auto = event("automation.rule", "on", "on", {"id": "run", "parent_id": "source"}, {"id": p["resource_id"]})
    target = event(TARGET, "off", "on", {"id": "unrelated"})
    ha.observations = [source, auto, target]
    assert ha.evidence(p) is None
    target["data"]["new_state"]["context"]["id"] = "run"
    assert ha.evidence(p)["targets"][0]["entity_id"] == TARGET
    ha.observations = [auto, target]
    assert ha.evidence(p) is None


def test_provider_schema_has_closed_required_objects():
    schema = provider_schema()
    assert set(schema["required"]) == set(schema["properties"])
    for definition in schema["$defs"].values():
        assert definition["additionalProperties"] is False
        assert set(definition["required"]) == set(definition["properties"])


async def test_device_action_serialization_and_supersede(engine, ha):
    original_inventory = ha.inventory
    async def mock_inventory():
        inv = await original_inventory()
        inv.extend([
            {"entity_id": "light.device_1", "domain": "light", "name": "light.device_1", "area": None, "state": "off", "availability": "available", "actions": ["turn_on", "turn_off"], "observed_at": "now", "metadata_hash": "a"},
            {"entity_id": "light.device_2", "domain": "light", "name": "light.device_2", "area": None, "state": "off", "availability": "available", "actions": ["turn_on", "turn_off"], "observed_at": "now", "metadata_hash": "b"}
        ])
        return inv
    ha.inventory = mock_inventory
    ha.states["light.device_1"] = "off"
    ha.states["light.device_2"] = "off"
    engine.store.set_setting("policy", {
        "entities": [
            {"entity_id": "light.device_1", "read": True, "write": "lighting", "purpose": "test"},
            {"entity_id": "light.device_2", "read": True, "write": "lighting", "purpose": "test"}
        ]
    })

    # 1. Propose and approve first command
    task1 = engine.task("cmd1", "test")
    task1 = await engine.propose(task1, action=DeviceAction(entity_id="light.device_1", action="turn_on"))
    p1 = engine.store.get("proposal", task1["proposal_id"])
    engine.approve(p1, "owner", p1["digest"], "key1")

    # 2. Propose and approve second command
    task2 = engine.task("cmd2", "test")
    task2 = await engine.propose(task2, action=DeviceAction(entity_id="light.device_1", action="turn_off"))
    p2 = engine.store.get("proposal", task2["proposal_id"])
    engine.approve(p2, "owner", p2["digest"], "key2")

    # We want deploy1 to hold the lock for a bit.
    original_service = ha.service
    device_1_first_call_started = asyncio.Event()
    device_1_delay_event = asyncio.Event()

    async def delayed_service(domain, service, entity_id):
        if entity_id == "light.device_1" and not device_1_first_call_started.is_set():
            device_1_first_call_started.set()
            await device_1_delay_event.wait()
        return await original_service(domain, service, entity_id)

    ha.service = delayed_service

    t1 = asyncio.create_task(engine.deploy(p1, "owner"))

    # Wait until it enters delayed_service
    await device_1_first_call_started.wait()

    # Now it holds the lock. Start deploy2.
    t2 = asyncio.create_task(engine.deploy(p2, "owner"))
    await asyncio.sleep(0.05)

    # Let deploy1 finish.
    device_1_delay_event.set()

    await t1
    await t2

    # p2 was accepted after p1, so p2 has a higher sequence.
    # Therefore both p1 and p2 should have executed, meaning final state is off (from p2).
    assert engine.store.get("task", p1["task_id"])["state"] == "succeeded"
    assert engine.store.get("task", p2["task_id"])["state"] == "succeeded"
    assert ha.states["light.device_1"] == "off"

    # Now let's test that an older command gets superseded.
    # p3 will be older, p4 newer.
    task3 = engine.task("cmd3", "test")
    task3 = await engine.propose(task3, action=DeviceAction(entity_id="light.device_1", action="turn_on"))
    p3 = engine.store.get("proposal", task3["proposal_id"])
    engine.approve(p3, "owner", p3["digest"], "key3")

    task4 = engine.task("cmd4", "test")
    task4 = await engine.propose(task4, action=DeviceAction(entity_id="light.device_1", action="turn_off"))
    p4 = engine.store.get("proposal", task4["proposal_id"])
    engine.approve(p4, "owner", p4["digest"], "key4")

    # Deploy p4 FIRST.
    await engine.deploy(p4, "owner")

    # Now deploy p3. It should be superseded!
    await engine.deploy(p3, "owner")

    assert engine.store.get("task", p4["task_id"])["state"] == "succeeded"
    assert engine.store.get("task", p3["task_id"])["state"] == "superseded"
    assert engine.store.get("task", p3["task_id"])["message"] == "被較新的指令覆蓋，不執行"


async def test_device_action_concurrent_different_devices(engine, ha):
    original_inventory = ha.inventory
    async def mock_inventory():
        inv = await original_inventory()
        inv.extend([
            {"entity_id": "light.device_1", "domain": "light", "name": "light.device_1", "area": None, "state": "off", "availability": "available", "actions": ["turn_on", "turn_off"], "observed_at": "now", "metadata_hash": "a"},
            {"entity_id": "light.device_2", "domain": "light", "name": "light.device_2", "area": None, "state": "off", "availability": "available", "actions": ["turn_on", "turn_off"], "observed_at": "now", "metadata_hash": "b"}
        ])
        return inv
    ha.inventory = mock_inventory
    ha.states["light.device_1"] = "off"
    ha.states["light.device_2"] = "off"
    engine.store.set_setting("policy", {
        "entities": [
            {"entity_id": "light.device_1", "read": True, "write": "lighting", "purpose": "test"},
            {"entity_id": "light.device_2", "read": True, "write": "lighting", "purpose": "test"}
        ]
    })

    # 1. Propose and approve first command for device 1
    task1 = engine.task("cmd1", "test")
    task1 = await engine.propose(task1, action=DeviceAction(entity_id="light.device_1", action="turn_on"))
    p1 = engine.store.get("proposal", task1["proposal_id"])
    engine.approve(p1, "owner", p1["digest"], "key1")

    # 2. Propose and approve second command for device 2
    task2 = engine.task("cmd2", "test")
    task2 = await engine.propose(task2, action=DeviceAction(entity_id="light.device_2", action="turn_on"))
    p2 = engine.store.get("proposal", task2["proposal_id"])
    engine.approve(p2, "owner", p2["digest"], "key2")

    # We want deploy1 to hold the lock for a bit.
    original_service = ha.service
    device_1_first_call_started = asyncio.Event()
    device_1_delay_event = asyncio.Event()

    async def delayed_service(domain, service, entity_id):
        if entity_id == "light.device_1" and not device_1_first_call_started.is_set():
            device_1_first_call_started.set()
            await device_1_delay_event.wait()
        return await original_service(domain, service, entity_id)

    ha.service = delayed_service

    t1 = asyncio.create_task(engine.deploy(p1, "owner"))

    # Wait until it enters delayed_service
    await device_1_first_call_started.wait()

    # Now it holds the lock for device_1. Start deploy for device_2.
    t2 = asyncio.create_task(engine.deploy(p2, "owner"))

    # t2 should not be blocked and should finish quickly
    await asyncio.wait_for(t2, timeout=1.0)

    # Let deploy1 finish.
    device_1_delay_event.set()

    await t1

    assert engine.store.get("task", p1["task_id"])["state"] == "succeeded"
    assert engine.store.get("task", p2["task_id"])["state"] == "succeeded"
    assert ha.states["light.device_1"] == "on"
    assert ha.states["light.device_2"] == "on"
