"""Composed browser E2E, not a real cloud/HAOS/hardware acceptance claim.

Real: Chromium, compiled Web, Controller, TLS Runner, installed Codex binary,
Hermes bridge/AIAgent/official MCP, isolated HA Core and its browser OAuth.
Fixtures: model responses and root Host network/install/VM lifecycle effects.
No browser API interception. Only disposable data and synthetic credentials.
A passive unhandledrejection listener records only a reason's shape and code.
"""
import asyncio
import contextlib
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import time
import traceback
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
import pytest
from aiohttp import web
from playwright.async_api import async_playwright

ROOT = Path(__file__).parents[1]
BASE = "http://localhost:8099"
CORE = "http://localhost:8123"
CORE_IMAGE = "ghcr.io/home-assistant/home-assistant:2026.10.1"
IMAGE_TAG = os.environ.get("AGENTIC_E2E_IMAGE_TAG", "0.11.1")
REVISION = json.loads((ROOT / "hermes/hermes.lock.json").read_text())["commit"]
# Shape only: type, constructor, own keys and a numeric or snake_case code. Never values.
REJECTION_SHAPE = r"""addEventListener("unhandledrejection", event => {
  const reason = event.reason, object = reason !== null && typeof reason === "object";
  const nested = object && reason.error !== null && typeof reason.error === "object" ? reason.error.code : undefined;
  const code = object ? (nested === undefined ? reason.code : nested) : typeof reason === "number" ? reason : undefined;
  const safe = typeof code === "number" || (typeof code === "string" && /^[a-z_]{1,40}$/.test(code));
  window.__agenticE2eRejection?.({kind: reason === null ? "null" : typeof reason,
    ctor: object ? String(reason.constructor?.name || "").slice(0, 40) : "",
    keys: object ? Object.keys(reason).slice(0, 8).map(key => String(key).slice(0, 24)) : [],
    code: safe ? code : null});
});"""
# Playwright call-log phrases. Only these labels are kept, never the log text.
WAIT_STATES = (("not_visible", "element is not visible"), ("not_stable", "element is not stable"),
               ("not_enabled", "element is not enabled"), ("intercepted", "intercepts pointer events"),
               ("outside_viewport", "outside of the viewport"), ("detached", "element is not attached"))
STACK_URL = re.compile(r"(https?://[^\s()]+?):\d+:\d+\)?$")
# Failure-only reads inside the containers. They print states, codes, counts and
# ages, never prompts, answers, credentials or URLs.
BOOTSTRAP_SHAPE = r"""
import datetime, json, sqlite3, time
c = sqlite3.connect('/data/controller.sqlite3')
r = c.execute("select body from records where kind='bootstrap_v1' and id='setup'").fetchone()
d = json.loads(r[0]) if r else {}
out = {k: d.get(k) for k in ('state', 'current_step', 'human_action')}
step = next((s for s in d.get('steps', []) if s.get('id') == d.get('current_step')), {})
out.update(step_state=step.get('state'), step_code=step.get('code'), transient_errors=d.get('transient_errors', 0),
           verification=bool(d.get('verification')), waits=sorted(d.get('observation_waits', {})))
try:
    out['updated_age_s'] = round(time.time() - datetime.datetime.fromisoformat(d['updated_at']).timestamp(), 1)
except (KeyError, TypeError, ValueError):
    out['updated_age_s'] = None
runs = [json.loads(b) for (b,) in c.execute("select body from records where kind='hermes_run' order by created")]
out['hermes_runs'] = [[x.get('state'), x.get('error_code')] for x in runs[-5:]]
# Tool calls the Controller accepted per grant, and evidence rows written: counts only.
grants = [json.loads(b) for (b,) in c.execute("select body from records where kind='mcp_grant' order by created")]
out['mcp_grant_calls'] = [[g.get('calls', 0), bool(g.get('revoked'))] for g in grants[-5:]]
out['case_evidence'] = c.execute("select count(*) from records where kind='case_evidence'").fetchone()[0]
print(json.dumps(out))
"""
# The Hermes image has no ps. Process names, ages, threads, CPU and kernel wait
# channels plus TCP port states only; never arguments, environment or addresses.
HERMES_PROCESSES = r"""
import json, os
tick = os.sysconf('SC_CLK_TCK')
uptime = float(open('/proc/uptime').read().split()[0])
procs, owners = [], {}
for pid in sorted(int(p) for p in os.listdir('/proc') if p.isdigit()):
    try:
        stat = open(f'/proc/{pid}/stat').read()
        wchan = open(f'/proc/{pid}/wchan').read()[:32]
        fds = os.listdir(f'/proc/{pid}/fd')
    except OSError:
        continue
    for fd in fds:
        try:
            link = os.readlink(f'/proc/{pid}/fd/{fd}')
        except OSError:
            continue
        if link.startswith('socket:['):
            owners[link[8:-1]] = pid
    f = stat[stat.rindex(')') + 2:].split()
    procs.append([pid, int(f[1]), round(uptime - int(f[19]) / tick), int(f[17]), f[0],
                  stat[stat.index('(') + 1:stat.rindex(')')][:16], round((int(f[11]) + int(f[12])) / tick, 1), wchan])
# Only these processes' own TCP sockets: pid, local and remote port, state, queued bytes.
tcp = []
for table in ('/proc/net/tcp', '/proc/net/tcp6'):
    try:
        lines = open(table).read().splitlines()[1:]
    except OSError:
        continue
    for line in lines:
        f = line.split()
        if f[9] in owners:
            tx, rx = (int(x, 16) for x in f[4].split(':'))
            tcp.append([owners[f[9]], int(f[1].rsplit(':', 1)[1], 16), int(f[2].rsplit(':', 1)[1], 16), f[3], rx, tx])
print(json.dumps({'processes': procs[:20], 'tcp': tcp[:40]}))
"""
HERMES_AUTH_SHAPE = r"""
import json, pathlib
p = pathlib.Path('/data/provider.json')
a = json.loads(p.read_text()) if p.exists() else {}
print(json.dumps({'state': a.get('state'), 'verified': bool(a.get('verified_at')), 'provider': a.get('provider')}))
"""


