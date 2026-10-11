import asyncio
import json
import sys
from pathlib import Path

import pytest
from agentic_runner.providers import VERSIONS, ProviderError, Providers


@pytest.mark.parametrize("name", ["agy", "grok"])
async def test_new_cli_contract_normalizes_output_and_keeps_secrets_out_of_argv(tmp_path, monkeypatch, name):
    providers = Providers(tmp_path)
    profile = providers.profile(name)
    (profile / "credential.json").write_text(
        json.dumps({"value": "fixture-provider-secret", "method": "api_key"})
    )
    seen = []

    async def probe(provider):
        return {"version_matches": True}

    async def spawn(provider, args, cwd=None):
        seen.append(args)
        env = providers.environment(provider)
        assert "fixture-provider-secret" not in " ".join(args)
        assert env["GEMINI_API_KEY" if name == "agy" else "XAI_API_KEY"] == "fixture-provider-secret"
        assert "SUPERVISOR_TOKEN" not in env
        if name == "agy":
            config = json.loads((profile / ".gemini/antigravity-cli/settings.json").read_text())
            assert config["modelProvider"] == "gemini"
            assert set(config["permissions"]["deny"]) == {
                a + "(*)"
                for a in (
                    "read_file",
                    "write_file",
                    "read_url",
                    "execute_url",
                    "command",
                    "unsandboxed",
                    "mcp",
                )
            }
            code = "import sys,json; p=json.loads(sys.stdin.readline()); assert p['message']['content']=='fixture prompt'; print(json.dumps({'event':'result','result':{'status':'SUCCESS','structured_output':{'message':'hello'}}}))"
        else:
            assert args[args.index("--tools") + 1] == "" and "--no-leader" in args
            assert args[args.index("--permission-mode") + 1] == "dontAsk"
            code = "import sys,json; assert sys.stdin.read()=='fixture prompt'; print(json.dumps({'structuredOutput':{'message':'hello'}}))"
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

    monkeypatch.setattr(providers, "probe", probe)
    monkeypatch.setattr(providers, "spawn", spawn)
    assert await providers.run("fixture-job", name, "fixture prompt", {"type": "object"}) == {
        "message": "hello"
    }
    assert not (tmp_path / "work/fixture-job").exists()
    assert len(seen) == 1


async def test_antigravity_tool_events_and_failed_output_are_rejected(tmp_path, monkeypatch):
    providers = Providers(tmp_path)

    async def probe(provider):
        return {"version_matches": True}

    async def spawn(provider, args, cwd=None):
        code = "import sys,json;sys.stdin.read();print(json.dumps({'event':'step_update','step_update':{'step_type':'tool'}}));print(json.dumps({'event':'result','result':{'status':'SUCCESS','structured_output':{'message':'unsafe'}}}))"
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

    monkeypatch.setattr(providers, "probe", probe)
    monkeypatch.setattr(providers, "spawn", spawn)
    with pytest.raises(ProviderError, match="工具"):
        await providers.run("fixture-job", "agy", "fixture prompt", {"type": "object"})


async def test_grok_device_code_uses_official_cli_and_only_expected_auth_domain(tmp_path, monkeypatch):
    providers = Providers(tmp_path)

    async def spawn(provider, args, cwd=None):
        assert provider == "grok" and args == ["login", "--device-auth"]
        code = "import time,sys;print('https://accounts.x.ai/device?user_code=ABCD-EFGH\\nABCD-EFGH',file=sys.stderr,flush=True);time.sleep(0.3)"
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

    monkeypatch.setattr(providers, "spawn", spawn)
    result = await providers.begin_device_auth("fixtureauth", "grok")
    assert result["status"] == "pending"
    for _ in range(50):
        if providers.auth_sessions["fixtureauth"]["url"]:
            break
        await asyncio.sleep(0.01)
    state = providers.auth_sessions["fixtureauth"]
    assert state["url"].startswith("https://accounts.x.ai/") and state["code"] == "ABCD-EFGH"
    await state["_task"]
    assert state["status"] == "connected" and state["code"] is None
    assert (providers.profile("grok") / "verified").exists()


def test_all_four_provider_versions_are_locked():
    root = Path(__file__).parents[1]
    lock = json.loads((root / "providers.lock.json").read_text())
    assert {k: v["version"] for k, v in lock["providers"].items()} == VERSIONS
