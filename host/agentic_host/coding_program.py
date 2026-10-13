"""Trusted fixed launcher. The agent's commands execute only inside the bounded worker."""
from __future__ import annotations

import json

from .coding_relay import (
    DENIED_HEADERS,
    IDLE_TIMEOUT,
    MAX_FORWARDED_REQUEST_HEADERS,
    MAX_FORWARDED_RESPONSE_HEADERS,
    RATE_FIELDS,
    REACHED_TYPES,
    REQUEST_HEADER_RULES,
    RESPONSE_HEADER_RULES,
)

# Placeholders are replaced below. The sandbox cannot import this module.
# chatgpt=True below forwards the union. The proxy must not learn which credential
# the relay holds; the relay applies this same allowlist again and drops ChatGPT-only names.
_PROXY = r'''
import http.client,json,os,socket,subprocess,threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
REQUEST_RULES=@@REQUEST_RULES@@
RESPONSE_RULES=@@RESPONSE_RULES@@
RATE_FIELDS=@@RATE_FIELDS@@
REACHED=frozenset(@@REACHED@@)
DENIED=frozenset(@@DENIED@@)
_REQUEST={name:(limit,kind,only) for name,limit,kind,only in REQUEST_RULES}
_RESPONSE={name:(limit,kind) for name,limit,kind in RESPONSE_RULES}
FORWARDED_REQUEST_CAP=@@MAX_FORWARDED_REQUEST@@
FORWARDED_RESPONSE_CAP=@@MAX_FORWARDED_RESPONSE@@
def _ascii(value,limit):
    return isinstance(value,str) and 0<len(value)<=limit and all(32<=ord(ch)<=126 for ch in value)
def _hex_uuid(value):
    if not isinstance(value,str) or len(value)!=36:return False
    parts=value.split('-')
    if [len(part) for part in parts]!=[8,4,4,4,12]:return False
    return all(ch in '0123456789abcdefABCDEF' for part in parts for ch in part)
def _slug(value,limit):
    return _ascii(value,limit) and all(ch.isalnum() or ch in '._:-' for ch in value)
def _model_slug(value,limit):
    return isinstance(value,str) and 0<len(value)<=limit and all(ch.isalnum() or ch in '._-' for ch in value)
def _kind_ok(value,kind,limit):
    if kind=='ascii':return _ascii(value,limit)
    if kind=='true':return value=='true'
    if kind=='uuid':return _hex_uuid(value)
    if kind=='originator':
        if not _ascii(value,limit) or not value[0].isalnum():return False
        return all(ch.isalnum() or ch in ' ._/-' for ch in value)
    if kind=='beta':
        if not _ascii(value,limit):return False
        parts=value.split(',')
        return all(part and all(ch.islower() or ch.isdigit() or ch=='_' for ch in part) for part in parts)
    if kind=='window':
        if not isinstance(value,str) or value.count(':')!=1:return False
        thread,number=value.split(':',1)
        return _hex_uuid(thread) and number.isdigit() and 1<=len(number)<=20
    if kind=='session':
        if _hex_uuid(value):return True
        if not isinstance(value,str) or value.count(':')!=1 or not _ascii(value,limit):return False
        label,thread=value.split(':',1)
        label_ok=1<=len(label)<=64 and all(ch.islower() or ch.isdigit() or ch=='_' for ch in label)
        return label_ok and _hex_uuid(thread)
    if kind=='subagent':return _ascii(value,limit) and all(ch.isalnum() or ch in '_-' for ch in value)
    if kind=='routing':
        if not _ascii(value,limit) or not value.startswith('model='):return False
        rest=value[6:]
        if ';tier=' in rest:
            model,_,tier=rest.partition(';tier=')
            return _model_slug(model,64) and _model_slug(tier,32)
        return _model_slug(rest,64) and ';' not in rest
    if kind=='metadata':
        if not _ascii(value,limit) or not value.startswith('{'):return False
        try:parsed=json.loads(value)
        except ValueError:return False
        return isinstance(parsed,dict)
    if kind=='slug':return _slug(value,limit)
    if kind=='decimal':
        if not isinstance(value,str) or not value:return False
        whole,dot,frac=value.partition('.')
        if not whole.isdigit() or not 1<=len(whole)<=6:return False
        if not dot:return True
        return frac.isdigit() and 1<=len(frac)<=4
    if kind=='int':return isinstance(value,str) and value.isdigit() and 1<=len(value)<=min(limit,19)
    if kind=='bool':return isinstance(value,str) and value.lower() in {'true','false','0','1'}
    if kind=='reached':return value in REACHED
    if kind=='limit-id':return _ascii(value,limit) and all(ch.isalnum() or ch in '_-' for ch in value)
    return False
def _denied(name):
    if name in DENIED or name.startswith('proxy-'):return True
    if name.endswith('-key') or name.endswith('_key'):return True
    return 'token' in name or 'bearer' in name or 'cookie' in name or 'authorization' in name
def _pairs(headers):
    if headers is None:return ()
    items=getattr(headers,'items',None)
    if callable(items):return items()
    if isinstance(headers,(str,bytes,bytearray)):return ()
    try:return [(name,value) for name,value in headers]
    except (TypeError,ValueError):return ()
def _header_name(raw):
    if not isinstance(raw,str):return None
    name=raw.strip().lower()
    if not name or not _ascii(name,128):return None
    return name
def _limit_token(token):
    if not token or len(token)>33:return False
    if not (token[0].islower() or token[0].isdigit()):return False
    return all(ch.islower() or ch.isdigit() or ch=='-' for ch in token)
def _rate_kind(name):
    if not name.startswith('x-'):return None
    for suffix,kind in RATE_FIELDS:
        marker='-'+suffix
        if name.endswith(marker) and _limit_token(name[2:-len(marker)]):return kind
    return None
def select_request_headers(headers,*,chatgpt):
    chosen,seen={},set()
    for raw_name,raw_value in _pairs(headers):
        name=_header_name(raw_name)
        if name is None or name in seen:continue
        seen.add(name)
        rule=_REQUEST.get(name)
        if rule is None or _denied(name) or not isinstance(raw_value,str):continue
        limit,kind,only=rule
        if only and not chatgpt:continue
        if not _kind_ok(raw_value,kind,limit):continue
        chosen[name]=raw_value
        if len(chosen)>=FORWARDED_REQUEST_CAP:break
    return chosen
def select_response_headers(headers):
    chosen,seen={},set()
    for raw_name,raw_value in _pairs(headers):
        name=_header_name(raw_name)
        if name is None or name in seen:continue
        seen.add(name)
        if _denied(name) or not isinstance(raw_value,str):continue
        rule=_RESPONSE.get(name)
        if rule is not None:
            limit,kind=rule
        else:
            kind=_rate_kind(name)
            if kind is None:continue
            limit=64 if kind=='ascii' else 19 if kind=='int' else 16
        if not _kind_ok(raw_value,kind,limit):continue
        chosen[name]=raw_value
        if len(chosen)>=FORWARDED_RESPONSE_CAP:break
    return chosen
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
            outbound={'Content-Type':'application/json'}
            outbound.update(select_request_headers(self.headers,chatgpt=True))
            client.request(self.command,self.path,body=self.rfile.read(size),headers=outbound)
            result=client.getresponse()
            self.send_response(result.status)
            self._started=True
            self.send_header('Content-Type',result.getheader('Content-Type','application/octet-stream'))
            for name,value in select_response_headers(result.getheaders()).items():
                self.send_header(name,value)
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
    values = {
        "@@RELAY@@": repr(relay_socket),
        "@@PORT@@": str(port),
        "@@IDLE@@": repr(float(idle)),
        "@@REQUEST_RULES@@": repr(tuple(REQUEST_HEADER_RULES)),
        "@@RESPONSE_RULES@@": repr(tuple(RESPONSE_HEADER_RULES)),
        "@@RATE_FIELDS@@": repr(tuple(RATE_FIELDS)),
        "@@REACHED@@": repr(tuple(REACHED_TYPES)),
        "@@DENIED@@": repr(tuple(DENIED_HEADERS)),
        "@@MAX_FORWARDED_REQUEST@@": str(int(MAX_FORWARDED_REQUEST_HEADERS)),
        "@@MAX_FORWARDED_RESPONSE@@": str(int(MAX_FORWARDED_RESPONSE_HEADERS)),
    }
    source = _PROXY
    for key, value in values.items():
        if key in value or "@@" in value:
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


def web_build_step_seconds(elapsed):
    """Seconds for one web step, or None when the verifier budget cannot spare it."""
    left = 240 - elapsed
    return None if left < 30 else min(120, left)


VERIFY_PROGRAM = r'''
import hashlib,json,os,shutil,subprocess,time
from pathlib import Path
_web_started=time.monotonic()
def web_build_step_seconds(elapsed):
    left=240-elapsed
    return None if left<30 else min(120,left)
