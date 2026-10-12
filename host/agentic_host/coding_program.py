"""Trusted fixed launcher. The agent's commands execute only inside the bounded worker."""
from __future__ import annotations

import json

PROGRAM = r'''
import http.client,json,os,socket,subprocess,threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
class Unix(http.client.HTTPConnection):
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.sock.settimeout(50)
        self.sock.connect('/relay.sock')
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def transfer(self):
        size=int(self.headers.get('Content-Length','0'))
        if not 0<=size<=2097152:
            self.send_error(413);return
        client=Unix('relay',timeout=50)
        try:
            client.request(self.command,self.path,body=self.rfile.read(size),headers={'Content-Type':'application/json'})
            result=client.getresponse();body=result.read(8388609)
            if len(body)>8388608:raise ValueError('response limit')
            self.send_response(result.status)
            self.send_header('Content-Type',result.getheader('Content-Type','application/octet-stream'))
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        except (OSError,ValueError):self.send_error(502)
        finally:client.close()
    do_GET=do_POST=transfer
server=ThreadingHTTPServer(('127.0.0.1',8765),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
os.environ.update({'CODEX_HOME':'/work/profile','HOME':'/work/profile','TMPDIR':'/work/scratch',
    'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null'})
catalog=json.loads(Path('/app/agentic_runner/codex-models.json').read_text())
for model in catalog['models']:
    model['multi_agent_version']=None
Path('/work/profile/coding-models.json').write_text(json.dumps(catalog))
config={'model_provider':'bounded','model':REQUEST['model'] or 'gpt-6-astra',
    'model_providers.bounded.name':'Agentic fixed provider relay',
    'model_providers.bounded.base_url':'http://127.0.0.1:8765/v1',
    'model_providers.bounded.wire_api':'responses','model_providers.bounded.requires_openai_auth':False,
    'model_catalog_json':'/work/profile/coding-models.json','web_search':'disabled','agents.max_threads':1}
args=['codex','exec','--ignore-user-config','--ephemeral','--skip-git-repo-check',
    '--dangerously-bypass-approvals-and-sandbox','--json','--output-last-message','/work/scratch/answer.txt']
for key,value in config.items():args+=['-c',key+'='+json.dumps(value)]
for feature in ('enable_request_compression','apps','plugins','hooks','multi_agent','multi_agent_v2',
    'browser_use','browser_use_external','computer_use','image_generation','code_mode','code_mode_host',
    'skill_search','skill_mcp_dependency_install','workspace_dependencies','memories','goals'):
    args+=['--disable',feature]
args+=['-']
prompt=('You are the '+REQUEST['mode']+' coding worker. Work only in /work/source, scratch /work/scratch. '
    'Live household/host services and credentials are unavailable. Do not spawn agents. '
    'Investigate actual files, use shell tools to reproduce and test, then propose the smallest repair. '
    'Git metadata is read only. Do not install packages or alter git metadata. '
    'Public source research uses Python urllib.request on http://127.0.0.1:8765/fetch?url='+
    '<URL-encoded https URL>. Only fixed official documentation and GitHub hosts are allowed. '
    'Write safe text artifacts under /work/artifacts. Finish with facts, inferences, unknowns, '
    'experiments, next_steps; do not claim deployment or approval.\nGoal: '+REQUEST['goal']+
    '\nQuestions: '+json.dumps(REQUEST['questions']))
with open('/work/scratch/events.jsonl','wb') as output:
    run=subprocess.run(args,input=prompt.encode(),stdout=output,stderr=subprocess.STDOUT,check=False)
server.shutdown()
# The broker independently observes exit and gathers evidence after all processes stop.
Path('/work/artifacts/execution.json').write_text(json.dumps({'cli_exit_code':run.returncode,
    'provider':'codex','cli_version':'0.157.1','mode':REQUEST['mode'],'live_mutations':False}))
answer=Path('/work/scratch/answer.txt')
if answer.exists() and answer.stat().st_size<=65536:
    Path('/work/artifacts/findings.txt').write_bytes(answer.read_bytes())
raise SystemExit(run.returncode)
'''


def program(request):
    safe = {key: request[key] for key in ("mode", "goal", "questions", "model")}
    return "REQUEST=" + repr(safe) + "\n" + PROGRAM


def digest_request(request):
    import hashlib
    return hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


VERIFY_PROGRAM = r'''
import hashlib,json,os,subprocess
from pathlib import Path
patch=Path('/work/candidate.patch').read_bytes()
result=subprocess.run(['git','-c','core.hooksPath=/dev/null','apply','--check','/work/candidate.patch'],capture_output=True)
if result.returncode==0:
    result=subprocess.run(['git','-c','core.hooksPath=/dev/null','apply','/work/candidate.patch'],capture_output=True)
bootstrap=('import sys; import pytest; import pytest_asyncio.plugin; '
    "sys.path[:0]=['/work/source/controller','/work/source/runner','/work/source/hermes']; "
    'raise SystemExit(pytest.main(sys.argv[1:]))')
args=['python','-I','-c',bootstrap,'-q','-p','pytest_asyncio.plugin','-c','/work/verification.ini',
      '--confcutdir=/work/trusted-tests','--basetemp=/work/scratch/pytest','/work/trusted-tests']
os.environ['PYTHONPATH']='/work/source/controller:/work/source/runner:/work/source/hermes'
os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD']='1'
if result.returncode==0:
    with open('/work/artifacts/verification.log','wb') as stream:
        result=subprocess.run(args,stdout=stream,stderr=subprocess.STDOUT,check=False)
Path('/work/artifacts/verification.json').write_text(json.dumps({'state':'passed' if result.returncode==0 else 'failed',
    'patch_sha256':hashlib.sha256(patch).hexdigest(),'exit_code':result.returncode,
    'test_command_sha256':hashlib.sha256(json.dumps(args).encode()).hexdigest(),
    'observed_from':'independent_host_verifier','trusted_tests':'pinned_baseline',
    'credentials_present':False,'network':'none','approval':False}))
raise SystemExit(result.returncode)
'''
