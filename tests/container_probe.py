"""Run inside the Runner image. Real CLI, local protocol fixture, no account/network inference."""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import aiohttp
import uvicorn
from agentic_runner.app import Broker, create_api
from aiohttp import web


async def main():
    broker = Broker(Path("/data"))
    providers = ("codex", "claude", "agy", "grok")
    probes = [await broker.providers.health(name) for name in providers]
    assert all(p["version_matches"] and p["status"] == "needs_reauth" for p in probes), probes
    for provider in providers:
        other = "claude" if provider == "codex" else "codex"
        checks = ("import os; "
                  f"assert os.getuid() == {broker.providers.uid(provider)}; "
                  "assert 'SUPERVISOR_TOKEN' not in os.environ; "
                  f"assert not os.access('/data/profiles/{other}', os.R_OK); "
                  f"assert all(not os.access('/data/profiles/'+p, os.R_OK) for p in {providers!r} if p != {provider!r}); "
                  "assert not os.access('/data/private/server.key', os.R_OK); "
                  "assert not os.access('/data/runner.sqlite3', os.R_OK); "
                  "assert not os.access('/data/runner.sqlite3-wal', os.R_OK); "
                  "assert not os.access('/data/runner.sqlite3-shm', os.R_OK)")
        proc = await asyncio.create_subprocess_exec(sys.executable, "-c", checks,
            user=broker.providers.uid(provider), group=broker.providers.uid(provider), extra_groups=[],
            env=broker.providers.environment(provider))
        assert await proc.wait() == 0
    tls_server = uvicorn.Server(uvicorn.Config(create_api(broker), host="127.0.0.1", port=8443,
                               ssl_certfile=str(broker.cert), ssl_keyfile=str(broker.key), log_level="error"))
    tls_task = asyncio.create_task(tls_server.serve())
    while not tls_server.started:
        await asyncio.sleep(0.05)
    try:
        async with aiohttp.ClientSession() as client:
            pin = aiohttp.Fingerprint(bytes.fromhex(broker.fingerprint))
            async with client.get("https://127.0.0.1:8443/status", ssl=pin) as response:
                assert response.status == 401
            async with client.post("https://127.0.0.1:8443/pair", ssl=pin, json={"code": broker.code,
                "controller_id": "f" * 32, "principal": "test-owner", "protocol": 1}) as response:
                assert response.status == 200
                token = (await response.json())["token"]
            async with client.get("https://127.0.0.1:8443/status", ssl=pin,
                                  headers={"Authorization": "Bearer " + token}) as response:
                assert response.status == 200 and (await response.json())["protocol"] == 1
            try:
                await client.get("https://127.0.0.1:8443/status", ssl=aiohttp.Fingerprint(b"\x00" * 32))
            except aiohttp.ServerFingerprintMismatch:
                pass
            else:
                raise AssertionError("Wrong TLS certificate was accepted")
    finally:
        tls_server.should_exit = True
        await tls_task
    captured = {"codex": [], "claude": []}
    answer = {"message": "local protocol fixture"}
    schema_value = {"type": "object", "properties": {"message": {"type": "string"}},
                    "required": ["message"], "additionalProperties": False}

    def sse(events):
        return web.Response(text="".join("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events),
                            content_type="text/event-stream")

    async def receive(request):
        captured["codex"].append(await request.json())
        item = {"id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": json.dumps(answer), "annotations": []}]}
        return sse([{"type": "response.created", "response": {"id": "resp_fixture"}},
                    {"type": "response.output_item.added", "output_index": 0, "item": {**item, "content": []}},
                    {"type": "response.output_text.delta", "item_id": item["id"], "output_index": 0,
                     "content_index": 0, "delta": json.dumps(answer)},
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": {"id": "resp_fixture", "status": "completed",
                     "output": [item], "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}}])

    async def anthropic(request):
        data = await request.json()
        captured["claude"].append(data)
        first = len(captured["claude"]) == 1
        content = {"type": "tool_use", "id": "tool_fixture", "name": "StructuredOutput", "input": {}} if first else {
            "type": "text", "text": ""}
        return sse([{"type": "message_start", "message": {"id": "msg_fixture", "type": "message", "role": "assistant",
                     "model": "claude-sonnet-4-6", "content": [], "stop_reason": None,
                     "usage": {"input_tokens": 1, "output_tokens": 1}}},
                    {"type": "content_block_start", "index": 0, "content_block": content},
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps(answer)}
                     if first else {"type": "text_delta", "text": "Done"}},
                    {"type": "content_block_stop", "index": 0},
                    {"type": "message_delta", "delta": {"stop_reason": "tool_use" if first else "end_turn", "stop_sequence": None},
                     "usage": {"output_tokens": 1}}, {"type": "message_stop"}])

    server = web.Application()
    server.router.add_post("/v1/responses", receive)
    server.router.add_post("/v1/messages", anthropic)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8765)
    await site.start()
    work = Path("/data/work/probe")
    work.mkdir(mode=0o700)
    os.chown(work, 1001, 1001)
    schema = work / "schema.json"
    schema.write_text(json.dumps(schema_value))
    schema.chmod(0o644)
    args = broker.providers.command("codex", schema, work / "answer.json", "gpt-5.4")[:-1]
    args += ["--disable", "enable_request_compression", "-c", 'model_provider="capture"',
             "-c", 'model_providers.capture.name="Local test fixture"',
             "-c", 'model_providers.capture.base_url="http://127.0.0.1:8765/v1"',
             "-c", 'model_providers.capture.wire_api="responses"',
             "-c", 'model_providers.capture.requires_openai_auth=false', "-"]
    try:
        for model in ("gpt-5.4", "gpt-6-astra"):
            args[args.index("--model") + 1] = model
            process = await broker.providers.spawn("codex", args, work)
            stdout, stderr = await broker.providers.capture(process, b"Return a JSON greeting.", timeout=60)
            assert process.returncode == 0, {"stdout": stdout[-3000:], "stderr": stderr[-3000:]}
            assert json.loads((work / "answer.json").read_text()) == answer
        assert captured["codex"]
        for data in captured["codex"]:
            assert data.get("tools", []) == [], data.get("tools")
        original_env = broker.providers.environment

        def environment(provider):
            return original_env(provider) | {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8765",
                                             "ANTHROPIC_API_KEY": "local-fixture-only"}

        broker.providers.environment = environment
        result = await broker.providers.run("c" * 32, "claude", "Return a JSON greeting.", schema_value, "claude-sonnet-4-6")
        assert result == answer, result
        assert captured["claude"]
        claude_tools = {t["name"] for r in captured["claude"] for t in r.get("tools", [])}
        assert not claude_tools - {"StructuredOutput"}, claude_tools
        print(json.dumps({"probes": probes, "codex_models": ["gpt-5.4", "gpt-6-astra"], "codex_tools": [],
                          "claude_tools": sorted(claude_tools), "structured_output": "passed",
                          "tls_pin_and_pairing": "passed", "uid_and_secret_isolation": "passed", "run_at": time.time(),
                          "inference": "local protocol fixture; no cloud account"}, ensure_ascii=False))
    finally:
        await runner.cleanup()
        broker.db.close()


if __name__ == "__main__":
    asyncio.run(main())