def _web_status(state,reason):
    Path('/work/artifacts/web-preview.json').write_text(json.dumps({'state':state,'reason':reason}))
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
def _web_build(code):
    location=globals().get('VERIFICATION_LOCATION','independent_host_verifier')
    if location=='independent_home_verifier':
        _web_status('unavailable','host_only')
        return
    touched=False
    for line in patch.splitlines():
        if line.startswith(b'diff --git ') and (b' a/web/' in line or b' b/web/' in line):
            touched=True
        elif line.startswith((b'--- a/web/', b'+++ b/web/', b'rename from web/', b'rename to web/')):
            touched=True
    if code!=0 or not touched:
        _web_status('not_applicable',None)
        return
    def pinned(name):
        left,right=Path('/work/source/web')/name,Path('/opt/web-toolchain')/name
        if left.is_symlink() or right.is_symlink() or not left.is_file() or not right.is_file():
            return False
        return left.read_bytes()==right.read_bytes()
    if not (pinned('package.json') and pinned('package-lock.json')):
        _web_status('failed','web_dependencies_changed')
        return
    source,build=Path('/work/source/web'),Path('/work/scratch/web-build')
    if build.exists():
        shutil.rmtree(build)
    def copy_web(origin,dest):
        if origin.is_symlink():
            raise RuntimeError('symlink')
        if origin.is_dir():
            if origin.name in ('node_modules','dist') and origin!=source:
                return
            dest.mkdir(mode=0o700)
            for child in origin.iterdir():
                if child.name in ('node_modules','dist'):
                    continue
                copy_web(child,dest/child.name)
        elif origin.is_file():
            dest.write_bytes(origin.read_bytes())
        else:
            raise RuntimeError('special')
    copy_web(source,build)
    (build/'node_modules').symlink_to('/opt/web-toolchain/node_modules')
    def run_node(script,extra):
        seconds=web_build_step_seconds(time.monotonic()-_web_started)
        if seconds is None:
            _web_status('failed','web_build_budget')
            return 'budget'
        env={'HOME':'/work/scratch','CI':'1','GOMAXPROCS':'2','UV_THREADPOOL_SIZE':'2',
             'NODE_OPTIONS':'--max-old-space-size=512','PATH':'/usr/local/bin:/usr/bin:/bin'}
        try:
            result=subprocess.run(['/usr/local/bin/node',script,*extra],cwd=build,env=env,
                stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=seconds,check=False)
            data=result.stdout or b''
            ok=result.returncode==0
        except subprocess.TimeoutExpired as exc:
            data=exc.stdout or b''
            if isinstance(data,str):
                data=data.encode()
            ok=False
        log=Path('/work/artifacts/web-build.log')
        previous=log.read_bytes() if log.is_file() and not log.is_symlink() else b''
        text=(previous+data)[-65536:].decode('utf-8','replace').replace('\x00','')
        log.write_text(text)
        return 'ok' if ok else 'fail'
    tsc='/opt/web-toolchain/node_modules/typescript/bin/tsc'
    vite='/opt/web-toolchain/node_modules/vite/bin/vite.js'
    step=run_node(tsc,['-b'])
    if step=='budget':
        return
    if step!='ok':
        _web_status('failed','web_typecheck_failed')
        return
    step=run_node(vite,['build','--outDir','/work/artifacts/web-preview','--emptyOutDir'])
    if step=='budget':
        return
    if step!='ok':
        _web_status('failed','web_build_failed')
        return
    fix,index=Path('/work/source/web/preview/fixtures.json'),Path('/work/artifacts/web-preview/index.html')
    if fix.is_symlink() or not fix.is_file() or index.is_symlink() or not index.is_file():
        _web_status('failed','web_build_failed')
        return
    data=fix.read_bytes()
    if len(data)>1048576 or b'\x00' in data:
        _web_status('failed','web_build_failed')
        return
    Path('/work/artifacts/web-fixtures.json').write_bytes(data)
    _web_status('built',None)
try:
    _web_build(code)
except Exception:
    if not Path('/work/artifacts/web-preview.json').exists():
        _web_status('failed','web_build_failed')
raise SystemExit(code)
'''


def verification_program(observed_from="independent_host_verifier"):
    if observed_from not in {"independent_host_verifier", "independent_home_verifier"}:
        raise ValueError("Unsupported verifier location")
    return "VERIFICATION_LOCATION=" + repr(observed_from) + "\n" + VERIFY_PROGRAM
