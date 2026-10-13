"""Durable CLI-first setup. Only fixed service adapters can perform setup actions.

The additive bootstrap_v1 record is independent of household policy and approvals.
GET reads it without network calls or mutations. Explicit start authorizes the fixed
workflow; lifespan recovery resumes observations and exact persisted operation IDs.
"""
from __future__ import annotations

import asyncio
import re
import time
import uuid

from fastapi import HTTPException, Request

from . import host
from .ha import HAError
from .hermes import HermesError
from .models import (
    BootstrapHumanAction,
    BootstrapPreferences,
    BootstrapRetry,
    BootstrapStart,
    BootstrapStatus,
    BootstrapStep,
    ChatInput,
    digest,
    utcnow,
)
from .runner import RunnerError

STEPS = ("network", "runner_pairing", "cli_auth", "cli_assist", "hermes_install",
         "hermes_auth", "channel", "ha_provision", "ha_native")
VERIFY_MESSAGE = "這是 Hermes 模型連線驗證。請僅回覆「Hermes 已可回應」，不要呼叫工具、修改設定或操作家庭。"
OBSERVATION_TIMEOUT = 120
# Same tokens as the Runner. The controller image does not import the runner package.
AUTH_MESSAGES = {
    "needs_reauth": "登入已失效，請重新登入。",
    "rate_limited": "已達用量上限，請稍後再試。",
    "invalid_credentials": "登入資訊無效，請重新輸入。",
    "network": "無法連線到 AI 服務，請檢查網路後再試。",
    "cli_output_unrecognized": "無法確認登入結果，請稍後再試。",
    "timeout": "AI 帳號回應逾時，請稍後再試。",
}
RETRY_CODES = {
    "cli_assist_failed", "cli_assist_expired", "hermes_verification_failed",
    "hermes_install_failed", "ha_provision_failed", "hermes_rate_limited",
    "hermes_provider_unavailable", "hermes_verification_timeout", *AUTH_MESSAGES,
}


class SetupPaused(Exception):
    """A pause observed after an await must fence the next setup effect."""


