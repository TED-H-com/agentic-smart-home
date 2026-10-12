"""Coding boundary/protocol evidence; model transport fixtures are not cloud acceptance."""
import hashlib
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "host"))
from agentic_host.coding_program import program  # noqa: E402
from agentic_host.coding_relay import Relay  # noqa: E402
from agentic_host.coding_sandbox import SandboxError  # noqa: E402
from agentic_host.coding_service import CodingService  # noqa: E402
from agentic_runner.app import Broker, create_api  # noqa: E402
from agentic_runner.coding import CodingJob  # noqa: E402


def body(**extra):
    return {"work_contract_major": 1, "id": uuid.uuid4().hex, "case_id": uuid.uuid4().hex,
            "worker_id": "host-coder", "mode": "development", "provider": "codex", "model": "",
            "repo_id": "agentic-home", "goal": "Repair add() and run its tests", "questions": [],
            "base_commit": None, "expires": time.time() + 300, "lease_epoch": 1, "context_days": 7, **extra}


def test_request_refuses_model_chosen_authority():
    from pydantic import ValidationError
    for field, value in (("command", "sh"), ("image", "evil"), ("mount", "/"), ("url", "http://localhost")):
        with pytest.raises(ValidationError):
            CodingJob.model_validate(body(**{field: value}))
    for provider in ("claude", "agy", "grok"):
        with pytest.raises(ValidationError):
            CodingJob.model_validate(body(provider=provider))


def test_prerequisite_refusal_is_durable_exact_receipt(tmp_path):
    manager = SimpleNamespace(root=tmp_path / "host", product=tmp_path / "product")
    manager.root.mkdir()
    service = CodingService(manager)
    request = body()
    status, job = service.handle("POST", "/coding/jobs", request)
    assert status == 202 and job["state"] == "failed"
    assert job["stop_confirmed"] is True and job["execution_started"] is False
    assert job["error_code"] == "coding_boundary_unavailable"
    restored = CodingService(manager)
    assert restored.handle("POST", "/coding/jobs", request) == (status, job)
    assert restored.handle("POST", "/coding/jobs", {**request, "goal": "changed"})[0] == 409
    assert restored.handle("GET", "/coding/jobs/" + request["id"])[1] == job
    service.close()
    restored.close()


def test_relay_refuses_network_authority_and_credentials_in_request(tmp_path):
    relay = Relay(tmp_path / "relay.sock", tmp_path / "absent.json", time.time() + 20)
    for method, url in (("CONNECT", "127.0.0.1:8123"), ("GET", "/fetch?url=http://127.0.0.1:8123"),
                        ("GET", "/fetch?url=https://supervisor/"), ("POST", "/v1/anything"),
                        ("GET", "/fetch?url=https://user:password@github.com/")):
        with pytest.raises(SandboxError):
            relay.request(method, url, b"")
    assert not relay.credential_path.exists()


def test_public_dns_pin_rejects_private_and_mixed_answers(monkeypatch):
    import socket

    from agentic_host.coding_relay import PublicTLS
    called = []
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kw: called.append(args))
    for values in (("127.0.0.1",), ("192.168.177.2",), ("1.1.1.1", "172.30.32.2")):
        monkeypatch.setattr(socket, "getaddrinfo", lambda *args, values=values, **kw:
                            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (value, 443)) for value in values])
        with pytest.raises(SandboxError):
            PublicTLS("api.openai.com").connect()
    assert not called


@pytest.mark.parametrize("attack", ["symlink", "hardlink", "nested_git", "fifo"])
def test_root_collection_rejects_untrusted_tree_before_git(tmp_path, monkeypatch, attack):
    import os

    from agentic_host import coding_service
    service = CodingService(SimpleNamespace(root=tmp_path / "host", product=tmp_path / "product"))
    source = tmp_path / "work" / ("a" * 32) / "workspace/source"
    source.mkdir(parents=True)
    (source / ".git").mkdir()
    outside = tmp_path / "private"
    outside.write_text("private-content")
    if attack == "symlink":
        (source / "trap").symlink_to(outside)
    elif attack == "hardlink":
        os.link(outside, source / "trap")
    elif attack == "fifo":
        os.mkfifo(source / "trap")
    else:
        (source / "nested").mkdir()
        (source / "nested/.git").write_text("gitdir: " + str(tmp_path))
    commands = []
    monkeypatch.setattr(coding_service, "command", lambda *args, **kw: commands.append(args))
    try:
        with pytest.raises(SandboxError):
            service.collect({"id": "a" * 32}, SimpleNamespace(root=tmp_path / "work"))
        assert not commands and outside.read_text() == "private-content"
    finally:
        service.close()


