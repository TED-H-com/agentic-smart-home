"""Real OpenSSL, extraction and symlink recovery inside a disposable root container.

Services/HA/LVM are fixed fixtures here; physical and nested-HAOS acceptance is
separate. No host mount is writable and the container has no network.
"""
import json
import subprocess
import tomllib
from pathlib import Path

import pytest
from test_host_update import envelope
from test_host_update import lab as host_update_lab

lab = host_update_lab

PROBE = r'''
import json,os,shutil,sqlite3,subprocess,threading,time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from agentic_host.host_update import HostBundle,CONTRACT,COMPATIBILITY
from agentic_host.host_reconcile import HostReconcile
from agentic_host.updater import offline_check
from agentic_host.update_service import create_server
fixture=json.loads(Path('/fixture/payload.json').read_text())
root=Path('/var/lib/agentic-home');root.mkdir(mode=0o700)
bundles=Path('/opt/agentic-home-bundles');previous=bundles/'baseline';(previous/'assets').mkdir(parents=True)
(previous/'assets/keep').write_text('retained asset')
(previous/'images.lock.json').write_text('{}');(previous/'providers.lock.json').write_text('{}')
(previous/'host').mkdir();(previous/'host/compose.yaml').write_text('fixture source')
(previous/'host/bases.lock.json').write_text('{}');(previous/'host/network.lock.json').write_text('{}')
active=Path('/opt/agentic-home');active.symlink_to(previous,target_is_directory=True)
state=root/'host';state.mkdir(parents=True)
database=state.parent/'controller/controller.sqlite3';database.parent.mkdir()
with sqlite3.connect(database) as db:
 db.executescript("PRAGMA user_version=3;CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);CREATE TABLE records(kind TEXT,id TEXT,body TEXT);INSERT INTO settings VALUES('household_saved','current');")
authority=root/'authority.json';authority.write_text(json.dumps(fixture['authority']))
trust=root/'trust.json';trust.write_text(json.dumps(fixture['trust']))
class Restore:
 def capacity(self,n):pass
 def create(self,record):return {'phase':'complete','scope':'container fixture; no real LVM/HAOS','ha_snapshot':None,'root_snapshot_name':None}
def run(*args,**kwargs):
 if args[0]!='openssl':raise RuntimeError('unexpected command')
 return subprocess.run(args,capture_output=True,timeout=10)
def fetch(url,target,maximum):
 data=Path('/fixture/product.tar').read_bytes();assert len(data)<=maximum;Path(target).write_bytes(data)
manager=SimpleNamespace(root=state,product=active,run=run,locked=nullcontext)
def engine():
 e=HostBundle(manager,bundles=bundles,authority=authority,trust=trust,restore=Restore(),download=fetch,launch=lambda:None,
   broker_probe=lambda:{'contract':CONTRACT,'bridge':{'controller_update':{'ingress_bridge_contract':1}},'status':{'vm':'not_created'}},
   controller_probe=lambda:{'status':'ok','host_update':COMPATIBILITY})
 def stop(record):
  record['controller_was_running']=True;offline_check(database,3,pause=True,gate=record['candidate_digest'])
 e._stop_controller=stop;e._start_services=lambda record:None
 def hook(action,candidate,**kwargs):
  environment={'PATH':'/usr/local/bin:/usr/bin:/bin','PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':'/product/host' if kwargs.get('immutable') else str(active/'host')}
  result=subprocess.run(['python','-m','agentic_host.host_reconcile','--'+action,'--candidate',candidate],capture_output=True,env=environment,timeout=30)
  if result.returncode:raise RuntimeError('fixed reconcile CLI failed: '+result.stderr.decode(errors='replace'))
 e._hook=hook
 return e
e=engine();record=e.stage(fixture['envelope'],background=False);assert record['state']=='staged'
assert active.resolve()==previous and (bundles/record['candidate_digest']/'assets/keep').read_text()=='retained asset'
e._approval({'candidate_digest':record['candidate_digest'],'approval_id':record['approval_id']})
def dispatch_gate():
 saved=e.records()[record['candidate_digest']]
 with sqlite3.connect(database) as db:
  db.execute("INSERT OR REPLACE INTO settings VALUES ('host_bundle_update_gate',?)",(json.dumps({'candidate_digest':saved['candidate_digest'],'operation':saved.get('consumed_action',saved['operation']),'approval_digest':saved['consumed_token_sha256']}),))
def gate_cleared():
 with sqlite3.connect(database) as db:
  assert json.loads(db.execute("SELECT value FROM settings WHERE key='host_bundle_update_gate'").fetchone()[0]) is None
dispatch_gate()
result=e.execute();assert result['state']=='deployed',result
gate_cleared()
cache=active/'host/agentic_host/__pycache__';cache.mkdir();(cache/'runtime.pyc').write_bytes(b'cache')
offer=e.rollback_offer({'candidate_digest':record['candidate_digest']})
e._approval({'candidate_digest':record['candidate_digest'],'approval_id':offer['rollback_approval_id']},rollback=True)
dispatch_gate()
assert e.execute()['state']=='rolled_back' and active.resolve()==previous
gate_cleared()
# Persist an already-approved switched attempt, then discard the entire engine
# object. Its replacement must recover exclusively from root-owned disk state.
saved=e.records()[record['candidate_digest']];saved.update(state='activating',phase='reconciling',operation='apply',recovery_only=False)
e.save(saved);e._switch(bundles/record['candidate_digest']);del e
e=engine();recovered=e.execute(recover=True)
assert recovered['state']=='rolled_back' and recovered['auto_rollback'] and active.resolve()==previous
with sqlite3.connect(database) as db:
 values=dict(db.execute('SELECT key,value FROM settings'))
 assert values['household_saved']=='current' and values['paused']=='true' and 'updater_gate' not in values
 assert db.execute('PRAGMA user_version').fetchone()[0]==3
# The retained 0.11 broker has no Host-update routes. The external immutable
# service must still accept the next signed candidate without replaying bootstrap.
transport=Path('/run/agentic-home');transport.mkdir(mode=0o755,parents=True,exist_ok=True)
os.chmod(transport,0o755)
legacy=create_server(transport/'host.sock',SimpleNamespace(handle=lambda *args:(404,{'detail':'Unknown route'})))
stable=create_server(transport/'host-updates.sock',e)
for server in (legacy,stable):threading.Thread(target=server.serve_forever,daemon=True).start()
client=r"""
import http.client,json,socket,sys
class Client(http.client.HTTPConnection):
 def connect(self):
  self.sock=socket.socket(socket.AF_UNIX);self.sock.settimeout(5);self.sock.connect(sys.argv[1])
c=Client('host');c.request(sys.argv[2],sys.argv[3],body=sys.stdin.read() or None)
r=c.getresponse();print(json.dumps({'status':r.status,'body':json.loads(r.read())}))
"""
def request(path,method='GET',route='/host-updates',body=None,owner=True):
 def identity():os.setgid(1000);os.setuid(1000)
 result=subprocess.run(['python','-c',client,str(path),method,route],input=json.dumps(body) if body is not None else '',capture_output=True,text=True,timeout=10,preexec_fn=identity if owner else None)
 assert result.returncode==0
 return json.loads(result.stdout)
assert request(transport/'host.sock')['status']==404
assert request(transport/'host-updates.sock',owner=False)['status']==403
assert request(transport/'host-updates.sock')['body']['available'] is True
future=fixture['future_envelope']
response=request(transport/'host-updates.sock','POST','/host-updates/stage',future)
assert response['status']==202;candidate=response['body']['candidate_digest']
for _ in range(100):
 next_record=request(transport/'host-updates.sock',route='/host-updates/'+candidate)['body']
 if next_record['state']!='staging':break
 time.sleep(.02)
assert next_record['state']=='staged' and candidate!=record['candidate_digest'] and active.resolve()==previous
for server in (legacy,stable):server.shutdown();server.server_close()
shutil.rmtree(root);active.unlink();shutil.rmtree(bundles)
print(json.dumps({'signature':'passed','safe_extraction':'passed','atomic_switch':'passed','manual_rollback':'passed','interrupted_executor_recovery':'passed','legacy_fallback_stable_transport':'passed','root_runner_denied':'passed','dispatch_gate_cleanup_without_browser':'passed','current_database_preserved':True,'physical_or_nested_haos':'not_run'}))
'''


