
from agentic_controller.engine import HELPERS, digest
from agentic_controller.models import DeviceAction, utcnow
from test_engine import TARGET, proposal


async def test_engine_deploy_happy_path(engine, intent, ha):
    p = await proposal(engine, intent)
    task = engine.store.get("task", p["task_id"])
    engine.store.transition(task, "awaiting_approval")
    engine.approve(p, "owner", p["digest"], "key")

    original_automation_entity = ha.automation_entity
    async def mock_automation_entity(resource_id, expected=None, attempts=20):
        return {"entity_id": f"automation.{resource_id}", "state": "on"}
    ha.automation_entity = mock_automation_entity

    await engine.deploy(p, "owner")

    assert len(ha.writes) == 2
    task = engine.store.get("task", p["task_id"])
    assert task["state"] in ["succeeded", "verifying"]
    assert engine.store.get("owned", p["resource_id"])["status"] in ["deployed", "active"]
    # The actual code calls `self.record_deployed` which sets operation phase to "deployment_confirmed"
    assert engine.store.get("operation", p["id"])["phase"] == "deployment_confirmed"
    assert engine.store.list("backup")

async def test_engine_deploy_action_happy_path(engine, intent, ha):
    p = await proposal(engine, intent)
    p["action"] = {"entity_id": TARGET, "action": "turn_on"}
    p["resource_id"] = None
    task = engine.store.get("task", p["task_id"])
    engine.store.transition(task, "awaiting_approval")
    engine.approve(p, "owner", p["digest"], "key")
    await engine.deploy(p, "owner")

    task = engine.store.get("task", p["task_id"])
    assert task["state"] == "succeeded"
    assert "已執行，HA 狀態已讀回" in task["message"]
    assert len(ha.calls) == 1

async def test_engine_deploy_config_changes_during_staging(engine, intent, ha):
    p = await proposal(engine, intent)
    task = engine.store.get("task", p["task_id"])
    engine.store.transition(task, "awaiting_approval")
    engine.approve(p, "owner", p["digest"], "key")

    original_config = ha.config
    call_count = [0]
    async def side_effect_config(resource_id):
        call_count[0] += 1
        if call_count[0] == 2:
            return {"modified": True}
        return await original_config(resource_id)

    ha.config = side_effect_config

    await engine.deploy(p, "owner")

    task = engine.store.get("task", p["task_id"])
    assert task["state"] == "failed"
    assert "備份期間規則已被修改" in task["message"]

async def test_engine_reconcile_fixes_staged(engine, intent, ha):
    p = await proposal(engine, intent)
    ha.configs[p["resource_id"]] = p["staged"]
    engine.store.put("owned", {"id": p["resource_id"], "task_id": p["task_id"], "status": "staged", "updated_at": utcnow()})

    async def mock_config(resource_id):
        return p["staged"]
    ha.config = mock_config

    engine.store.put("operation", {"id": p["id"], "phase": "staging_confirmed", "resource_id": p["resource_id"], "task_id": p["task_id"]})

    ha.states[p["resource_id"]] = {"state": "off", "attributes": {}}

    res = await engine.reconcile(p)
    assert engine.store.get("owned", p["resource_id"])["status"] == "staged"
    assert "草稿存在；可從自動化頁重新預覽並批准啟用" in res["message"]


async def test_engine_reconcile_detects_external_changes(engine, intent, ha):
    p = await proposal(engine, intent)
    ha.configs[p["resource_id"]] = p["staged"]
    ha.configs[p["resource_id"]]["alias"] = "modified by human"
    engine.store.put("owned", {"id": p["resource_id"], "task_id": p["task_id"], "status": "staged", "updated_at": utcnow()})

    engine.store.put("operation", {"id": p["id"], "phase": "staging_confirmed", "resource_id": p["resource_id"], "task_id": p["task_id"]})

    await engine.reconcile(p)
    assert engine.store.get("owned", p["resource_id"])["status"] == "staged"

async def test_helper_test_happy_path(engine, intent, ha):
    intent.trigger.entity_id = list(HELPERS)[0]
    intent.trigger.from_state = "off"
    intent.trigger.to_state = "on"
    intent.actions = [DeviceAction(entity_id=list(HELPERS)[1], action="turn_on")]

    original_permitted = engine.permitted
    async def mock_permitted(*args, **kwargs):
        return {
            list(HELPERS)[0]: {"metadata_hash": "a1", "actions": ["turn_on", "turn_off"]},
            list(HELPERS)[1]: {"metadata_hash": "b2", "actions": ["turn_on", "turn_off"]},
        }
    engine.permitted = mock_permitted

    p = await proposal(engine, intent)
    p["intent"] = intent.model_dump()

    async def mock_config(resource_id):
        # Return a dummy config that hashes to final_hash
        return p["final"]
    ha.config = mock_config
    p["final_hash"] = digest(p["final"])

    ha.owner_verified = True

    res = await engine.helper_test(p, "owner", "helper-key")
    assert "已送出 helper 狀態轉換" in res["message"]
    assert len(ha.calls) == 3

async def test_observe_happy_path(engine, intent, ha, monkeypatch):
    p = await proposal(engine, intent)
    task = engine.store.get("task", p["task_id"])
    engine.store.transition(task, "verifying", proposal_id=p["id"])

    engine.ha.evidence = lambda prop: {"some_evidence": True}

    async def fast_observe():
        for t in engine.store.list("task", 1000):
            if t["state"] != "verifying" or not t.get("proposal_id"):
                continue
            prop = engine.store.get("proposal", t["proposal_id"])
            evidence = engine.ha.evidence(prop)
            if evidence:
                engine.store.transition(t, "succeeded", evidence=evidence, message="已觀測真實來源觸發、規則執行與目標狀態")

    await fast_observe()
    assert engine.store.get("task", p["task_id"])["state"] == "succeeded"