def test_relay_selected_model_and_call_deadline(tmp_path, monkeypatch):
    from agentic_host import coding_relay
    monkeypatch.setattr(coding_relay, "credentials", lambda path: ("api.openai.com", "/v1/responses", {"Authorization": "Bearer fixture"}))
    forwarded = []
    def transport(*args):
        forwarded.append(args)
        return 200, "application/json", b"{}"
    relay = Relay(tmp_path / "relay.sock", tmp_path / "auth.json", time.time() + 10,
                  transport=transport, model="gpt-6-astra")
    with pytest.raises(SandboxError):
        relay.request("POST", "/v1/responses", b'{"model":"gpt-6-sol"}')
    with pytest.raises(SandboxError):
        relay.request("POST", "/v1/responses", b'{"model":"gpt-6-astra","tools":[{"type":"mcp"}]}')
    assert not forwarded
    relay.calls = 80
    with pytest.raises(SandboxError):
        relay.request("POST", "/v1/responses", b'{"model":"gpt-6-astra"}')
    relay.calls, relay.deadline = 0, time.time() - 1
    with pytest.raises(SandboxError):
        relay.request("POST", "/v1/responses", b'{"model":"gpt-6-astra"}')


async def test_coding_endpoints_paired_and_role_separate(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_HOST_MODE", "1")
    broker = Broker(tmp_path)
    broker.settings({"token_hash": hashlib.sha256(b"fixture").hexdigest()})
    app = create_api(broker)
    assert broker.coding_credentials.profile("codex") != broker.providers.profile("codex")
    assert broker.coding_credentials.uid("codex") == 1010
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://runner") as client:
        assert (await client.get("/coding/capabilities")).status_code == 401
        monkeypatch.setenv("AGENTIC_HOST_MODE", "0")
        result = await client.get("/coding/capabilities", headers={"Authorization": "Bearer fixture"})
        assert result.json()["runtime_location"] == "haos_app"
        assert result.json()["available"] is False
        assert (await client.post("/coding/jobs", json=body(), headers={"Authorization": "Bearer fixture"})).status_code == 409
    broker.db.close()


async def test_coding_auth_lost_reply_recovers_receipt_without_reusing_operator_profile(tmp_path, monkeypatch):
    from agentic_runner import coding
    monkeypatch.setenv("AGENTIC_HOST_MODE", "1")
    calls = []
    async def host(method, path, body=None):
        calls.append(path)
        return 200, {"stop_confirmed": True}
    monkeypatch.setattr(coding, "host_request", host)
    broker = Broker(tmp_path)
    broker.settings({"token_hash": hashlib.sha256(b"fixture").hexdigest()})
    app = create_api(broker)
    enrolled = []
    async def login(provider, method, secret):
        enrolled.append((provider, method))
        return {"status": "connected"}
    monkeypatch.setattr(broker.coding_credentials, "secret_login", login)
    request = {"id": "c" * 32, "method": "api_key", "secret": "synthetic-role-credential"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://runner",
                                 headers={"Authorization": "Bearer fixture"}) as client:
        first = await client.post("/coding/providers/codex/auth", json=request)
        assert first.status_code == 200 and first.json()["status"] == "connected"
        assert (await client.post("/coding/providers/codex/auth", json=request)).json() == first.json()
        assert (await client.get("/coding/auth/" + request["id"])).json() == first.json()
        assert len(enrolled) == 1 and calls == ["/fence", "/auth-release"]
        assert (await client.post("/coding/providers/codex/auth", json={**request, "secret": "different"})).status_code == 409
        receipts = tmp_path / "coding-auth/private/auth-receipts.json"
        assert request["secret"] not in receipts.read_text()
    broker.db.close()
    restarted = Broker(tmp_path)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_api(restarted)), base_url="https://runner",
                                     headers={"Authorization": "Bearer fixture"}) as client:
            assert (await client.get("/coding/auth/" + request["id"])).json()["status"] == "connected"
    finally:
        restarted.db.close()


def test_launcher_keeps_tools_inside_container_and_no_bearer():
    script = program(body())
    compile(script, "coding launcher", "exec")
    assert "--dangerously-bypass-approvals-and-sandbox" in script
    assert "/relay.sock" in script and "SUPERVISOR_TOKEN" not in script
    assert "shell_tool" not in script  # Legacy runner alone disables tools.
    assert "--ephemeral" in script


