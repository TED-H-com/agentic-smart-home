import asyncio
import re

import aiohttp


class RunnerError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class Runner:
    def __init__(self, store, scope="host"):
        self.store = store
        self.scope = scope
        self.key = "runner_haos" if scope == "haos" else "runner"

    async def call(self, method, path, data=None, pairing=None):
        peer = pairing or self.store.setting(self.key)
        if not peer:
            raise RunnerError("請先配對 Runner")
        managed_guest = self.scope == "haos" and peer["host"] == "192.168.177.2"
        if not managed_guest and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", peer["host"]):
            raise RunnerError("Runner host 必須是 HA 內部 App 名稱")
        if peer.get("port", 8443) != 8443:
            raise RunnerError("Runner 只接受固定內部 port 8443")
        try:
            pin = aiohttp.Fingerprint(bytes.fromhex(peer["fingerprint"]))
            headers = {"Authorization": "Bearer " + peer["token"]} if peer.get("token") else {}
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45), trust_env=False) as session:
                async with session.request(method, f"https://{peer['host']}:8443{path}", json=data,
                                           ssl=pin, headers=headers, allow_redirects=False) as response:
                    if response.content_length and response.content_length > 262144:
                        raise RunnerError("Runner 回應超過上限")
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(16384):
                        raw.extend(chunk)
                        if len(raw) > 262144:
                            raise RunnerError("Runner 回應超過上限")
                    import json
                    result = json.loads(raw)
                    if response.status >= 300:
                        raise RunnerError(str(result.get("detail", "Runner 拒絕操作"))[:300], response.status)
                    return result
        except (aiohttp.ClientError, ValueError, TimeoutError) as exc:
            raise RunnerError("Runner 無法連線或憑證指紋不符，請檢查配對與 App 狀態") from exc

    async def pair(self, pairing, principal, controller_id):
        response = await self.call("POST", "/pair", {
            "code": pairing["code"], "principal": principal, "controller_id": controller_id, "protocol": 1}, pairing)
        if response.get("protocol") != 1 or response.get("identity") != "agentic-home-runner":
            raise RunnerError("Runner 通訊版本不相容")
        self.store.set_setting(self.key, {k: pairing[k] for k in ("host", "fingerprint", "port")} |
                               {"token": response["token"]})
        return {"paired": True}

    async def run(self, request):
        await self.call("POST", "/jobs", request)
        async with asyncio.timeout(320):
            while True:
                job = await self.call("GET", "/jobs/" + request["id"])
                if job["state"] == "succeeded":
                    return job["result"]
                if job["state"] in {"failed", "cancelled"}:
                    raise RunnerError(job.get("error", "任務已取消"))
                await asyncio.sleep(1)