class Bootstrap:
    def __init__(self, store, runner, coordinator, gateway, ha, controller_id, *, host_request=None):
        self.store, self.runner, self.coordinator, self.gateway = store, runner, coordinator, gateway
        self.ha, self.controller_id = ha, controller_id
        self.host_request = host_request or host.host_call
        self.lock, self.task = asyncio.Lock(), None

    def record(self):
        return self.store.get("bootstrap_v1", "setup")

    def public(self):
        record = self.record()
        if not record:
            return BootstrapStatus(steps=[BootstrapStep(id=step) for step in STEPS]).model_dump()
        return BootstrapStatus.model_validate({key: value for key, value in record.items()
                                              if key in BootstrapStatus.model_fields}).model_dump()

    def save(self, record):
        record["updated_at"] = utcnow()
        self.store.put("bootstrap_v1", record)

    def require_active(self):
        if self.store.setting("paused", False):
            raise SetupPaused

    def checkpoint(self, record, step, state, code=None, action=None):
        if state in {"completed", "skipped"}:
            self.require_active()
        target = next(item for item in record["steps"] if item["id"] == step)
        verified = target.get("verified_at") or utcnow() if state in {"completed", "skipped"} else None
        target.update(state=state, code=code, verified_at=verified)
        if state in {"completed", "skipped"}:
            record.pop("transient_errors", None)
        record.update(current_step=step, human_action=action,
                      state=state if state in {"waiting_input", "blocked"} else "running")
        self.save(record)

    @staticmethod
    def human(kind, message, **fields):
        return BootstrapHumanAction(kind=kind, message=message, **fields).model_dump()

    def block(self, record, step, code, message):
        self.checkpoint(record, step, "blocked", code, self.human("retry", message, code=code))

    def wait_for_observation(self, record, step, name, code, message, *, identity=None):
        waits = record.setdefault("observation_waits", {})
        pending = waits.get(name)
        if not pending or pending.get("identity") != identity:
            pending = {"deadline": time.time() + OBSERVATION_TIMEOUT, "identity": identity}
            waits[name] = pending
        if time.time() >= pending["deadline"]:
            self.block(record, step, name + "_timeout", message)
        else:
            self.checkpoint(record, step, "running", code)

    def resume(self):
        record = self.record()
        if record and record["state"] in {"running", "waiting_input"} and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def run(self):
        while self.record() and self.record()["state"] in {"running", "waiting_input"}:
            await self.advance()
            await asyncio.sleep(3)

    async def start(self, body, key):
        async with self.lock:
            previous = self.store.request("bootstrap_start", key, digest(body.model_dump()))
            record = self.record()
            if record and (record["provider"] != body.provider or record["channel"] != body.channel) and not previous:
                raise HTTPException(409, "設定流程已開始；通訊選擇請使用 preferences，不會替換正在工作的 CLI")
            if not record:
                if self.store.setting("paused", False):
                    raise HTTPException(409, "請先恢復 AI，再開始設定")
                record = {**BootstrapStatus(provider=body.provider, channel=body.channel,
                    steps=[BootstrapStep(id=step) for step in STEPS]).model_dump(),
                    "id": "setup", "data_contract": 1, "run_id": uuid.uuid4().hex, "state": "running",
                    "current_step": "network", "jobs": {}, "attempts": {}, "bridge_auto_enroll": True}
                self.save(record)
                # Selecting the external audience is still a separate authenticated action.
                self.store.set_setting("gateway", {**self.gateway.preferences().model_dump(),
                    "primary": body.channel, "provider": body.provider, "runtime": "host", "coordinator": "hermes"})
                self.store.set_setting("gateway_selected", True)
            self.store.request("bootstrap_start", key, digest(body.model_dump()), record["run_id"])
        self.resume()
        return self.public()

    async def retry(self, key):
        async with self.lock:
            record = self.record()
            if not record:
                raise HTTPException(409, "請先開始設定")
            previous = self.store.request("bootstrap_retry", key, digest({}))
            if not previous:
                step = record["current_step"]
                if record["state"] == "blocked":
                    current = next(item for item in record["steps"] if item["id"] == step)
                    if current["code"] in {"hermes_revision_timeout", "hermes_runtime_timeout", "hermes_readback_timeout"}:
                        # Retry only the observation window. Keep the confirmed
                        # install job and exact inference event; do not replay either.
                        record.get("observation_waits", {}).pop(current["code"].removesuffix("_timeout"), None)
                    # Only terminal failed jobs get a new identity. Lost responses and
                    # unconfirmed side effects retain the old handle for reconciliation.
                    if current["code"] in RETRY_CODES:
                        record["attempts"][step] = record["attempts"].get(step, 0) + 1
                        if step == "cli_assist":
                            record.pop("assist_request", None)
                        if step == "hermes_auth":
                            record.pop("verification", None)
                        for operation in list(record["jobs"]):
                            if operation.startswith(step + ":") and record["jobs"][operation].get("state") == "failed":
                                record["jobs"].pop(operation)
                    record.pop("transient_errors", None)
                    self.checkpoint(record, step, "running", "retry_requested")
                self.store.request("bootstrap_retry", key, digest({}), record["run_id"])
        self.resume()
        return self.public()

    async def preferences(self, body, key):
        async with self.lock:
            record = self.record()
            if not record:
                raise HTTPException(409, "請先開始設定")
            previous = self.store.request("bootstrap_preferences", key, digest(body.model_dump()))
            if not previous:
                # A channel change cannot retract an already issued HA operation.
                if any(step["state"] != "pending" for step in record["steps"] if step["id"] in {"ha_provision", "ha_native"}):
                    raise HTTPException(409, "家庭啟動已開始；請在通訊設定中變更通道")
                if record["channel"] != body.channel and record["channel"] in {"line", "telegram"}:
                    await self.gateway.cancel_pairing(record["channel"])
                record["channel"] = body.channel
                channel = next(step for step in record["steps"] if step["id"] == "channel")
                channel.update(state="pending", code=None, verified_at=None)
                if record["current_step"] == "channel":
                    record.update(state="running", human_action=None)
                self.save(record)
                self.store.set_setting("gateway", {**self.gateway.preferences().model_dump(), "primary": body.channel})
                self.store.request("bootstrap_preferences", key, digest(body.model_dump()), record["run_id"])
        self.resume()
        return self.public()

    async def host_operation(self, record, step, action, observed):
        self.require_active()
        operation_key = step + ":" + action
        operation = record["jobs"].get(operation_key)
        if not operation:
            identifier = uuid.uuid4().hex
            operation = {"request": {"id": identifier, "action": action}, "state": "submitting"}
            record["jobs"][operation_key] = operation
            self.save(record)  # Commit identity before sending any host side effect.
        result = next((item for item in observed.get("jobs", [])
                       if item.get("id") == operation["request"]["id"]), None)
        if result is None:
            self.require_active()
            result = await self.host_request("POST", operation["request"])
        state = result.get("state")
        operation["state"] = state if state in {"running", "succeeded", "failed", "needs_reconcile"} else "unknown"
        self.save(record)
        self.require_active()  # Preserve an already dispatched handle even if pause arrived in flight.
        if state in {"needs_reconcile", "unknown"} or operation["state"] == "unknown":
            self.block(record, step, "host_needs_reconcile", "主機操作尚未確認；請檢查主機診斷，再重試相同操作。")
        elif state == "failed":
            self.block(record, step, step + "_failed", "固定主機設定未完成；請查看主機診斷後重試。")
        else:
            self.checkpoint(record, step, "running", "host_operation_pending")

    async def assist(self, record, observed):
        request = record.get("assist_request")
        if not request:
            runtime = await self.coordinator.capabilities()
            self.require_active()
            auth_state = runtime["runtime"].get("state", "unavailable")
            if auth_state not in {"needs_auth", "configured", "ready", "needs_reauth"}:
                auth_state = "unavailable"
            vm = observed.get("vm")
            request = {"id": uuid.uuid4().hex, "provider": record["provider"], "stage": "hermes_setup",
                "expires": time.time() + 300, "observations": {
                    "network_ready": True, "hermes_installed": observed["bootstrap"]["hermes"].get("state") in {"bundled", "ready"},
                    "hermes_auth_state": auth_state, "channel": record["channel"],
                    "ha_state": "running" if vm == "running" else "not_installed" if not observed.get("provisioned") else "stopped"}}
            record["assist_request"] = request
            self.save(record)
        try:
            result = await self.runner.call("GET", "/bootstrap/jobs/" + request["id"])
        except RunnerError as exc:
            if exc.status_code != 404:
                raise
            if request["expires"] <= time.time():
                self.block(record, "cli_assist", "cli_assist_expired", "尚未接單的 CLI 查證已過期，請重試。")
                return False
            self.require_active()
            result = await self.runner.call("POST", "/bootstrap/jobs", request)
        self.require_active()
        state = result.get("state")
        if state == "stop_unconfirmed":
            self.block(record, "cli_assist", "runner_needs_reconcile", "Runner 尚未確認原程序已停止；請查看診斷後重新核對原工作。")
            return False
        if state == "succeeded" and result.get("evidence", {}).get("inference_completed") is True:
            self.checkpoint(record, "cli_assist", "completed", "cli_inference_verified")
            return True
        if state in {"queued", "running"}:
            self.checkpoint(record, "cli_assist", "running", "cli_assistance_running")
        else:
            code = result.get("error")
            if code in AUTH_MESSAGES:
                self.block(record, "cli_assist", code, AUTH_MESSAGES[code])
            else:
                self.block(record, "cli_assist", "cli_assist_failed", "CLI 尚未完成實際模型查證；請檢查登入與供應商狀態後重試。")
        return False

    async def hermes_auth(self, record):
        runtime = (await self.coordinator.capabilities())["runtime"]
        self.require_active()
        if runtime.get("execution_state") == "stop_unconfirmed":
            self.block(record, "hermes_auth", "hermes_needs_reconcile", "Hermes 先前程序尚未確認停止；請查看診斷後重新核對。")
            return False
        if runtime.get("state") == "ready" and runtime.get("auth_verified_at"):
            record.get("observation_waits", {}).pop("hermes_readback", None)
            record.get("observation_waits", {}).pop("hermes_runtime", None)
            self.checkpoint(record, "hermes_auth", "completed", "hermes_inference_verified")
            return True
        if runtime.get("state") in {"needs_auth", "needs_reauth"}:
            record.get("observation_waits", {}).pop("hermes_runtime", None)
            self.checkpoint(record, "hermes_auth", "waiting_input", "hermes_login_required", self.human(
                "hermes_login", "請完成 Hermes 自己的官方登入；完成後會自動驗證模型連線。"))
            return False
        if runtime.get("state") != "configured":
            self.wait_for_observation(record, "hermes_auth", "hermes_runtime", "hermes_runtime_starting",
                                      "Hermes 服務在等待期限內未恢復；請查看診斷，重試只會重新核對狀態。")
            return False
        record.get("observation_waits", {}).pop("hermes_runtime", None)
        verification = record.get("verification")
        epoch = self.store.setting("hermes_auth_epoch")
        if not verification or verification.get("epoch") != epoch:
            verification = {"event": uuid.uuid4().hex, "epoch": epoch}
            record["verification"] = verification
            self.save(record)
        item = self.gateway.submit(ChatInput(message=VERIFY_MESSAGE, coordinator="hermes", new_conversation=True),
                                   "web", "owner", self.store.setting("owner") or "host-owner", verification["event"])
        if item["state"] in {"needs_reconcile", "stop_unconfirmed"}:
            self.block(record, "hermes_auth", "hermes_needs_reconcile", "Hermes 驗證工作尚待核對；重試會保留原工作，不重新送出推理。")
        elif item["state"] in {"failed", "cancelled", "interrupted", "succeeded"}:
            # Success text is not authority: wait for independently observed ready.
            if item["state"] != "succeeded":
                code = item.get("error_code")
                if code in {"needs_auth", "needs_reauth"}:
                    self.checkpoint(record, "hermes_auth", "waiting_input", "hermes_login_required", self.human(
                        "hermes_login", "請完成 Hermes 自己的官方登入；完成後會自動驗證模型連線。"))
                elif code == "rate_limited":
                    self.block(record, "hermes_auth", "hermes_rate_limited", "供應商暫時限制請求，請稍後再重試驗證。")
                elif code == "provider_unavailable":
                    self.block(record, "hermes_auth", "hermes_provider_unavailable", "模型服務暫時無法連線，請稍後再重試驗證。")
                elif code == "timeout":
                    self.block(record, "hermes_auth", "hermes_verification_timeout", "這次模型驗證逾時，請稍後再重試。")
                else:
                    self.block(record, "hermes_auth", "hermes_verification_failed", "Hermes 模型驗證未完成，請檢查登入後重試。")
            else:
                self.wait_for_observation(record, "hermes_auth", "hermes_readback", "hermes_readback_pending",
                    "Hermes 回覆後仍未確認登入驗證；請查看診斷，重試只會核對原工作。", identity=verification["event"])
        else:
            self.checkpoint(record, "hermes_auth", "running", "hermes_verification_running")
        return False

    def channel_ready(self, record):
        channel = record["channel"]
        if channel == "web":
            return True
        config = self.gateway.config(channel)
        return bool(config.get("enabled") and config.get("chats") and config.get("senders")
                    and self.gateway.states.get(channel) == "connected")

    async def advance(self):
        async with self.lock:
            record = self.record()
            if not record or record["state"] not in {"running", "waiting_input"}:
                return
            step = record["current_step"] or "network"
            try:
                self.require_active()
                observed = await self.host_request()
                self.require_active()
                platform = observed.get("bootstrap")
                if not isinstance(platform, dict):
                    self.block(record, step, "host_upgrade_required", "主機服務尚未提供新版設定接口。")
                    return
                for step in STEPS:
                    checkpoint = next(item for item in record["steps"] if item["id"] == step)
                    if checkpoint["state"] in {"completed", "skipped"}:
                        continue
                    # Re-observing a step is not a state change. Preserve active
                    # login/setup forms while an awaited health probe is in flight.
                    if checkpoint["state"] == "pending" or record["current_step"] != step:
                        self.checkpoint(record, step, "running")
                    if step == "network":
                        if platform.get("network", {}).get("ready") is not True:
                            self.checkpoint(record, step, "waiting_input", "network_unavailable", self.human(
                                "retry", "請接上網路；偵測到連線後會自動繼續。", code="network_unavailable"))
                            return
                        self.checkpoint(record, step, "completed", "network_verified")
                    elif step == "runner_pairing":
                        await host.pair_runner(self.store, self.runner, self.controller_id,
                                               commit_guard=self.require_active)
                        self.checkpoint(record, step, "completed", "runner_paired")
                    elif step == "cli_auth":
                        peer = await self.runner.call("GET", "/status")
                        self.require_active()
                        if peer.get("execution_state") == "stop_unconfirmed":
                            self.block(record, step, "runner_needs_reconcile", "Runner 尚未確認原程序已停止；請查看診斷後再繼續登入。")
                            return
                        provider = next((item for item in peer.get("providers", []) if item.get("provider") == record["provider"]), {})
                        if provider.get("version_matches") is not True:
                            self.block(record, step, "cli_version_mismatch", "所選 CLI 尚未通過套件版本檢查。")
                            return
                        if provider.get("status") != "connected":
                            reason = provider.get("reason")
                            if reason in AUTH_MESSAGES:
                                message = AUTH_MESSAGES[reason]
                                action = self.human("cli_login", message, provider=record["provider"], code=reason)
                            else:
                                action = self.human(
                                    "cli_login", "請完成所選 CLI 的官方登入；完成後會自動繼續。", provider=record["provider"])
                            self.checkpoint(record, step, "waiting_input", "cli_login_required", action)
                            return
                        self.checkpoint(record, step, "completed", "cli_login_observed")
                    elif step == "cli_assist":
                        if not await self.assist(record, observed):
                            return
                    elif step == "hermes_install":
                        installed = platform.get("hermes", {})
                        revision = installed.get("commit")
                        if installed.get("state") == "ready" and installed.get("source") in {"release", "latest"} and isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40}", revision):
                            runtime = (await self.coordinator.capabilities())["runtime"]
                            self.require_active()
                            if runtime.get("upstream_commit") != revision:
                                self.wait_for_observation(record, step, "hermes_revision", "hermes_runtime_starting",
                                    "已安裝的 Hermes 與執行版本仍不一致；請查看診斷，重試只會重新核對版本。", identity=revision)
                                return
                            record.get("observation_waits", {}).pop("hermes_revision", None)
                            self.checkpoint(record, step, "completed", "hermes_revision_verified")
                        else:
                            await self.host_operation(record, step, "bootstrap_hermes", observed)
                            return
                    elif step == "hermes_auth":
                        if not await self.hermes_auth(record):
                            return
                    elif step == "channel":
                        if not self.channel_ready(record):
                            self.checkpoint(record, step, "waiting_input", "channel_setup_required", self.human(
                                "channel_setup", "請完成通道登入與允許的對象；也可明確選擇先使用 Web。", channel=record["channel"]))
                            return
                        self.checkpoint(record, step, "skipped" if record["channel"] == "web" else "completed",
                                        "channel_explicitly_skipped" if record["channel"] == "web" else "channel_connection_verified")
                    elif step == "ha_provision":
                        # Recheck live gates immediately before any household platform action.
                        auth_epoch = self.store.setting("hermes_auth_epoch")
                        runtime = (await self.coordinator.capabilities())["runtime"]
                        self.require_active()
                        if (auth_epoch != self.store.setting("hermes_auth_epoch") or
                            not self.channel_ready(record) or runtime.get("state") != "ready" or
                            not runtime.get("auth_verified_at") or runtime.get("execution_state") == "stop_unconfirmed"):
                            self.block(record, step, "setup_readiness_changed", "Hermes 或通道連線已改變；恢復連線後再重試。")
                            return
                        if observed.get("restore_pending") or observed.get("kvm") is not True:
                            self.block(record, step, "ha_host_not_ready", "請先完成主機虛擬化或恢復診斷，再重試。")
                            return
                        if observed.get("provisioned") is not True:
                            await self.host_operation(record, step, "provision", observed)
                            return
                        if observed.get("vm") != "running":
                            await self.host_operation(record, step, "start", observed)
                            return
                        self.checkpoint(record, step, "completed", "ha_vm_running")
                    elif step == "ha_native":
                        native = platform.get("ha", {})
                        owner = self.store.setting("owner")
                        token = getattr(self.ha, "token", None)
                        native_complete = native.get("onboarding_complete") is True
                        # Official Core omits this route after a completed onboarding
                        # restart. A 404 alone is never proof: require active owner + config.
                        completed_route_absent = native.get("http_status") == 404
                        if (native_complete or completed_route_absent) and owner:
                            try:
                                valid_owner = await self.ha.owner(owner)
                                config = await self.ha.request("GET", "/config")
                            except HAError:
                                valid_owner, config = None, {}
                            self.require_active()
                            if (valid_owner and isinstance(config.get("version"), str) and
                                owner == self.store.setting("owner") and token == getattr(self.ha, "token", None)):
                                self.checkpoint(record, step, "completed", "ha_owner_and_route_absence_verified"
                                                if completed_route_absent and not native_complete else "ha_owner_and_onboarding_verified")
                                continue
                        if native.get("reachable") is not True and not completed_route_absent:
                            self.checkpoint(record, step, "running", "ha_starting")
                            return
                        code = "ha_owner_required" if native_complete or completed_route_absent else "ha_native_onboarding_required"
                        self.checkpoint(record, step, "waiting_input", code, self.human("ha_native_onboarding",
                            "請在 Home Assistant 原生頁面完成帳號與家庭設定，再連結 HA owner；系統會自動確認。",
                            url="http://localhost:8123/onboarding.html", code=code))
                        return
                record.update(state="completed", current_step=None, human_action=None)
                self.save(record)
            except SetupPaused:
                self.block(record, step, "ai_paused", "AI 已暫停；恢復後可重試設定。")
            except (HTTPException, RunnerError, HermesError, HAError, OSError, ValueError, KeyError, TypeError) as exc:
                if isinstance(exc, RunnerError) and exc.status_code == 409 and str(exc) == "stop_unconfirmed":
                    self.block(record, step, "runner_needs_reconcile", "Runner 尚未確認原程序已停止；請查看診斷後重新核對原工作。")
                    return
                record["transient_errors"] = record.get("transient_errors", 0) + 1
                if record["transient_errors"] <= 3:
                    self.checkpoint(record, step, "running", "service_reconnecting")
                else:
                    self.block(record, step, "service_unavailable", "設定服務暫時無法確認狀態；重試會沿用原操作識別碼。")


def routes(app, bootstrap):
    def key(request):
        value = request.headers.get("idempotency-key", "")
        if not value or len(value) > 200:
            raise HTTPException(400, "缺少有效的 Idempotency-Key")
        return value

    @app.get("/api/bootstrap", response_model=BootstrapStatus)
    async def status():
        return bootstrap.public()

    @app.post("/api/bootstrap/start", status_code=202, response_model=BootstrapStatus)
    async def start(body: BootstrapStart, request: Request):
        return await bootstrap.start(body, key(request))

    @app.post("/api/bootstrap/retry", status_code=202, response_model=BootstrapStatus)
    async def retry(body: BootstrapRetry, request: Request):
        return await bootstrap.retry(key(request))

    @app.post("/api/bootstrap/preferences", status_code=202, response_model=BootstrapStatus)
    async def preferences(body: BootstrapPreferences, request: Request):
        return await bootstrap.preferences(body, key(request))