def test_actual_codex_tool_repair_and_test(request):
    if not request.config.getoption("--containers"):
        pytest.skip("Actual packaged Codex requires --containers")
    name = "agentic-coding-tools-" + uuid.uuid4().hex[:12]
    source = Path(__file__).parents[1]
    script = r'''
import json,os,socketserver,subprocess,threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler
from agentic_host.coding_program import program
root=Path('/work')
root.mkdir()
for name in ('source','scratch','profile','artifacts'):
    p=root/name;p.mkdir();os.chown(p,10000,10000);p.chmod(0o700)
(root/'source/calculator.py').write_text('def add(a,b): return a-b\n')
(root/'source/test_calculator.py').write_text('import unittest\nfrom calculator import add\nclass Test(unittest.TestCase):\n def test_add(self): self.assertEqual(add(4,2),6)\n')
for p in (root/'source').iterdir():os.chown(p,10000,10000)
calls=[]
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
    value=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
    calls.append(value)
    tools=[t.get('name') for x in value.get('input',[]) if x.get('type')=='additional_tools'
           for namespace in x.get('tools',[]) for t in namespace.get('tools',[])]
    assert 'spawn_agent' not in tools and 'delegate_task' not in tools,tools
    if len(calls)==1:
      assert 'exec_command' in tools,tools
      action="python -c \"from pathlib import Path; p=Path('calculator.py'); p.write_text(p.read_text().replace('a-b','a+b'))\" && python -m unittest -v"
      item={'type':'function_call','id':'fc_fixture','call_id':'call_fixture','name':'exec_command','namespace':'functions','arguments':json.dumps({'cmd':action,'max_output_tokens':2000}),'status':'completed'}
    else:
      assert any(x.get('type')=='function_call_output' and 'OK' in str(x) for x in value['input']),[(x.get('type'),str(x.get('output',''))[:500]) for x in value['input']]
      item={'id':'msg_fixture','type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':'Facts: fixed add; unittest passed. No deployment.','annotations':[]}]}
    events=[{'type':'response.created','response':{'id':'resp_fixture'}},
      {'type':'response.output_item.added','output_index':0,'item':item},
      {'type':'response.output_item.done','output_index':0,'item':item},
      {'type':'response.completed','response':{'id':'resp_fixture','status':'completed','output':[item],'usage':{'input_tokens':1,'output_tokens':1,'total_tokens':2}}}]
    data=''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in events).encode()
    self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
class Server(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):daemon_threads=True
server=Server('/relay.sock',Handler);os.chown('/relay.sock',10000,10000);os.chmod('/relay.sock',0o600)
threading.Thread(target=server.serve_forever,daemon=True).start()
request={'mode':'development','model':'','goal':'Repair add() and run its tests','questions':[]}
(root/'task.py').write_text(program(request))
result=subprocess.run(['python','-I','/work/task.py'],cwd='/work/source',user=10000,group=10000,extra_groups=[],env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':'/work/profile','TMPDIR':'/work/scratch'},capture_output=True,text=True,timeout=90)
assert result.returncode==0,(result.returncode,(root/'scratch/events.jsonl').read_text()[-6000:],result.stderr)
assert len(calls)==2,len(calls)
assert 'a+b' in (root/'source/calculator.py').read_text()
verified=subprocess.run(['python','-m','unittest','-v'],cwd='/work/source',capture_output=True,text=True)
assert verified.returncode==0,verified.stderr
assert not (root/'profile/auth.json').exists()
print(json.dumps({'actual_cli':'0.157.1','tool_calls':1,'model_turns':len(calls),'independent_unittest':'passed','cloud_account':'not_used','network':'none'}))
server.shutdown();server.server_close()
'''
    try:
        result = subprocess.run(["docker", "run", "--rm", "--name", name, "--network", "none", "--memory", "768m",
            "--memory-swap", "768m", "--pids-limit", "128", "--cpus", "1", "--mount", f"type=bind,src={source / 'host'},dst=/host,readonly",
            "--env", "PYTHONPATH=/host", "--entrypoint", "python", "agentic-home-runner:0.6.0", "-c", script],
            capture_output=True, text=True, timeout=110)
        assert result.returncode == 0, result.stdout + result.stderr
        evidence = json.loads(result.stdout)
        assert evidence["tool_calls"] == 1 and evidence["independent_unittest"] == "passed"
    finally:
        subprocess.run(["docker", "rm", "-fv", name], capture_output=True, check=False)


def test_relay_auth_failure_requires_new_role_login(tmp_path, monkeypatch):
    from agentic_host import coding_relay
    monkeypatch.setattr(coding_relay, "credentials", lambda path: ("api.openai.com", "/v1/responses", {}))
    calls = []
    def rejected(*args):
        calls.append(args)
        return 401, "application/json", b'{}'
    relay = Relay(tmp_path / "relay", tmp_path / "auth", time.time() + 10, transport=rejected)
    assert relay.request("POST", "/v1/responses", b'{"model":"gpt-6-astra"}')[0] == 401
    with pytest.raises(SandboxError):
        relay.request("POST", "/v1/responses", b'{"model":"gpt-6-astra"}')
    assert len(calls) == 1