def test_root_container_extract_switch_rollback_and_interrupted_executor(lab, tmp_path, request):
    if not request.config.getoption("--containers"):
        pytest.skip("requires packaged container environment")
    root = Path(__file__).parents[1]
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    from test_host_update import files_for
    files = files_for(lab)
    for name in ("__init__.py", "manager.py", "artifacts.py", "host_reconcile.py", "appliance_update.py",
                 "appliance_network.py", "appliance_power.py"):
        files["host/agentic_host/" + name] = (root / "host/agentic_host" / name).read_bytes()
    files["host/managed-system.json"] = b'{"format":1,"files":[]}'
    body = envelope(lab, files=files)
    future = envelope(lab, files=files, manifest_change={"version": "0.11.2", "source_commit": "d" * 40,
                      "bundle": {**body["manifest"]["bundle"], "url": "https://github.com/TED-H-com/agentic-smart-home/releases/download/v0.11.2/agentic-home-host-0.11.2.tar"}})
    payload = {"envelope": body, "future_envelope": future,
               "authority": json.loads(lab.authority.read_text()), "trust": json.loads(lab.trust.read_text())}
    (tmp_path / "payload.json").write_text(json.dumps(payload))
    (tmp_path / "product.tar").write_bytes(lab._test.archives[body["manifest"]["bundle"]["url"]])
    (tmp_path / "payload.json").chmod(0o644)
    (tmp_path / "product.tar").chmod(0o644)
    result = subprocess.run(["docker", "run", "--rm", "--network", "none", "--user", "0:0", "--cap-drop", "ALL",
                             "--cap-add", "SETUID", "--cap-add", "SETGID", "--cap-add", "CHOWN",
                             "--memory", "256m", "--pids-limit", "32", "--entrypoint", "python",
                             "--mount", f"type=bind,src={root / 'host'},dst=/product/host,readonly",
                             "--mount", f"type=bind,src={tmp_path / 'payload.json'},dst=/fixture/payload.json,readonly",
                             "--mount", f"type=bind,src={tmp_path / 'product.tar'},dst=/fixture/product.tar,readonly",
                             "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTHONPATH=/product/host",
                             "agentic-home-preview:" + version, "-c", PROBE],
                            check=False, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["signature"] == evidence["safe_extraction"] == evidence["atomic_switch"] == "passed"
    assert evidence["manual_rollback"] == evidence["interrupted_executor_recovery"] == "passed"
    assert evidence["legacy_fallback_stable_transport"] == evidence["root_runner_denied"] == "passed"
    assert evidence["dispatch_gate_cleanup_without_browser"] == "passed"
    assert evidence["current_database_preserved"] is True