def stack_file(stack):
    """File name of the first stack frame, without query or path. It identifies a bundle chunk."""
    for line in (stack or "").splitlines():
        match = STACK_URL.search(line.strip())
        if match:
            return urlsplit(match.group(1)).path.rsplit("/", 1)[-1][:80]
    return None


def rejection_shape(origin, value):
    value = value if isinstance(value, dict) else {}
    keys = value.get("keys") if isinstance(value.get("keys"), list) else []
    code = value.get("code")
    return {"origin": origin, "kind": str(value.get("kind", ""))[:12], "ctor": str(value.get("ctor", ""))[:40],
            "keys": [str(key)[:24] for key in keys[:8]],
            "code": code if type(code) is int or (isinstance(code, str) and re.fullmatch(r"[a-z_]{1,40}", code)) else None}


async def docker(*args):
    def run():
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, "Docker command failed: " + str(args[:2]) + " " + result.stderr[-800:]
        return result.stdout.strip()
    return await asyncio.to_thread(run)


async def eventually(call, check, timeout=90):
    until = time.monotonic() + timeout
    last = None
    while time.monotonic() < until:
        try:
            last = await call()
            if check(last):
                return last
        except (aiohttp.ClientError, TimeoutError, ValueError):
            pass
        await asyncio.sleep(0.5)
    raise AssertionError("E2E checkpoint timed out: " + str(last)[:800])