async def test_role_logout_reply_is_durable_and_cannot_replay_into_new_auth(tmp_path, monkeypatch):
    from agentic_runner import coding
    monkeypatch.setenv("AGENTIC_HOST_MODE", "1")
    calls = []
    async def host(method, path, body=None):
        calls.append(path)
        return 200, {"stop_confirmed": True}
    monkeypatch.setattr(coding, "host_request", host)
    broker = Broker(tmp_path)
    broker.settings({"token_hash": hashlib.sha256(b"fixture").hexdigest()})
    app = create_api(broker)
    logged_out = []
    async def logout(provider):
        logged_out.append(provider)
    monkeypatch.setattr(broker.coding_credentials, "logout", logout)
    request = {"id": "d" * 32}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://runner",
                                     headers={"Authorization": "Bearer fixture"}) as client:
            first = await client.post("/coding/providers/codex/logout", json=request)
            assert first.status_code == 200 and first.json()["status"] == "disconnected"
            assert (await client.post("/coding/providers/codex/logout", json=request)).json() == first.json()
            assert (await client.get("/coding/auth/" + request["id"])).json() == first.json()
            assert calls == ["/fence", "/auth-release"] and logged_out == ["codex"]
            assert (await client.post("/coding/providers/codex/auth", json={**request, "method": "api_key", "secret": "new"})).status_code == 409
    finally:
        broker.db.close()


def test_actual_independent_verifier_rejects_candidate_pytest_shadow(request):
    if not request.config.getoption("--containers"):
        pytest.skip("Actual pinned verifier requires --containers")
    name = "agentic-coding-verifier-" + uuid.uuid4().hex[:12]
    source = Path(__file__).parents[1]
    script = r'''
import json,os,subprocess
from pathlib import Path
from agentic_host.coding_program import VERIFY_PROGRAM
root=Path('/work');root.mkdir()
for name in ('source','scratch','artifacts','trusted-tests'):
 p=root/name;p.mkdir();p.chmod(0o755)
for name in ('source','scratch','artifacts'):os.chown(root/name,10000,10000)
source=root/'source'
(source/'README').write_text('before\n')
subprocess.run(['git','init','-q',str(source)],check=True)
subprocess.run(['git','-c','safe.directory=*','-C',str(source),'add','.'],check=True)
subprocess.run(['git','-c','safe.directory=*','-C',str(source),'-c','user.name=fixture','-c','user.email=fixture@example.invalid','commit','-qm','baseline'],check=True)
(source/'pytest.py').write_text('print("FAKE PASS");raise SystemExit(0)\n')
for directory,dirs,files in os.walk(source):
 os.chown(directory,10000,10000)
 for name in files:os.chown(Path(directory)/name,10000,10000)
(source/'controller').mkdir()
(source/'controller/pytest_asyncio.py').write_text('raise RuntimeError("candidate plugin shadow")\n')
(root/'candidate.patch').write_text('diff --git a/README b/README\n--- a/README\n+++ b/README\n@@ -1 +1 @@\n-before\n+after\n')
(root/'trusted-tests/test_failure.py').write_text('def test_real_failure(): assert False, "trusted-test-failure"\n')
(root/'verification.ini').write_text('[pytest]\nasyncio_mode=auto\n')
(root/'task.py').write_text(VERIFY_PROGRAM)
result=subprocess.run(['python','-I','/work/task.py'],cwd=source,user=10000,group=10000,extra_groups=[],env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':'/work/scratch','TMPDIR':'/work/scratch'},capture_output=True,text=True,timeout=30)
assert result.returncode==1,(result.returncode,result.stderr)
log=(root/'artifacts/verification.log').read_text()
assert 'trusted-test-failure' in log and 'FAKE PASS' not in log,log
assert json.loads((root/'artifacts/verification.json').read_text())['state']=='failed'
print('trusted baseline rejected fake candidate pytest')
'''
    try:
        result = subprocess.run(["docker", "run", "--rm", "--name", name, "--network", "none", "--memory", "768m",
            "--memory-swap", "768m", "--pids-limit", "128", "--cpus", "1", "--mount", f"type=bind,src={source / 'host'},dst=/host,readonly",
            "--env", "PYTHONPATH=/host", "--entrypoint", "python", "agentic-home-runner:0.6.0", "-c", script],
            capture_output=True, text=True, timeout=45)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        subprocess.run(["docker", "rm", "-fv", name], capture_output=True, check=False)
