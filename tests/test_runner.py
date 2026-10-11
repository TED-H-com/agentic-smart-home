import hashlib
import time

import httpx
from agentic_runner.app import Broker, create_api
from agentic_runner.providers import DISABLED_CODEX_FEATURES, Providers


def test_child_environment_is_an_allowlist(tmp_path, monkeypatch):
    for key in ("SUPERVISOR_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY", "MCP_CONFIG"):
        monkeypatch.setenv(key, "never-forward-this")
    providers = Providers(tmp_path)
    for name in ("codex", "claude"):
        env = providers.environment(name)
        assert "never-forward-this" not in env.values()
        assert not any(key in env for key in ("SUPERVISOR_TOKEN", "AWS_SECRET_ACCESS_KEY", "MCP_CONFIG"))
        assert providers.profile(name).stat().st_mode & 0o777 == 0o700


def test_provider_tools_are_disabled_and_no_bypass_flag(tmp_path):
    providers = Providers(tmp_path)
    schema = tmp_path / "schema.json"
    schema.write_text("{}")
    for name in ("codex", "claude"):
        args = providers.command(name, schema, tmp_path / "out", "")
        assert not any("bypass" in arg or "skip-permissions" in arg or "yolo" in arg for arg in args)
        if name == "codex":
            assert "shell_tool" in DISABLED_CODEX_FEATURES and "--ignore-user-config" in args
            assert args[args.index("--sandbox") + 1] == "read-only"
        else:
            assert args[args.index("--tools") + 1] == ""
            assert "--strict-mcp-config" in args


async def test_pairing_is_one_time_and_bearer_protected(tmp_path):
    broker = Broker(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_api(broker)), base_url="https://runner") as client:
        assert (await client.get("/status")).status_code == 401
        body = {"code": broker.code, "controller_id": "a" * 32, "principal": "owner", "protocol": 1}
        result = await client.post("/pair", json=body)
        assert result.status_code == 200
        token = result.json()["token"]
        assert broker.setting("token_hash") == hashlib.sha256(token.encode()).hexdigest()
        assert token not in (tmp_path / "runner.sqlite3").read_bytes().decode(errors="ignore")
        assert (await client.post("/pair", json=body)).status_code == 409
        assert (await client.get("/jobs/nonexistent", headers={"Authorization": "Bearer " + token})).status_code == 404
    broker.db.close()


async def test_job_is_idempotent_and_single_worker(tmp_path, monkeypatch):
    import asyncio
    broker = Broker(tmp_path)
    broker.settings({"token_hash": hashlib.sha256(b"test-token").hexdigest()})
    started = []
    release = asyncio.Event()

    async def run(id, provider, prompt, schema, model):
        started.append(id)
        await release.wait()
        return {"message": "fixture"}

    monkeypatch.setattr(broker.providers, "run", run)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_api(broker)), base_url="https://runner",
                                headers={"Authorization": "Bearer test-token"}) as client:
        body = {"id": "a" * 32, "provider": "codex", "prompt": "fixture", "schema": {}, "expires": time.time() + 300}
        assert (await client.post("/jobs", json=body)).status_code == 200
        assert (await client.post("/jobs", json=body)).status_code == 200
        assert (await client.post("/jobs", json={**body, "prompt": "different"})).status_code == 409
        assert (await client.post("/jobs", json={**body, "id": "b" * 32})).status_code == 200
        await asyncio.sleep(0)
        assert started == ["a" * 32]
        await client.post("/jobs/" + "b" * 32 + "/cancel")
        release.set()
        await asyncio.gather(*broker.tasks.values(), return_exceptions=True)
        assert started == ["a" * 32]
    broker.db.close()
