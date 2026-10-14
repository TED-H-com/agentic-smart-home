"""Real Linux peer credentials against the packaged broker, without host mounts."""
import json
import subprocess
import time
import tomllib
import uuid
from pathlib import Path

import pytest


def test_actual_unix_broker_separates_runner_from_owner_and_cli_uids(request):
    if not request.config.getoption("--containers"):
        pytest.skip("requires packaged container environment")
    root = Path(__file__).resolve().parents[1]
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    image = "agentic-home-preview:" + version
    name = "agentic-broker-roles-" + uuid.uuid4().hex
    broker = """import json
from pathlib import Path
from agentic_host.daemon import serve
from agentic_host.manager import Manager
root=Path('/var/lib/agentic-home/host');root.mkdir(parents=True,exist_ok=True)
job={'id':'fixture-job','request':{'id':'fixture-job','action':'snapshot','snapshot':None},'state':'running','at':1.0}
(root/'jobs.json').write_text(json.dumps({'fixture-job':job}))
def unavailable_status(self):raise RuntimeError('live-status-observer-fixture')
Manager.status=unavailable_status
serve()
"""
    try:
        subprocess.run(["docker", "run", "-d", "--name", name, "--network", "none", "--user", "0:0",
                        "--memory", "256m", "--pids-limit", "32", "--cpus", "1", "--entrypoint", "python",
                        "--mount", f"type=bind,src={root},dst=/opt/agentic-home,readonly", "--env", "PYTHONPATH=/opt/agentic-home/host",
                        image, "-c", broker], check=True, capture_output=True)
        for _ in range(40):
            ready = subprocess.run(["docker", "exec", name, "python", "-c",
                                    "from pathlib import Path; raise SystemExit(not Path('/run/agentic-home/host.sock').exists())"], capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(.1)
        assert ready.returncode == 0
        script = """import http.client,json,socket
class C(http.client.HTTPConnection):
 def connect(self):
  self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.connect('/run/agentic-home/host.sock')
result={}
for path in ['/network','/coding/capabilities','/status','/jobs/fixture-job','/jobs/missing-id','/jobs/../invalid']:
 try:
  c=C('localhost');c.request('GET',path);r=c.getresponse();result[path]=r.status;body=json.loads(r.read());c.close()
  if path=='/jobs/fixture-job' and r.status==200:result['receipt']={key:body[key] for key in ['id','request','state']}
 except PermissionError:result[path]='filesystem_denied'
print(json.dumps(result))
"""
        def probe(uid):
            output = subprocess.run(["docker", "exec", "--user", f"{uid}:{uid}", name, "python", "-c", script], check=True, capture_output=True, text=True)
            return json.loads(output.stdout)
        paths = ["/network", "/coding/capabilities", "/status", "/jobs/fixture-job", "/jobs/missing-id", "/jobs/../invalid"]
        assert probe(0) == {path: 200 if path == "/coding/capabilities" else 403 for path in paths}
        owner = probe(1000)
        assert owner == {"/network": 200, "/coding/capabilities": 403, "/status": 503,
                         "/jobs/fixture-job": 200, "/jobs/missing-id": 404, "/jobs/../invalid": 409,
                         "receipt": {"id": "fixture-job", "request": {"id": "fixture-job", "action": "snapshot", "snapshot": None},
                                     "state": "needs_reconcile"}}
        assert probe(1001) == dict.fromkeys(paths, "filesystem_denied")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=120)
