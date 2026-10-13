"""Trusted fixed launcher. The agent's commands execute only inside the bounded worker."""
from __future__ import annotations

import json

from .coding_relay import IDLE_TIMEOUT

# Placeholders are replaced below. The sandbox cannot import this module.
_PROXY = r'''
import http.client,json,os,socket,subprocess,threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
class Unix(http.client.HTTPConnection):
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.sock.settimeout(@@IDLE@@)
        self.sock.connect(@@RELAY@@)
class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*args):pass
    def setup(self):
        super().setup()
        self.connection.settimeout(@@IDLE@@)
        self._started=False
        try:self.connection.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
        except OSError:pass
    def _fail(self,code):
        if self._started:
            self.close_connection=True
            try:self.connection.shutdown(socket.SHUT_WR)
            except OSError:pass
            return
        self.send_error(code)
    def transfer(self):
        self.close_connection=True
        try:size=int(self.headers.get('Content-Length','0'))
        except ValueError:
            self._fail(400);return
        if self.headers.get('Transfer-Encoding') or not 0<=size<=2097152:
            self._fail(413);return
        client=Unix('relay',timeout=@@IDLE@@)
        try:
            client.request(self.command,self.path,body=self.rfile.read(size),headers={'Content-Type':'application/json'})
            result=client.getresponse()
            self.send_response(result.status)
            self._started=True
            self.send_header('Content-Type',result.getheader('Content-Type','application/octet-stream'))
            self.send_header('Transfer-Encoding','chunked')
            self.send_header('Connection','close')
            self.end_headers()
            self.wfile.flush()
            total=0
            while True:
                chunk=result.read1(65536)
                if not chunk:break
                total+=len(chunk)
                if total>8388608:raise ValueError('response limit')
                self.wfile.write(f'{len(chunk):X}\r\n'.encode()+chunk+b'\r\n')
                self.wfile.flush()
            self.wfile.write(b'0\r\n\r\n')
            self.wfile.flush()
        except (OSError,ValueError,http.client.HTTPException):
            try:self._fail(502)
            except OSError:pass
        finally:client.close()
    do_GET=do_POST=transfer
server=ThreadingHTTPServer(('127.0.0.1',@@PORT@@),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
'''


def sandbox_proxy(relay_socket="/relay.sock", port=8765, idle=IDLE_TIMEOUT):
    """In-sandbox HTTP proxy. It streams relay bytes and never sees provider credentials."""
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("Invalid proxy port")
    if not isinstance(relay_socket, str) or not relay_socket or "\n" in relay_socket or "\x00" in relay_socket:
        raise ValueError("Invalid relay socket")
    if isinstance(idle, bool) or not isinstance(idle, (int, float)) or not idle > 0:
        raise ValueError("Invalid proxy idle timeout")
    values = {"@@RELAY@@": repr(relay_socket), "@@PORT@@": str(port), "@@IDLE@@": repr(float(idle))}
    source = _PROXY
    for key, value in values.items():
        if key in value:
            raise ValueError("Invalid proxy substitution")
        source = source.replace(key, value)
    if "@@" in source:
        raise ValueError("Unresolved proxy placeholder")
    return source


_LAUNCHER = r'''
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
if OBSERVED_FROM == 'haos_app':
    prompt += ('\nFor fresh local observations, GET http://127.0.0.1:8765/observe with Python urllib.request. '
        'This fixed read-only endpoint reports only the HAOS App perspective and unauthenticated Core API '
        'reachability. It does not prove Core container or whole HAOS health. Use at most eight observations; '
        'you may write reproductions against the returned facts. No Home Assistant credentials are available.')
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

PROGRAM = sandbox_proxy() + _LAUNCHER


def program(request, *, observed_from="host_container"):
    if observed_from not in {"host_container", "haos_app"}:
        raise ValueError("Unsupported observation location")
    safe = {key: request[key] for key in ("mode", "goal", "questions", "model")}
    return "REQUEST=" + repr(safe) + "\nOBSERVED_FROM=" + repr(observed_from) + "\n" + PROGRAM


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
bootstrap = """
import json,os,sys
import pytest
import pytest_asyncio.plugin
fd=int(sys.argv[1])
class Completion:
    collected=0
    executed=0
    passed=0
    bad=0
    def pytest_collection_finish(self,session):
        self.collected=len(session.items)
    def pytest_runtest_logreport(self,report):
        if report.when=='call':
            self.executed+=1
            if report.passed and not getattr(report,'wasxfail',False):self.passed+=1
        if report.failed or report.skipped or getattr(report,'wasxfail',False):self.bad+=1
completion=Completion()
sys.path[:0]=['/work/source/controller','/work/source/runner','/work/source/hermes']
code=pytest.main(sys.argv[2:],plugins=[completion])
os.write(fd,json.dumps({'completed':True,'collected':completion.collected,'executed':completion.executed,
    'passed':completion.passed,'bad':completion.bad}).encode())
os.close(fd)
raise SystemExit(code)
"""
read_fd,write_fd=os.pipe()
os.set_blocking(read_fd,False)
args=['python','-I','-c',bootstrap,str(write_fd),'-q','-p','no:cacheprovider','-p','pytest_asyncio.plugin','-c','/work/verification.ini',
      '--confcutdir=/work/trusted-tests','--basetemp=/work/scratch/pytest','/work/trusted-tests']
os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD']='1'
completion={}
try:
    if result.returncode==0:
        with open('/work/artifacts/verification.log','wb') as stream:
            result=subprocess.run(args,stdout=stream,stderr=subprocess.STDOUT,check=False,pass_fds=(write_fd,))
finally:
    os.close(write_fd)
    try:
        raw=os.read(read_fd,4097)
        if len(raw)<=4096:completion=json.loads(raw)
    except (OSError,ValueError):pass
    finally:os.close(read_fd)
valid=(isinstance(completion,dict) and completion.get('completed') is True and
    all(type(completion.get(k)) is int for k in ('collected','executed','passed','bad')) and
    completion['collected']>0 and completion['executed']==completion['collected']==completion['passed'] and
    completion['bad']==0)
code=result.returncode if result.returncode else (0 if valid else 1)
# A normal pytest completion is required. Candidate code shares the test interpreter;
# this receipt is not a cryptographic proof against hostile interpreter tampering.
Path('/work/artifacts/verification.json').write_text(json.dumps({'state':'passed' if code==0 else 'failed',
    'patch_sha256':hashlib.sha256(patch).hexdigest(),'exit_code':code,'completion_verified':valid,
    'tests_executed':completion.get('executed',0) if valid else 0,
    'test_command_sha256':hashlib.sha256(json.dumps(args).encode()).hexdigest(),
    'observed_from':globals().get('VERIFICATION_LOCATION','independent_host_verifier'),'trusted_tests':'pinned_baseline',
    'credentials_present':False,'network':'none','approval':False}))
raise SystemExit(code)
'''


def verification_program(observed_from="independent_host_verifier"):
    if observed_from not in {"independent_host_verifier", "independent_home_verifier"}:
        raise ValueError("Unsupported verifier location")
    return "VERIFICATION_LOCATION=" + repr(observed_from) + "\n" + VERIFY_PROGRAM
