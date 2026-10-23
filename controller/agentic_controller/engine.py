from __future__ import annotations

import asyncio
import time
import uuid

from .ha import HAError
from .models import (
    HELPERS,
    AutomationIntent,
    DeviceAction,
    Policy,
    compile_automation,
    digest,
    utcnow,
)
from .store import Conflict

BACKUP_FAILED = "Home Assistant 備份沒有完成，沒有變更家庭；請稍後再試一次。"


class Rejected(Exception):
    pass


class Engine:
    def __init__(self, store, ha):
        self.store, self.ha = store, ha
        # Device actions, automation changes, helper tests and reconciliation
        # share one writer through send and readback; helper tests span devices.
        self.writer = asyncio.Lock()

    def policy(self):
        return Policy.model_validate(self.store.setting("policy", {"entities": []}))

    async def permitted(self, ids, writes=(), *, available=True):
        permissions = {p.entity_id: p for p in self.policy().entities}
        inventory = {e["entity_id"]: e for e in await self.ha.inventory()}
        for id in ids:
            if id not in permissions or not permissions[id].read:
                raise Rejected(f"尚未授權讀取 {id}")
            if id not in inventory:
                raise Rejected(f"設備不存在：{id}")
            if available and inventory[id]["availability"] != "available":
                raise Rejected(f"設備目前無法確認狀態：{id}")
        for id in writes:
            p = permissions[id]
            if p.write == "none" or not inventory[id]["actions"]:
                raise Rejected(f"尚未授權操作 {id}")
            if id.split(".")[0] not in {"light", "switch", "input_boolean"}:
                raise Rejected("此設備類型不支援操作")
        return {id: inventory[id] for id in ids}

    def task(self, text, provider, id=None):
        task = {"id": id or uuid.uuid4().hex, "request": text, "provider": provider,
                "state": "queued", "created_at": utcnow(), "updated_at": utcnow(),
                "message": "正在排入佇列", "proposal_id": None}
        self.store.put("task", task)
        self.store.event(task["id"], "queued", {})
        return task

    async def propose(self, task, intent: AutomationIntent | None = None, action: DeviceAction | None = None,
                      *, mode="upsert", resource_id=None, commit_guard=None):
        prior_intent = None
        if action:
            entities = await self.permitted({action.entity_id}, {action.entity_id})
            if action.action not in entities[action.entity_id]["actions"]:
                raise Rejected("Home Assistant 未提供這個動作")
            final, prior, resource_id = None, None, action.entity_id
        else:
            if mode != "upsert":
                owned = self.store.get("owned", resource_id)
                if not owned:
                    raise Rejected("只能管理本產品建立的規則")
                intent = AutomationIntent.model_validate(
                    owned.get("prior_intent") if mode == "rollback" and owned.get("prior") else owned["intent"])
            resource_id = resource_id or intent.resource_id or "agentic_" + uuid.uuid4().hex
            entities = await self.permitted({intent.trigger.entity_id, *(a.entity_id for a in intent.actions)},
                                            {a.entity_id for a in intent.actions})
            for a in intent.actions:
                if a.action not in entities[a.entity_id]["actions"]:
                    raise Rejected("Home Assistant 未提供這個動作")
            semantic = digest(intent.model_dump(exclude={"title", "resource_id"}))
            if mode == "upsert" and intent.resource_id is None:
                for owned in self.store.list("owned", 10000):
                    if owned.get("semantic") == semantic and owned.get("status") == "deployed":
                        live = await self.ha.config(owned["id"])
                        if digest(live) == owned["last_hash"]:
                            if commit_guard:
                                await commit_guard()
                            return self.store.transition(task, "succeeded", resource_id=owned["id"],
                                message="相同規則已存在，未建立重複自動化", verification="existing_config_readback")
            prior = await self.ha.config(resource_id)
            owned = self.store.get("owned", resource_id)
            prior_intent = owned["intent"] if owned else None
            if prior is not None and (not owned or owned.get("last_hash") != digest(prior)):
                raise Rejected("規則由其他來源建立或已被外部修改；請先核對差異")
            if prior is None and (intent.resource_id or mode != "upsert"):
                raise Rejected("要修改的規則已不存在")
            final = compile_automation(intent, resource_id)
            if mode in {"enable", "disable"}:
                final = {**prior}
                final.pop("initial_state", None)
                if mode == "disable":
                    final["initial_state"] = False
            elif mode == "rollback":
                final = owned.get("prior") or {**prior, "initial_state": False}
            await self.ha.validate(final)
            # Dedicated helpers must not already affect unrelated automations.
            helpers = set(entities) & HELPERS
            if helpers:
                conflicts = await self.ha.dependencies(helpers, {o["id"] for o in self.store.list("owned", 10000)})
                if conflicts:
                    raise Rejected("測試 helper 已被其他規則使用：" + ", ".join(conflicts))

        proposal = {"id": uuid.uuid4().hex, "task_id": task["id"], "revision": 1,
                    "mode": "action" if action else mode, "resource_id": resource_id,
                    "intent": intent.model_dump() if intent else None,
                    "action": action.model_dump() if action else None,
                    "prior": prior, "prior_intent": prior_intent, "base_hash": digest(prior), "final": final,
                    "final_hash": digest(final), "staged": {**final, "initial_state": False} if final else None,
                    "policy_hash": digest(self.policy().model_dump()),
                    "inventory": {id: e["metadata_hash"] for id, e in entities.items()},
                    "created_at": utcnow(), "expires": time.time() + 600,
                    "risk": "single_action" if action else "persistent_automation",
                    "operations": ([action.model_dump()] if action else
                        ["建立 HA 備份", "停用狀態寫入並讀回", "寫入最終設定並讀回",
                         "停用規則" if final.get("initial_state") is False else "啟用規則", "觀測真實觸發"]),
                    "verification_plan": "觀測來源狀態轉換、規則 context 與目標狀態；不以 API 成功代替觸發",
                    "rollback_condition": "live config 必須仍等於本產品最後寫入的 hash"}
        proposal["digest"] = digest(proposal)
        if commit_guard:
            await commit_guard()
        self.store.put("proposal", proposal)
        return self.store.transition(task, "awaiting_approval", proposal_id=proposal["id"],
                                     message="請先看這份提案；批准前不會改家庭。")

    async def fresh(self, proposal, principal):
        if self.store.setting("updater_gate"):
            raise Rejected("update_in_progress")
        approval = self.store.get("approval", proposal["id"])
        if not approval or approval["principal"] != principal or approval["digest"] != proposal["digest"]:
            raise Rejected("缺少與這份提案相符的批准")
        if time.time() > approval["expires"]:
            raise Rejected("批准已過期，請重新產生提案")
        if not await self.ha.owner(principal):
            raise Rejected("無法重新確認 HA owner 身份")
        if not proposal["action"]:
            await self.ha.ensure_write_compatibility()
        if digest(self.policy().model_dump()) != proposal["policy_hash"]:
            raise Rejected("設備授權已變更，請重新產生提案")
        writes = {proposal["action"]["entity_id"]} if proposal["action"] else {
            a["entity_id"] for a in proposal["intent"]["actions"]}
        current = await self.permitted(set(proposal["inventory"]), writes)
        if self.store.setting("updater_gate"):
            raise Rejected("update_in_progress")
        if any(current[id]["metadata_hash"] != h for id, h in proposal["inventory"].items()):
            raise Rejected("設備所屬裝置或房間已變更，請重新產生提案")

    def approve(self, proposal, principal, presented_digest, key):
        previous = self.store.request("approval", key, digest([proposal["id"], presented_digest, principal]))
        if previous:
            return False
        if presented_digest != proposal["digest"] or time.time() > proposal["expires"]:
            raise Rejected("提案已變更或過期，請重新預覽")
        task = self.store.get("task", proposal["task_id"])
        if task["state"] != "awaiting_approval":
            raise Conflict("此提案已處理")
        self.store.put("approval", {"id": proposal["id"], "principal": principal,
                                   "digest": proposal["digest"], "expires": time.time() + 600,
                                   "approved_at": utcnow()})
        self.store.request("approval", key, digest([proposal["id"], presented_digest, principal]), proposal["id"])
        self.store.transition(task, "executing", message="已批准，準備備份與部署")
        return True

    def journal(self, proposal, phase, **fields):
        op = self.store.get("operation", proposal["id"]) or {"id": proposal["id"],
             "resource_id": proposal["resource_id"], "task_id": proposal["task_id"]}
        self.store.put("operation", {**op, **fields, "phase": phase, "updated_at": utcnow()})
        self.store.event(proposal["task_id"], phase, fields)

    async def deploy(self, proposal, principal):
        mutated = False
        async with self.writer:
            task = self.store.get("task", proposal["task_id"])
            try:
                await self.fresh(proposal, principal)
                if proposal["action"]:
                    action = proposal["action"]
                    self.journal(proposal, "service_prepared")
                    mutated = True
                    await self.ha.service(action["entity_id"].split(".")[0], action["action"], action["entity_id"])
                    self.journal(proposal, "service_acknowledged")
                    observed = await self.ha.request("GET", "/states/" + action["entity_id"])
                    expected = "on" if action["action"] == "turn_on" else "off"
                    if observed["state"] != expected:
                        raise HAError("動作已回覆，但目標尚未反映預期狀態")
                    self.store.transition(task, "succeeded", message="已執行，HA 狀態已讀回",
                        evidence={"entity_id": action["entity_id"], "state": observed["state"], "at": utcnow(),
                                  "kind": "state_readback", "physical_effect": "unknown"})
                    return

                resource_id = proposal["resource_id"]
                if digest(await self.ha.config(resource_id)) != proposal["base_hash"]:
                    raise Rejected("批准後規則已被修改，未送出寫入")
                self.journal(proposal, "backup_prepared")
                try:
                    backup = await self.ha.backup("Agentic Home " + proposal["id"][:12])
                except HAError as exc:
                    # Nothing is written before the backup exists. The Supervisor's own words do not help the owner.
                    raise HAError(BACKUP_FAILED) from exc
                self.store.put("backup", backup)
                await self.store.snapshot(self.store.path.parent / "snapshots" / (proposal["id"] + ".sqlite3"))
                self.journal(proposal, "backup_completed", backup_id=backup["id"])
                await self.fresh(proposal, principal)
                if digest(await self.ha.config(resource_id)) != proposal["base_hash"]:
                    raise Rejected("備份期間規則已被修改，未送出寫入")
                self.journal(proposal, "staging_prepared", expected_hash=digest(proposal["staged"]))
                mutated = True
                await self.ha.write(resource_id, proposal["staged"])
                if digest(await self.ha.config(resource_id)) != digest(proposal["staged"]):
                    raise HAError("草稿讀回不一致，需要核對")
                entity = await self.ha.automation_entity(resource_id, expected=proposal["staged"])
                await self.fresh(proposal, principal)
                await self.ha.service("automation", "turn_off", entity["entity_id"])
                self.journal(proposal, "staging_confirmed")
                await self.fresh(proposal, principal)
                if digest(await self.ha.config(resource_id)) != digest(proposal["staged"]):
                    raise Rejected("草稿被外部修改，停止部署")
                self.journal(proposal, "final_prepared", expected_hash=proposal["final_hash"])
                await self.ha.write(resource_id, proposal["final"])
                if digest(await self.ha.config(resource_id)) != proposal["final_hash"]:
                    raise HAError("最終設定讀回不一致，需要核對")
                entity = await self.ha.automation_entity(resource_id, expected=proposal["final"])
                await self.fresh(proposal, principal)
                self.journal(proposal, "activation_prepared")
                desired = "off" if proposal["final"].get("initial_state") is False else "on"
                await self.ha.service("automation", "turn_" + desired, entity["entity_id"])
                for _ in range(20):
                    observed = await self.ha.request("GET", "/states/" + entity["entity_id"])
                    if observed["state"] == desired:
                        break
                    await asyncio.sleep(0.5)
                else:
                    raise HAError("規則啟用狀態未反映，需要核對")
                if digest(await self.ha.config(resource_id)) != proposal["final_hash"]:
                    raise HAError("啟用期間設定被改動，需要核對")
                self.record_deployed(proposal, observed)
            except (HAError, Rejected, OSError) as exc:
                self.store.transition(task, "needs_reconcile" if mutated else "failed", message=str(exc))

    def record_deployed(self, proposal, entity):
        proposal = {**proposal, "deployed_at": proposal.get("deployed_at") or utcnow()}
        self.store.put("proposal", proposal)
        self.store.put("owned", {"id": proposal["resource_id"], "intent": proposal["intent"],
            "last_hash": proposal["final_hash"], "last_config": proposal["final"], "prior": proposal["prior"],
            "prior_intent": proposal["prior_intent"],
            "semantic": digest({k: v for k, v in proposal["intent"].items() if k not in {"title", "resource_id"}}),
            "status": "deployed", "entity_id": entity["entity_id"], "proposal_id": proposal["id"],
            "enabled": entity["state"] == "on", "deployed_at": proposal["deployed_at"]})
        self.journal(proposal, "deployment_confirmed", config_hash=proposal["final_hash"],
                     entity_id=entity["entity_id"], enabled=entity["state"] == "on")
        task = self.store.get("task", proposal["task_id"])
        disabled = entity["state"] == "off"
        self.store.transition(task, "succeeded" if disabled else "verifying",
            message="規則已停用並讀回" if disabled else "已部署並啟用，正在等待真實狀態觸發", resource_id=proposal["resource_id"])

    async def reconcile(self, proposal):
        async with self.writer:
            if proposal["action"]:
                state = await self.ha.request("GET", "/states/" + proposal["action"]["entity_id"])
                return {"state": state["state"], "message": "僅確認目前狀態；無法判定先前動作是否執行，不重送"}
            operation = self.store.get("operation", proposal["id"])
            if not operation or operation.get("phase") in {"backup_prepared", "backup_completed"}:
                raise Rejected("沒有此提案的寫入紀錄，不能認領現有規則")
            live = await self.ha.config(proposal["resource_id"])
            if digest(live) == proposal["final_hash"]:
                entity = await self.ha.automation_entity(proposal["resource_id"], attempts=1, expected=proposal["final"])
                desired = "off" if proposal["final"].get("initial_state") is False else "on"
                if entity["state"] == desired:
                    self.record_deployed(proposal, entity)
                    return {"message": "讀回最終設定與啟用狀態，已恢復追蹤"}
            if digest(live) == digest(proposal["staged"]):
                # A prepared exact ID plus exact hash lets us recover ownership, not authorization.
                if operation and operation.get("phase") in {"staging_prepared", "staging_confirmed", "final_prepared"}:
                    self.store.put("owned", {"id": proposal["resource_id"], "intent": proposal["intent"],
                        "last_hash": digest(live), "last_config": live, "prior": proposal["prior"],
                        "prior_intent": proposal["prior_intent"],
                        "status": "staged", "enabled": False, "proposal_id": proposal["id"]})
                return {"message": "草稿存在；可從自動化頁重新預覽並批准啟用"}
            return {"message": "現況與原始、草稿或最終設定不同，保留現場設定", "live": live,
                    "expected": proposal["final"], "requires_review": True}

    async def helper_test(self, proposal, principal, key):
        intent = proposal.get("intent")
        if (not intent or {intent["trigger"]["entity_id"], *(a["entity_id"] for a in intent["actions"])} != HELPERS
                or intent["trigger"]["from_state"] not in {"off", "on"}
                or intent["trigger"]["to_state"] not in {"off", "on"}):
            raise Rejected("自動測試僅操作兩個專用 test helpers")
        async with self.writer:
            if self.store.request("helper_test", key, proposal["digest"]):
                return {"message": "此測試已送出，未重複觸發"}
            if not await self.ha.owner(principal):
                raise Rejected("無法確認 owner")
            if digest(await self.ha.config(proposal["resource_id"])) != proposal["final_hash"]:
                raise Rejected("規則已被修改")
            await self.permitted(HELPERS, HELPERS)
            conflicts = await self.ha.dependencies(HELPERS, {o["id"] for o in self.store.list("owned", 10000)})
            if conflicts:
                raise Rejected("測試 helper 現在被其他規則使用")
            self.store.request("helper_test", key, proposal["digest"], proposal["id"])
            self.store.event(proposal["task_id"], "helper_test_authorized", {"principal": principal})
            try:
                for action in intent["actions"]:
                    opposite = "turn_off" if action["action"] == "turn_on" else "turn_on"
                    await self.ha.service("input_boolean", opposite, action["entity_id"])
                await self.ha.service("input_boolean", "turn_" + intent["trigger"]["from_state"],
                                      intent["trigger"]["entity_id"])
                await asyncio.sleep(0.25)
                await self.ha.service("input_boolean", "turn_" + intent["trigger"]["to_state"],
                                      intent["trigger"]["entity_id"])
            except HAError:
                task = self.store.get("task", proposal["task_id"])
                self.store.transition(task, "needs_reconcile", message="測試操作回應不確定，未自動重送")
                raise
            return {"message": "已送出 helper 狀態轉換，正在觀測規則執行"}

    async def observe(self):
        while True:
            for task in self.store.list("task", 1000):
                if task["state"] != "verifying" or not task.get("proposal_id"):
                    continue
                proposal = self.store.get("proposal", task["proposal_id"])
                evidence = self.ha.evidence(proposal)
                if evidence:
                    self.store.transition(task, "succeeded", evidence=evidence,
                                          message="已觀測真實來源觸發、規則執行與目標狀態")
            await asyncio.sleep(1)
