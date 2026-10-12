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
    try:
        subprocess.run(["docker", "run", "-d", "--name", name, "--network", "none", "--user", "0:0",
                        "--memory", "256m", "--pids-limit", "32", "--cpus", "1", "--entrypoint", "python",
                        "--mount", f"type=bind,src={root},dst=/opt/agentic-home,readonly", "--env", "PYTHONPATH=/opt/agentic-home/host",
                        image, "-m", "agentic_host.daemon"], check=True, capture_output=True)
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
for path in ['/network','/coding/capabilities']:
 try:
  c=C('localhost');c.request('GET',path);r=c.getresponse();result[path]=r.status;r.read();c.close()
 except PermissionError:result[path]='filesystem_denied'
print(json.dumps(result))
"""
        def probe(uid):
            output = subprocess.run(["docker", "exec", "--user", f"{uid}:{uid}", name, "python", "-c", script], check=True, capture_output=True, text=True)
            return json.loads(output.stdout)
        assert probe(0) == {"/network": 403, "/coding/capabilities": 200}
        assert probe(1000) == {"/network": 200, "/coding/capabilities": 403}
        assert probe(1001) == {"/network": "filesystem_denied", "/coding/capabilities": "filesystem_denied"}
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=20)