class BoundaryFixtures:
    def __init__(self):
        self.calls, self.cli_requests, self.hermes_requests = [], [], []
        self.jobs = {}
        self.installed, self.provisioned = False, False

    async def host_status(self, request):
        native = {"reachable": False, "onboarding_complete": False, "steps": []}
        if self.provisioned:
            async with aiohttp.ClientSession() as session:
                async with session.get(CORE + "/api/onboarding") as response:
                    native["http_status"] = response.status
                    if response.status == 200:
                        steps = await response.json()
                        native.update(reachable=True, steps=steps,
                                      onboarding_complete=bool(steps) and all(x["done"] for x in steps))
        return web.json_response({"kvm": True, "provisioned": self.provisioned,
            "vm": "running" if self.provisioned else "not_created", "restore_pending": None,
            "jobs": list(self.jobs.values()), "snapshots": [],
            "bootstrap": {"network": {"ready": True, "internet_verified": False},
                "hermes": {"state": "ready" if self.installed else "bundled",
                           "source": "release" if self.installed else "bundled", "commit": REVISION},
                "ha": native}})

    async def host_action(self, request):
        body = await request.json()
        assert set(body) == {"id", "action"}
        assert body["action"] in {"bootstrap_hermes", "provision", "start"}
        if body["id"] in self.jobs:
            assert self.jobs[body["id"]]["request"] == body
        else:
            self.calls.append(body["action"])
            self.jobs[body["id"]] = {"id": body["id"], "request": body, "state": "succeeded"}
            if body["action"] == "bootstrap_hermes":
                self.installed = True
            else:
                self.provisioned = True
        return web.json_response(self.jobs[body["id"]])

    async def cli_model(self, request):
        body = await request.json()
        self.cli_requests.append(body)
        assert not body.get("tools")
        answer = json.dumps({"decision": "continue", "next_step": "install_hermes", "reason_codes": ["runtime_missing"]})
        item = {"id": "msg_e2e", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": answer, "annotations": []}]}
        response = {"id": "resp_e2e", "status": "completed", "output": [item],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
        events = [{"type": "response.created", "response": {"id": "resp_e2e"}},
            {"type": "response.output_item.added", "output_index": 0, "item": {**item, "content": []}},
            {"type": "response.output_text.delta", "item_id": item["id"], "output_index": 0, "content_index": 0, "delta": answer},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": response}]
        return web.Response(text="".join("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events), content_type="text/event-stream")

    async def hermes_model(self, request):
        body = await request.json()
        self.hermes_requests.append(body)
        messages = body["messages"]
        verify = "模型連線驗證" in json.dumps(messages, ensure_ascii=False)
        tools = any(m["role"] == "tool" for m in messages)
        if verify or tools:
            message = {"role": "assistant", "content": "Hermes 已可回應" if verify else "E2E 主機證據已取得；此為隔離測試。"}
            finish = "stop"
        else:
            assert any(t["function"]["name"] == "mcp__agentic__host_status" for t in body["tools"])
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_e2e", "type": "function",
                "function": {"name": "mcp__agentic__host_status", "arguments": "{}"}}]}
            finish = "tool_calls"
        reply = {"id": "e2e-completion", "object": "chat.completion", "created": 1, "model": "fixture/model",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
        if not body.get("stream"):
            return web.json_response(reply)
        delta = dict(message)
        if "tool_calls" in delta:
            delta["tool_calls"] = [{"index": 0, **delta["tool_calls"][0]}]
        chunk = {**reply, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
        final = {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}
        return web.Response(text="data: " + json.dumps(chunk) + "\n\ndata: " + json.dumps(final) + "\n\ndata: [DONE]\n\n", content_type="text/event-stream")


async def test_composed_onboarding_browser_services_and_native_home(tmp_path, request):
    if not (request.config.getoption("--containers") and request.config.getoption("--real-ha")):
        pytest.skip("Composed E2E requires --containers --real-ha")
    assert str(tmp_path).startswith(str(Path.home() / "agent-work/tmp/pytest"))
    # Refuse to replace or stop an existing service. All listeners stay loopback.
    for port in (8096, 8097, 8098, 8099, 8123, 8443):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
    dirs = {name: tmp_path / name for name in ("controller", "runner", "hermes", "pairing", "hermes-link", "host-socket", "core")}
    # Linux AF_UNIX paths are bounded to 108 bytes; the pytest case name is long.
    dirs["host-socket"] = tmp_path.parent / "s"
    for path in dirs.values():
        path.mkdir(mode=0o700)
    key = secrets.token_urlsafe(40)
    (dirs["controller"] / "access-key").write_text(key)
    (dirs["controller"] / "access-key").chmod(0o600)
    (dirs["hermes-link"] / "service-token").write_text(secrets.token_urlsafe(48))
    dirs["hermes-link"].chmod(0o750)
    (dirs["hermes-link"] / "service-token").chmod(0o640)
    (dirs["core"] / "configuration.yaml").write_text("""homeassistant:
  name: E2E isolated home
  latitude: 0
  longitude: 0
  elevation: 0
  unit_system: metric
  time_zone: UTC
frontend:
lovelace:
api:
websocket_api:
recorder:
config:
onboarding:
http:
  server_host: 127.0.0.1
""")
    fixtures = BoundaryFixtures()
    models = web.Application()
    models.router.add_post("/v1/responses", fixtures.cli_model)
    models.router.add_post("/v1/chat/completions", fixtures.hermes_model)
    async def model_info(request):
        return web.json_response({"model_info": {"general.architecture": "fixture", "fixture.context_length": 65536}})
    models.router.add_post("/api/show", model_info)
    models_server = web.AppRunner(models)
    host = web.Application()
    host.router.add_get("/status", fixtures.host_status)
    host.router.add_post("/actions", fixtures.host_action)
    host_server = web.AppRunner(host)
    containers, errors, responses, rejections, ws_errors = [], [], [], [], []
    prefix = "agentic-e2e-" + uuid.uuid4().hex[:10]
    started = time.monotonic()

    async def launch(role, image, extra):
        name = prefix + "-" + role
        containers.append(name)
        memory, pids, cpu = (1536, 256, ["--cpus=2"]) if role == "core" else (768, 128, [])
        await docker("run", "-d", "--name", name, "--network=host", f"--memory={memory}m", f"--pids-limit={pids}", *cpu, *extra, image)
        return name

    try:
        await models_server.setup()
        site = web.TCPSite(models_server, "127.0.0.1", 0)
        await site.start()
        model_url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        await host_server.setup()
        await web.UnixSite(host_server, str(dirs["host-socket"] / "host.sock")).start()
        # Images deliberately use fixed service identities, independently of
        # the developer/GitHub UID. Only these new disposable mounts are changed.
        mounted = []
        for role in ("controller", "hermes", "hermes-link", "host-socket", "pairing"):
            mounted += ["-v", str(dirs[role]) + ":/" + role]
        preparation = """import os,sys
owner=int(sys.argv[1])
items=[('/controller',1000,1000,0o700),('/controller/access-key',1000,1000,0o600),
 ('/hermes',10003,10003,0o700),('/hermes-link',owner,1000,0o750),
 ('/hermes-link/service-token',owner,1000,0o640),('/host-socket',owner,1000,0o750),
 ('/host-socket/host.sock',owner,1000,0o660),('/pairing',owner,1000,0o750)]
for path,uid,gid,mode in items:
 assert not os.path.islink(path)
 os.chown(path,uid,gid)
 os.chmod(path,mode)
 stat=os.stat(path)
 assert (stat.st_uid,stat.st_gid,stat.st_mode&0o777)==(uid,gid,mode)
"""
        await docker("run", "--rm", "--network=none", *mounted, "--entrypoint", "python",
                     f"agentic-home-runner:{IMAGE_TAG}", "-c", preparation, str(os.getuid()))
        controller_access = """import asyncio,os,pathlib,aiohttp
assert (os.getuid(),os.getgid())==(1000,1000)
assert pathlib.Path('/controller/access-key').read_text().strip()
assert pathlib.Path('/hermes-link/service-token').read_text().strip()
assert os.access('/pairing',os.R_OK|os.X_OK)
probe=pathlib.Path('/controller/access-probe');probe.touch();probe.unlink()
async def main():
 async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path='/host-socket/host.sock')) as client:
  async with client.get('http://fixture/status') as response:assert response.status==200
asyncio.run(main())
"""
        hermes_access = """import os,pathlib
assert (os.getuid(),os.getgid())==(10003,10003) and 1000 in os.getgroups()
assert pathlib.Path('/hermes-link/service-token').read_text().strip()
probe=pathlib.Path('/hermes/access-probe');probe.touch();probe.unlink()
"""
        await asyncio.gather(
            docker("run", "--rm", "--network=none", "--user", "1000:1000", *mounted,
                   "--entrypoint", "python", f"agentic-home-controller:{IMAGE_TAG}", "-c", controller_access),
            docker("run", "--rm", "--network=none", "--user", "10003:10003", "--group-add", "1000", *mounted,
                   "--entrypoint", "python", f"agentic-home-hermes:{IMAGE_TAG}", "-c", hermes_access))
        runtime_args = ["-v", str(ROOT / "tests/e2e_runtime_fixture.py") + ":/e2e.py:ro", "-e", "E2E_MODEL_URL=" + model_url, "-e", "PYTHONPATH=/app", "--entrypoint", "python"]
        # Docker commands below use fixture-only entrypoints, with source mounted
        # read-only. No test hook or provider URL option is added to product code.
        await launch("core", CORE_IMAGE, ["-v", str(dirs["core"]) + ":/config"])
        for role in ("runner", "hermes"):
            name = prefix + "-" + role
            containers.append(name)
            extra = ["-v", str(dirs[role]) + ":/data", "-v", str(ROOT / role / ("agentic_" + role)) + ":/app/agentic_" + role + ":ro",
                     "-e", "E2E_ROLE=" + role, *runtime_args]
            if role == "runner":
                extra += ["-e", "AGENTIC_HOST_MODE=1", "-e", "AGENTIC_DISABLED_PROVIDERS=grok",
                          "-v", str(dirs["pairing"]) + ":/pairing"]
            else:
                extra += ["--group-add", "1000", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "-v", str(dirs["hermes-link"]) + ":/hermes-link:ro"]
            caps = ["--memory=1536m", "--pids-limit=256"] if role == "runner" else ["--memory=768m", "--pids-limit=64", "--cpus=1"]
            await docker("run", "-d", "--name", name, "--network=host", *caps, *extra, f"agentic-home-{role}:{IMAGE_TAG}", "/e2e.py")
        controller = await launch("controller", f"agentic-home-controller:{IMAGE_TAG}", [
            "-e", "AGENTIC_HOST_MODE=1", "-e", "AGENTIC_HERMES_MCP=1", "-e", "AGENTIC_HA_URL=" + CORE,
            "-v", str(dirs["controller"]) + ":/data", "-v", str(dirs["pairing"]) + ":/pairing:ro",
            "-v", str(dirs["hermes-link"]) + ":/hermes-link:ro", "-v", str(dirs["host-socket"]) + ":/run/agentic-home",
            "-v", str(ROOT / "controller/agentic_controller") + ":/app/agentic_controller:ro"])
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as http:
            async def get(url):
                async with http.get(url) as response:
                    return response.status, await response.json()
            await eventually(lambda: get(BASE + "/health"), lambda r: r[0] == 200)
            await eventually(lambda: get(CORE + "/api/onboarding"), lambda r: r[0] == 200, 180)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": 1440, "height": 960})
            page = await context.new_page()
            async def rejection(source, value):
                rejections.append(rejection_shape(urlsplit(source["frame"].url).netloc, value))
            await context.expose_binding("__agenticE2eRejection", rejection)
            await context.add_init_script(REJECTION_SHAPE)

            def browser_error(source, error):
                message = error.message or ""
                # A short digest tells repeats apart without keeping text that may hold a token.
                detail = {"origin": urlsplit(source.url).netloc, "name": error.name,
                          "message_sha256": hashlib.sha256(message.encode()).hexdigest()[:12]}
                if message in {"Object", "undefined", "null"} or message.isdigit():
                    detail["message"] = message
                if frame := stack_file(error.stack):
                    detail["stack_file"] = frame
                try:
                    value = json.loads(error.message)
                    if isinstance(value, dict) and value.get("code") in {"unknown_command", "not_found", "unauthorized", "invalid_format"}:
                        detail["code"] = value["code"]
                except (ValueError, TypeError):
                    pass
                errors.append(detail)

            def watch_socket(socket):
                # HA command types and error codes only. The auth frame has no id
                # and is never read; values and messages are never kept.
                if urlsplit(socket.url).path != "/api/websocket":
                    return
                sent = {}

                def frames(payload):
                    try:
                        value = json.loads(payload)
                    except (TypeError, ValueError):
                        return []
                    return [item for item in (value if isinstance(value, list) else [value]) if isinstance(item, dict)]

                def outgoing(payload):
                    for item in frames(payload):
                        kind = item.get("type")
                        if type(item.get("id")) is int and isinstance(kind, str) and re.fullmatch(r"[a-z0-9_/]{1,60}", kind):
                            sent[item["id"]] = kind

                def incoming(payload):
                    for item in frames(payload):
                        if item.get("type") == "result" and item.get("success") is False:
                            code = item["error"].get("code") if isinstance(item.get("error"), dict) else None
                            ws_errors.append({"command": sent.get(item.get("id")), "code": code if isinstance(code, str)
                                              and re.fullmatch(r"[a-z_]{1,40}", code) else None})
                socket.on("framesent", outgoing)
                socket.on("framereceived", incoming)
            page.on("pageerror", lambda e: browser_error(page, e))
            page.on("websocket", watch_socket)
            page.on("response", lambda response: responses.append({
                "path": urlsplit(response.url).path, "status": response.status})
                if urlsplit(response.url).path in {"/api/bootstrap", "/api/bootstrap/start", "/api/runtimes", "/api/status"} else None)
            page.set_default_timeout(60000)
            await page.goto(BASE + "/#onboarding")
            await page.get_by_label("主機登入金鑰", exact=True).fill(key)
            await page.get_by_role("button", name="開啟管理中心").click()
            await page.locator(".onboarding-body").get_by_role("button", name="開始設定", exact=True).click()
            hermes = page.get_by_role("region", name="智能管家登入")
            await hermes.get_by_role("radio", name="OpenRouter 金鑰", exact=True).check()
            assert fixtures.calls == ["provision", "bootstrap_hermes"] and fixtures.cli_requests == []
            await page.reload()
            await hermes.get_by_role("radio", name="OpenRouter 金鑰", exact=True).check()
            assert fixtures.calls == ["provision", "bootstrap_hermes"] and fixtures.cli_requests == []
            bootstrap = await page.request.get(BASE + "/api/bootstrap")
            assert bootstrap.status == 200
            flow = await bootstrap.json()
            assert [step["id"] for step in flow["steps"]] == [
                "network", "runner_pairing", "ha_provision", "hermes_install", "hermes_auth", "ha_native",
                "cli_auth", "cli_assist", "channel"]
            steps = {step["id"]: step for step in flow["steps"]}
            for step, code in (("cli_auth", "coding_setup_deferred"), ("cli_assist", "coding_setup_deferred"),
                               ("channel", "channel_explicitly_skipped")):
                assert (steps[step]["state"], steps[step]["code"]) == ("skipped", code)
            await hermes.get_by_role("textbox", name="模型", exact=True).fill("fixture/model")
            await hermes.get_by_role("textbox", name="金鑰", exact=True).fill(
                "e2e-synthetic-hermes-key-no-cloud")
            await hermes.get_by_role("button", name="儲存登入").click()
            await page.get_by_role("heading", name="設定你的家庭").wait_for()
            assert fixtures.calls == ["provision", "bootstrap_hermes"] and fixtures.cli_requests == []
            # A real Controller restart resumes the same persisted operation IDs.
            await docker("restart", controller)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as http:
                await eventually(lambda: get(BASE + "/health"), lambda r: r[0] == 200)
            await page.reload()
            await page.get_by_role("heading", name="設定你的家庭").wait_for()
            assert fixtures.calls == ["provision", "bootstrap_hermes"] and fixtures.cli_requests == []
            await page.screenshot(path=str(ROOT / "evidence/ui/e2e-native-home.png"), full_page=True)
            # Complete native setup through real Core endpoints in this isolated
            # household; browser login/authorization below is not intercepted.
            password = secrets.token_urlsafe(28)
            async with aiohttp.ClientSession() as http:
                async with http.post(CORE + "/api/onboarding/users", json={"client_id": CORE + "/", "name": "E2E Owner", "username": "e2e-owner", "password": password, "language": "en"}) as response:
                    assert response.status == 200
                    code = (await response.json())["auth_code"]
                async with http.post(CORE + "/auth/token", data={"grant_type": "authorization_code", "code": code, "client_id": CORE + "/"}) as response:
                    token = (await response.json())["access_token"]
                for step in ("core_config", "analytics", "integration"):
                    fields = {"client_id": CORE + "/", "redirect_uri": CORE + "/"} if step == "integration" else {}
                    async with http.post(CORE + "/api/onboarding/" + step, json=fields, headers={"Authorization": "Bearer " + token}) as response:
                        assert response.status == 200, (step, response.status)
                async with http.get(CORE + "/api/onboarding") as response:
                    native_steps = await response.json()
                    assert response.status == 200 and native_steps and all(item["done"] for item in native_steps)
            await page.get_by_role("button", name="授權連接家庭", exact=True).click()
            await page.wait_for_url("**:8123/auth/authorize?**")
            await page.get_by_label("Username", exact=True).fill("e2e-owner")
            await page.get_by_label("Password", exact=True).fill(password)
            await page.get_by_role("button", name="Log in", exact=True).click()
            await page.wait_for_url(BASE + "/#onboarding", timeout=60000)
            authorization = await page.request.get(BASE + "/api/host/authorization")
            assert authorization.status == 200 and (await authorization.json())["state"] == "connected"
            await page.get_by_role("heading", name="設定完成").wait_for()
            assert fixtures.calls == ["provision", "bootstrap_hermes"] and fixtures.cli_requests == []
            assert "code=" not in page.url and "state=" not in page.url
            await page.screenshot(path=str(ROOT / "evidence/ui/e2e-complete.png"), full_page=True)
            entry = await page.request.get(BASE + "/api/host/home-entry")
            assert entry.status == 200 and (await entry.json())["state"] == "ready"
            async with context.expect_page() as opened:
                await page.get_by_role("link", name="進入家庭 ↗", exact=True).click()
            native = await opened.value
            native.set_default_timeout(60000)
            native.on("pageerror", lambda e: browser_error(native, e))
            native.on("websocket", watch_socket)
            await native.wait_for_url(CORE + "/**")
            # Authorizing Controller does not log the native browser into its
            # separate OAuth client. Follow HA's own login when it is needed;
            # never copy Controller's bearer into frontend storage.
            username = native.get_by_label("Username", exact=True)
            await username.or_(native.locator("home-assistant-main")).first.wait_for()
            native_login_required = await username.is_visible()
            if native_login_required:
                await native.screenshot(path=str(ROOT / "evidence/ui/e2e-native-login.png"), full_page=True)
                await username.fill("e2e-owner")
                await native.get_by_label("Password", exact=True).fill(password)
                await native.get_by_role("button", name="Log in", exact=True).click()
            await native.locator("home-assistant-main").wait_for()
            # This fixture binds Core to loopback; confirm HA's own pending HTTP
            # settings dialog before judging the native dashboard ready.
            await native.get_by_role("button", name="Confirm", exact=True).click()
            await native.get_by_role("button", name="Confirm", exact=True).wait_for(state="hidden")
            await native.locator("hui-root").wait_for()
            await native.screenshot(path=str(ROOT / "evidence/ui/e2e-native-dashboard.png"), full_page=True)
            await native.close()
            await page.get_by_role("button", name="智能管家", exact=True).click()
            await page.get_by_label("對話訊息").wait_for()
            # Daily startup reaches the actual native frontend; browser Back and
            # reload preserve the outer rescue/assistant entry without a loop.
            await page.goto(BASE + "/")
            await page.wait_for_url(CORE + "/**")
            await page.locator("hui-root").wait_for()
            await page.go_back()
            await page.wait_for_url(BASE + "/#entry")
            await page.get_by_role("heading", name="家庭已就緒").wait_for()
            await page.reload()
            await page.get_by_role("heading", name="家庭已就緒").wait_for()
            assert page.url == BASE + "/#entry"
            await page.goto(BASE + "/#gateway")
            await page.get_by_label("對話訊息").fill("請使用工具查主機狀態。")
            await page.get_by_role("button", name="送出訊息", exact=True).click()
            await page.get_by_text("E2E 主機證據已取得；此為隔離測試。", exact=True).wait_for()
            await page.reload()
            await page.get_by_text("E2E 主機證據已取得；此為隔離測試。", exact=True).wait_for()
            assert not errors, errors
            assert fixtures.cli_requests == []
            storage = await page.evaluate("JSON.stringify({local:{...localStorage},session:{...sessionStorage}})")
            assert all(secret not in storage for secret in (key, password, token, "e2e-synthetic-cli-key-no-cloud", "e2e-synthetic-hermes-key-no-cloud"))
            assert any(any(m["role"] == "tool" for m in body["messages"]) for body in fixtures.hermes_requests)
            # Inspect durable allowlisted evidence and its exact case/run/message
            # association. A model's final sentence alone cannot prove MCP worked.
            script = """import json,sqlite3
c=sqlite3.connect('file:/data/controller.sqlite3?mode=ro',uri=True)
def records(kind):
 return [json.loads(row[0]) for row in c.execute('select body from records where kind=?',(kind,))]
print(json.dumps({'evidence':records('case_evidence'),
 'runs':[{k:r.get(k) for k in ('id','case_id','message_id','state')} for r in records('hermes_run')],
 'messages':[{k:m.get(k) for k in ('id','case_id','hermes_run_id','state','coordinator')} for m in records('conversation')]}))
"""
            deadline = time.monotonic() + 30
            while True:
                observed = json.loads(await docker("exec", controller, "python", "-c", script))
                evidence_cases = {item.get("case_id") for item in observed["evidence"]}
                completed = [run for run in observed["runs"] if run.get("case_id") in evidence_cases
                             and run.get("state") == "succeeded"]
                if completed and any(message.get("id") == run.get("message_id") and
                        message.get("state") == "succeeded" for run in completed for message in observed["messages"]):
                    break
                assert time.monotonic() < deadline, "case_evidence_not_persisted"
                await asyncio.sleep(.25)
            assert len(observed["evidence"]) == 1
            evidence = observed["evidence"][0]
            assert evidence["observed_from"] == "host_broker"
            assert evidence["collection_method"] == "fixed_broker_status"
            assert evidence["target_component"] == "host_and_managed_vm"
            assert evidence["redaction"] == "field_allowlist" and evidence["limitations"]
            facts = evidence["facts"]
            assert facts["vm"] == "running" and facts["kvm"] is True and facts["provisioned"] is True
            assert facts["ha_onboarding"]["observed_from"] == "outer_host_to_core_onboarding_api"
            assert facts["ha_onboarding"]["onboarding_complete"] is True
            assert facts["ha_onboarding"]["steps"] == native_steps
            expected_digest = hashlib.sha256(json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            assert evidence["content_digest"] == expected_digest
            run = next(item for item in observed["runs"] if item["case_id"] == evidence["case_id"])
            message = next(item for item in observed["messages"] if item["id"] == run["message_id"])
            assert run["state"] == message["state"] == "succeeded"
            assert message["case_id"] == evidence["case_id"] and message["hermes_run_id"] == run["id"]
            assert message["coordinator"] == "hermes"
            tool_messages = [m for body in fixtures.hermes_requests for m in body["messages"] if m["role"] == "tool"]
            assert any(evidence["evidence_id"] in json.dumps(m) and expected_digest in json.dumps(m) for m in tool_messages)
            runtime_images = {}
            expected_limits = {"core": (1536, 256, 2), "controller": (768, 128, 0),
                               "runner": (1536, 256, 0), "hermes": (768, 64, 1)}
            for name in containers:
                role = name.rsplit("-", 1)[-1]
                info = json.loads(await docker("inspect", name))[0]
                limits = info["HostConfig"]
                memory, pids, cpus = expected_limits[role]
                assert (limits["Memory"], limits["PidsLimit"], limits["NanoCpus"]) == (memory * 1024**2, pids, cpus * 10**9)
                assert info["State"]["Running"] and not info["State"]["OOMKilled"]
                runtime_images[role] = {"image_id": info["Image"], "image_ref": info["Config"]["Image"],
                    "memory_bytes": limits["Memory"], "pids_limit": limits["PidsLimit"], "nano_cpus": limits["NanoCpus"],
                    "oom_killed": info["State"]["OOMKilled"], "restart_count": info["RestartCount"]}
            report = {"kind": "composed_onboarding_e2e", "result": "passed", "duration_seconds": round(time.monotonic() - started, 2),
                "excluded_cli_probes": {"grok": "not_run: owner instruction"},
                "real": ["Chromium and compiled Web without API interception", "Controller HTTP/session/CSRF", "Runner TLS pairing and broker", "packaged Codex binary", "Hermes bridge and AIAgent", "official MCP to actual Controller", "isolated Core 2026.10.1", "native HA browser login and OAuth callback", "native dashboard entry and daily startup", "Back/reload rescue entry and explicit assistant link", "Controller restart and operation deduplication", "Web conversation reload"],
                "fixtures": ["CLI and Hermes cloud model responses", "root Host network/install/VM lifecycle effects", "synthetic model API credentials"],
                "not_run": ["real cloud OAuth", "LINE/Telegram external messages", "physical laptop", "HAOS App coding", "complete native HA setup wizard clicks"],
                "cli_inference_calls": len(fixtures.cli_requests), "hermes_model_calls": len(fixtures.hermes_requests), "host_actions": fixtures.calls,
                "mcp_evidence": evidence, "mcp_case_run_message_verified": True, "runtime_images": runtime_images,
                "native_browser_login_required": native_login_required,
                "product_service_resource_limits": "match host/compose.yaml; Core is an isolated fixture",
                "mount_access_probes": {"host_uid": os.getuid(), "controller_uid": 1000, "hermes_uid": 10003,
                                        "shared_gid": 1000, "fixed_modes_verified": True},
                "hermes_commit": REVISION, "runtime_location": "isolated_containers_on_loopback", "device_kind": "none",
                "source_hashes": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for directory in ("controller/agentic_controller", "runner/agentic_runner", "hermes/agentic_hermes") for p in sorted((ROOT / directory).glob("*.py"))}}
            (ROOT / "evidence/onboarding-e2e.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
            await browser.close()
    except Exception as exc:
        observations = {"error_type": type(exc).__name__, "host_actions": fixtures.calls,
                        "cli_calls": len(fixtures.cli_requests), "hermes_calls": len(fixtures.hermes_requests),
                        "browser_error_types": errors, "browser_rejections": rejections[-20:], "http_statuses": responses[-20:],
                        "ha_ws_errors": ws_errors[-20:],
                        "network_error_codes": [code for code in ("ERR_CONNECTION_REFUSED", "ERR_CONNECTION_RESET", "ERR_EMPTY_RESPONSE") if code in str(exc)],
                        "test_lines": [frame.lineno for frame in traceback.extract_tb(exc.__traceback__)
                                       if frame.filename == __file__]}
        for name in containers:
            with contextlib.suppress(AssertionError, ValueError):
                state = json.loads(await docker("inspect", name))[0]["State"]
                observations[name.rsplit("-", 1)[-1]] = {"status": state["Status"], "exit_code": state["ExitCode"]}
                if name.endswith("-controller") and state["Running"]:
                    observations["bootstrap"] = json.loads(await docker("exec", name, "python", "-c", BOOTSTRAP_SHAPE))
                if name.endswith("-hermes") and state["Running"]:
                    observations["hermes_auth"] = json.loads(await docker("exec", name, "python", "-c", HERMES_AUTH_SHAPE))
                    observations["hermes_processes"] = json.loads(
                        await docker("exec", name, "python", "-c", HERMES_PROCESSES))
        if type(exc).__name__ == "TimeoutError":
            text = str(exc)
            observations["wait_states"] = [label for label, phrase in WAIT_STATES if phrase in text] + (
                [] if "locator resolved to" in text else ["unresolved"])
        print("Safe E2E checkpoints:", json.dumps(observations, ensure_ascii=False))
        # Playwright errors may contain token-bearing URLs or fill values. Keep
        # the failure location and safe observations without copying its message.
        raise AssertionError("Composed E2E failed; see safe checkpoints above") from None
    finally:
        # Never emit container logs, model prompts or token-bearing URLs.
        for name in reversed(containers):
            with contextlib.suppress(AssertionError):
                await docker("rm", "-fv", name)
        await host_server.cleanup()
        await models_server.cleanup()
        await docker("run", "--rm", "-v", str(tmp_path) + ":/lab", "-v", str(dirs["host-socket"]) + ":/host-socket",
                     "--entrypoint", "chown", f"agentic-home-runner:{IMAGE_TAG}", "-R", f"{os.getuid()}:{os.getgid()}", "/lab", "/host-socket")
