"""Explicit cloud transport fixture and probes for a disposable real HAOS Runner App.

The installed product modules, Codex binary, TLS routes, source checkout, sandbox
and verifier are real. Only model HTTPS response bytes are supplied by this test.
Never import this helper into a shipped image or enable it through product options.
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import re
import socket
import struct
import threading
import time
from pathlib import Path

FIXTURE = Path('/data/home-worker-fixture')
FIXTURE_KEY = 'fixture-home-role-key-not-a-cloud-account'
DIAGNOSTIC_CLIENT_SECONDS = 15
CANARY = 'fixture-home-private-parent-not-a-real-token'
MODES = frozenset({'source', 'cancel', 'restart', 'crash'})
SHA256 = re.compile(r'^[a-f0-9]{64}$')
HEX40 = re.compile(r'^[a-f0-9]{40}$')
ENTRY_CHECK_NAMES = frozenset({'private_coding_path_absent', 'staging_path_absent', 'private_workspace_fds_absent',
    'inherited_directory_fds_absent', 'effective_capabilities_zero', 'no_new_privileges', 'seccomp_filter_active',
    'worker_fuse_device_unavailable', 'nested_user_namespace_denied'})
ENTRY_BOUNDARY_SOURCE = r'''
def entry_boundary_checks():
    import ctypes,errno,os,stat
    from pathlib import Path
    checks={}
    for name,path in {'private_coding_path_absent':'/data/coding',
        'staging_path_absent':'/opt/agentic-worker/staging'}.items():
        try:os.lstat(path);checks[name]=False
        except FileNotFoundError:checks[name]=True
        except OSError:checks[name]=False
    private_fds=False;directory_fds=False
    for name in os.listdir('/proc/self/fd'):
        if not name.isdecimal() or int(name)<3:continue
        try:
            target=os.readlink('/proc/self/fd/'+name)
            private_fds|=any(prefix in target for prefix in ('/data/coding','/opt/agentic-worker/staging'))
            directory_fds|=stat.S_ISDIR(os.fstat(int(name)).st_mode)
        except OSError as error:
            if error.errno not in {errno.ENOENT,errno.EBADF}:private_fds=True;directory_fds=True
    checks['private_workspace_fds_absent']=not private_fds
    checks['inherited_directory_fds_absent']=not directory_fds
    status=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    checks['effective_capabilities_zero']=status.get('CapEff','').strip()=='0000000000000000'
    checks['no_new_privileges']=status.get('NoNewPrivs','').strip()=='1'
    checks['seccomp_filter_active']=status.get('Seccomp','').strip()=='2'
    library=ctypes.CDLL(None,use_errno=True)
    checks['nested_user_namespace_denied']=library.unshare(ctypes.c_int(0x10000000))==-1 and ctypes.get_errno()==errno.EPERM
    try:
        descriptor=os.open('/dev/fuse',os.O_RDWR|os.O_CLOEXEC|os.O_NOFOLLOW)
        os.close(descriptor);checks['worker_fuse_device_unavailable']=False
    except OSError as error:
        checks['worker_fuse_device_unavailable']=error.errno in {errno.ENOENT,errno.EACCES,errno.EPERM}
    return checks
'''

# This deliberately makes a nonfunctional, reviewable change to the actual
# shipped repository. It proves code/tool/patch/test plumbing, not a real defect
# repair or autonomous research quality. No synthetic repository replaces it.
SOURCE_COMMAND = r'''python - <<'PYCODE'
import hashlib,json,os,socket,urllib.request
from pathlib import Path
''' + ENTRY_BOUNDARY_SOURCE + r'''
p=Path('controller/agentic_controller/models.py')
before=p.read_text()
old='def canonical(value) -> str:\n'
assert old in before
p.write_text(before.replace(old,old+'    """Serialize stable public contract fields for digest comparison."""\n',1))
with urllib.request.urlopen('http://127.0.0.1:8765/observe',timeout=8) as response:
    observation=json.load(response)
assert observation['observed_from']=='haos_app'
worker_namespaces={name+'_ns':os.stat('/proc/self/ns/'+name).st_ino for name in ('pid','cgroup','mnt')}
app_namespaces=observation['content']['app_runtime']['namespace_ids']
checks={'uid_unprivileged':os.getuid()==10000,'supervisor_env_absent':not os.environ.get('SUPERVISOR_TOKEN'),
    'hassio_env_absent':not os.environ.get('HASSIO_TOKEN'),
    'fixture_parent_env_absent':not os.environ.get('AGENTIC_PRIVATE_TEST_CANARY'),
    'docker_socket_absent':not Path('/var/run/docker.sock').exists(),
    'host_socket_absent':not Path('/run/agentic-home/host.sock').exists()}
checks.update(entry_boundary_checks())
for name,value in worker_namespaces.items():
    checks['worker_'+name+'_distinct_from_app']=type(app_namespaces.get(name)) is int and value!=app_namespaces[name]
for name,path in {'role_auth':'/data/coding-auth/profiles/codex/auth.json',
    'runner_key':'/data/private/server.key','private_canary':'/data/home-worker-fixture/private-canary',
    'parent_environ':'/proc/1/environ'}.items():
    try:
        raw=Path(path).read_bytes()
        checks[name+'_secret_unreadable']=b'fixture-home-' not in raw and b'PRIVATE KEY' not in raw
    except OSError:
        checks[name+'_secret_unreadable']=True
for name,address in {'lan':('172.30.32.1',80),'public':('1.1.1.1',443)}.items():
    try:
        socket.create_connection(address,timeout=0.5).close()
        checks[name+'_direct_network_denied']=False
    except OSError:
        checks[name+'_direct_network_denied']=True
checks['tmp_on_bounded_volume']=os.stat('/tmp').st_dev==os.stat('/work/scratch').st_dev
for name,path in {'root':'/','run':'/run','shm':'/dev/shm','dev':'/dev'}.items():
    target=Path(path)
    while not target.exists():target=target.parent
    checks[name+'_outside_volume_readonly']=bool(os.statvfs(target).f_flag & os.ST_RDONLY) and not os.access(target,os.W_OK)
checks['git_metadata_read_only']=not os.access('.git/config',os.W_OK)
Path('/work/artifacts/home-boundaries.json').write_text(json.dumps({'checks':checks,
    'observed_from':'haos_app','observation':observation,'source_patch':'documentation_only_fixture',
    'worker_namespace_ids':worker_namespaces,'app_namespace_ids':app_namespaces}))
assert all(checks.values()),'worker_boundary_probe_failed'
print('HOME_PROBE_SOURCE_AND_BOUNDARIES_OK')
PYCODE
python -m pytest -q tests/test_engine.py tests/test_api.py
'''

HOLD_COMMAND = r'''python - <<'PYCODE'
import json,os,time
from pathlib import Path
child=os.fork()
if child==0:
    os.setsid()
    while True:
        Path('/work/scratch/descendant-pulse').write_text(str(time.monotonic()))
        time.sleep(.1)
Path('/work/artifacts/descendant.json').write_text(json.dumps({'parent_pid':os.getpid(),'child_pid':child}))
print('HOME_PROBE_DESCENDANT_STARTED',flush=True)
while True:time.sleep(.1)
PYCODE
'''


def atomic(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix('.pending')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(value, stream, sort_keys=True)
    os.replace(temporary, path)


def assert_fixture():
    """A private test marker is provided only by the disposable Supervisor build."""
    marker = Path('/opt/home-worker-test-fixture')
    assert os.geteuid() == 0 and marker.is_file(), 'fixture_not_installed'
    assert marker.read_text().strip() == 'derived_model_transport_fixture_v1', 'fixture_identity_invalid'
    assert not Path('/var/run/docker.sock').exists(), 'fixture_must_not_have_docker_socket'
    assert os.environ.get('AGENTIC_HOST_MODE') != '1', 'fixture_must_be_inner_app'


def response_stream(item):
    events = [
        {'type': 'response.created', 'response': {'id': 'resp_home_fixture'}},
        {'type': 'response.output_item.added', 'output_index': 0, 'item': item},
        {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
        {'type': 'response.completed', 'response': {'id': 'resp_home_fixture', 'status': 'completed',
            'output': [item], 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}},
    ]
    return ''.join('event: ' + item['type'] + '\ndata: ' + json.dumps(item) + '\n\n' for item in events).encode()


class ModelFixture:
    """Supply only the provider response; all tool execution remains actual Codex."""
    def __init__(self, root=FIXTURE):
        self.root = root
        self.lock = threading.Lock()

    def __call__(self, host, path, method, body, headers, **kwargs):
        if host != 'api.openai.com' or path != '/v1/responses' or method != 'POST':
            raise AssertionError('fixture_unexpected_provider_route')
        if headers.get('Authorization') != 'Bearer ' + FIXTURE_KEY or FIXTURE_KEY.encode() in body:
            raise AssertionError('fixture_credential_boundary_failed')
        value = json.loads(body)
        if not isinstance(value.get('input'), list):
            raise AssertionError('fixture_provider_input_invalid')
        mode = (self.root / 'mode').read_text().strip()
        if mode not in MODES:
            raise AssertionError('fixture_mode_invalid')
        outputs = [item for item in value['input'] if isinstance(item, dict) and item.get('type') == 'function_call_output']
        with self.lock:
            receipt = self.root / 'transport.json'
            summary = json.loads(receipt.read_text()) if receipt.exists() else {}
            counts = summary.setdefault(mode, {'requests': 0, 'tool_output_received': False, 'source_marker': False,
                                              'pytest_passed': False})
            counts['requests'] += 1
            if outputs:
                text = json.dumps(outputs)
                counts.update(tool_output_received=True, source_marker=counts['source_marker'] or 'HOME_PROBE_SOURCE_AND_BOUNDARIES_OK' in text,
                              pytest_passed=counts['pytest_passed'] or ' passed' in text and 'failed' not in text)
            atomic(receipt, summary)
        if not outputs:
            item = {'type': 'function_call', 'id': 'fc_home_fixture', 'call_id': 'call_home_fixture',
                'name': 'exec_command', 'namespace': 'functions', 'status': 'completed',
                'arguments': json.dumps({'cmd': SOURCE_COMMAND if mode == 'source' else HOLD_COMMAND,
                                         'max_output_tokens': 2000, 'yield_time_ms': 1000})}
        elif mode != 'source' or not counts['source_marker'] or not counts['pytest_passed']:
            sessions = re.findall(r'(?:session ID|session_id)[^0-9]{0,20}([0-9]+)', json.dumps(outputs), re.I)
            if not sessions:
                raise AssertionError('fixture_tool_session_or_success_missing')
            item = {'type': 'function_call', 'id': 'fc_home_wait_' + str(counts['requests']), 'call_id': 'call_home_wait_' + str(counts['requests']),
                'name': 'write_stdin', 'namespace': 'functions', 'status': 'completed',
                'arguments': json.dumps({'session_id': int(sessions[-1]), 'chars': '', 'yield_time_ms': 1000,
                                         'max_output_tokens': 2000})}
        else:
            item = {'id': 'msg_home_fixture', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': 'Fixture source change and tool execution complete. No deployment.',
                             'annotations': []}]}
        return 200, 'text/event-stream', response_stream(item)


def stderr_summary_bytes(raw):
    """Only fixed booleans leave memory; never expose private stderr prose."""
    text = raw[:16384].decode(errors='replace').lower()
    return {'available': True, 'scanned_bytes': min(len(raw), 16384), 'truncated': len(raw) > 16384,
        **{name: phrase in text for name, phrase in {
            'bubblewrap': 'bwrap:', 'permission_denied': 'permission denied',
            'operation_not_permitted': 'operation not permitted', 'pivot_root': 'pivot_root',
            'mount': 'mount', 'namespace': 'namespace', 'missing_file': 'no such file',
            'python_exception': 'traceback (most recent call last)', 'no_space': 'no space left',
            'missing_module': 'modulenotfounderror', 'pytest_collection': 'error collecting',
            'root_mount_propagation': 'make / slave', 'proc_mount': 'mount proc',
            'loopback_setup': 'loopback', 'private_fd_lookup': '/proc/self/fd/',
        }.items()}}


def stderr_summary(path):
    """Classify bounded bytes without exposing commands, paths, tokens or logs."""
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(16385)
    except OSError:
        return {'available': False}
    return stderr_summary_bytes(raw)


def failure_receipt(error, *, source_files=()):
    # Only literal assertion codes in trusted fixture source files are public.
    codes = set()
    for path in (Path(__file__), *source_files):
        for node in ast.walk(ast.parse(Path(path).read_text())):
            if isinstance(node, ast.Assert) and isinstance(node.msg, ast.Constant):
                value = node.msg.value
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == 'AssertionError' and node.args and isinstance(node.args[0], ast.Constant)):
                value = node.args[0].value
            else:
                continue
            if isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9_]{2,100}', value):
                codes.add(value)
    value = str(error).split('\n', 1)[0]
    dynamic = re.fullmatch(r'(?:fixture_command_failed_(?:ssh|docker|ssh-keygen|mkfs\.ext4)_[1-9][0-9]{0,2}|controller_(?:get|post)_status_[1-5][0-9]{2})', value)
    kinds = {'AssertionError', 'BoundaryError', 'SandboxError', 'OSError', 'PermissionError', 'FileNotFoundError',
             'ModuleNotFoundError', 'ImportError', 'KeyError', 'ValueError', 'TimeoutError', 'TimeoutExpired',
             'HAError', 'CancelledError', 'KeyboardInterrupt'}
    result = {'error_type': type(error).__name__ if type(error).__name__ in kinds else 'Exception',
            'error_code': value if value in codes or dynamic else 'unclassified_fixture_error',
            'errno': error.errno if isinstance(error, OSError) and type(error.errno) is int and 0 < error.errno < 4096 else None}
    from agentic_host.coding_sandbox import ERROR_CODES as SANDBOX_CODES
    from agentic_host.coding_sandbox import SandboxError
    from agentic_runner.home_launcher import ERROR_CODES as LAUNCHER_CODES
    from agentic_runner.home_launcher import BoundaryError
    if isinstance(error, SandboxError):
        result['error_code'] = error.code if error.code in SANDBOX_CODES else 'sandbox_rejected'
        result.update(command_diagnostics(error.diagnostics))
    elif isinstance(error, BoundaryError):
        result['error_code'] = value if value in LAUNCHER_CODES else 'home_launcher_failure'
    return result


def command_diagnostics(raw):
    from agentic_host.coding_sandbox import OPERATIONS
    allowed = set(OPERATIONS) | {name + '.' + action for name, actions in OPERATIONS.items() for action in actions}
    result = {}
    if isinstance(raw.get('operation'), str) and raw['operation'] in allowed:
        result['operation'] = raw['operation']
    for key, low, high in (('command_exit_code', -128, 255), ('timeout_seconds', 1, 900)):
        if type(raw.get(key)) is int and low <= raw[key] <= high:
            result[key] = raw[key]
    return result


def launcher_receipt(raw):
    """Project a launch receipt without publishing messages, arguments or paths."""
    from agentic_host.coding_sandbox import ERROR_CODES as SANDBOX_CODES
    from agentic_runner.home_launcher import ERROR_CODES
    if not isinstance(raw, dict):
        return {'available': False, 'read_status': 'invalid'}
    code = raw.get('error_code')
    mode, kind = raw.get('mode'), raw.get('error_type')
    return {'available': True,
            'mode': mode if isinstance(mode, str) and mode in {'prepare', 'execute', 'supervise', 'fuse'} else 'unknown',
            'error_code': code if isinstance(code, str) and code in ERROR_CODES | SANDBOX_CODES else 'home_launcher_failure',
            'error_type': kind if isinstance(kind, str) and kind in {'SandboxError', 'BoundaryError', 'OSError', 'Exception'} else 'Exception',
            'errno': raw.get('errno') if type(raw.get('errno')) is int and 0 < raw['errno'] < 4096 else None,
            **command_diagnostics(raw)}


def launcher_diagnostic(path):
    try:
        path = Path(path)
        if path.stat().st_size > 4096:
            return {'available': False, 'read_status': 'oversize'}
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return {'available': False, 'read_status': 'missing'}
    except OSError:
        return {'available': False, 'read_status': 'unreadable'}
    except ValueError:
        return {'available': False, 'read_status': 'invalid'}
    return launcher_receipt(raw)


async def diagnostic_request(reader, writer):
    """Fixture-only readback in the initialized App namespace; never setns."""
    try:
        peer = writer.get_extra_info('socket').getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        if struct.unpack('3i', peer)[1] != 0:
            return
        request = await asyncio.wait_for(reader.readline(), timeout=2)
        match = re.fullmatch(rb'job-proof ([a-f0-9]{32})\n', request)
        if match is None:
            return
        result = job_proof(match.group(1).decode('ascii'))
        result['diagnostic_namespace_ids'] = {name + '_ns': os.stat('/proc/self/ns/' + name).st_ino for name in ('pid', 'cgroup', 'mnt')}
        raw = json.dumps(result).encode()
        if len(raw) <= 65536:
            writer.write(raw)
            await writer.drain()
    except (OSError, ValueError, TimeoutError, AssertionError):
        pass
    finally:
        writer.close()
        await writer.wait_closed()


def diagnostic_client(identifier, *, clock=time.monotonic):
    assert re.fullmatch(r'[a-f0-9]{32}', identifier), 'invalid_fixture_job_id'
    deadline = clock() + DIAGNOSTIC_CLIENT_SECONDS

    def remaining():
        value = deadline - clock()
        if value <= 0:
            raise TimeoutError('diagnostic_read_deadline')
        return value

    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(remaining())
        client.connect(str(FIXTURE / 'diagnostic.sock'))
        client.settimeout(remaining())
        client.sendall(('job-proof ' + identifier + '\n').encode())
        chunks = bytearray()
        while len(chunks) <= 65536:
            client.settimeout(remaining())
            piece = client.recv(min(4096, 65537 - len(chunks)))
            if not piece:
                break
            chunks.extend(piece)
        assert len(chunks) <= 65536, 'diagnostic_response_limit'
        return json.loads(chunks)


def namespace_snapshot():
    process_fields = Path('/proc/self/stat').read_text().rsplit(') ', 1)[1].split()
    result = {'observed_from': 'haos_app', 'uid': os.geteuid(),
        'process_id': os.getpid(), 'process_start_ticks': int(process_fields[19]),
        'supervisor_token_present': bool(os.environ.get('SUPERVISOR_TOKEN')),
        'docker_socket_present': Path('/var/run/docker.sock').exists(),
        'host_socket_present': Path('/run/agentic-home/host.sock').exists(),
        'fuse_device_present': Path('/dev/fuse').exists(),
        'namespaces': {name: os.stat('/proc/self/ns/' + name).st_ino for name in ('pid', 'cgroup', 'mnt', 'net')},
        'boot_id_sha256': hashlib.sha256(Path('/proc/sys/kernel/random/boot_id').read_bytes()).hexdigest()}
    from agentic_runner.home_launcher import BoundaryError, validate_fuse_device
    try:
        validate_fuse_device()
        result['fuse_device_openable'] = True
    except BoundaryError:
        result['fuse_device_openable'] = False
    try:
        profile = Path('/proc/self/attr/current').read_text().strip()
        result['apparmor_enforcing'] = profile.endswith(' (enforce)') and 'unconfined' not in profile
    except OSError:
        result['apparmor_enforcing'] = False
    return result


async def serve_fixture():
    assert_fixture()
    os.umask(0o077)
    FIXTURE.mkdir(mode=0o700, exist_ok=True)
    if not (FIXTURE / 'mode').exists():
        (FIXTURE / 'mode').write_text('source')
    (FIXTURE / 'private-canary').write_text(CANARY)
    (FIXTURE / 'private-canary').chmod(0o600)
    os.environ['AGENTIC_PRIVATE_TEST_CANARY'] = CANARY
    # No real credential is supplied by this harness. Normal Runner startup
    # removes Supervisor credentials before untrusted CLI work can begin.
    os.environ['SUPERVISOR_TOKEN'] = CANARY
    os.environ['HASSIO_TOKEN'] = CANARY
    from agentic_runner import app, home_service
    from agentic_runner.home_launcher import ERROR_CODES, initialize_home_runtime
    try:
        initialize_home_runtime()
        if not (FIXTURE / 'boundaries.json').exists():
            atomic(FIXTURE / 'boundaries.json', boundary_probes())
    except Exception as error:
        known = {'home_app_root_required', 'home_private_record_invalid', 'home_private_root_required',
            'home_private_ancestor_required', 'home_init_before_threads_required', 'home_private_app_cgroup_required',
            'home_cgroup_controllers_unavailable', 'home_fuse_device_required', 'home_unshare_unavailable',
            'home_mount_unavailable', 'home_cgroup_delegation_unavailable', 'home_runtime_identity_changed'} | ERROR_CODES
        atomic(FIXTURE / 'startup.json', {'state': 'failed', 'phase': 'initialize_home_runtime',
            'error_type': type(error).__name__, 'error_code': str(error) if str(error) in known else 'unclassified',
            'errno': error.errno if isinstance(error, OSError) and type(error.errno) is int else None})
        raise
    atomic(FIXTURE / 'startup.json', {'state': 'ready'})
    original_relay = home_service.HomeRelay

    class FixtureRelay(original_relay):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs, transport=ModelFixture())

    original_broker = app.Broker

    class PairCapture(original_broker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            atomic(FIXTURE / 'pairing.json', {'fingerprint': self.fingerprint, 'code': self.code})
            atomic(FIXTURE / 'app-observation.json', namespace_snapshot())

    home_service.HomeRelay = FixtureRelay
    app.Broker = PairCapture
    diagnostic_path = FIXTURE / 'diagnostic.sock'
    diagnostic_path.unlink(missing_ok=True)
    server = await asyncio.start_unix_server(diagnostic_request, path=diagnostic_path, limit=128)
    diagnostic_path.chmod(0o600)
    try:
        await app.serve()
    finally:
        server.close()
        await server.wait_closed()
        diagnostic_path.unlink(missing_ok=True)


QUOTA_PROGRAM = ENTRY_BOUNDARY_SOURCE + r'''
import errno,json,os
from pathlib import Path
entry_checks=entry_boundary_checks()
Path('/work/artifacts/entry-boundaries.json').write_text(json.dumps(entry_checks))
assert all(entry_checks.values()),'entry_boundary_failed'
root=Path('/work/scratch');written=0;error=None
try:
 for n in range(200):
  with (root/('fill-'+str(n))).open('wb',buffering=0) as stream:
   for _ in range(8):written+=stream.write(b'x'*1024**2)
except OSError as exc:error=exc.errno
files=list(root.glob('fill-*'))
for p in files:p.unlink()
assert error==errno.ENOSPC and written>16*1024**2,'hard_quota_not_enforced'
space=os.statvfs('/work')
Path('/work/artifacts/quota.json').write_text(json.dumps({'errno':error,'written_bytes':written,
 'capacity_bytes':space.f_blocks*space.f_frsize,'tmp_same_volume':os.stat('/tmp').st_dev==os.stat('/work').st_dev}))
'''
PID_CPU_PROGRAM = r'''
import errno,json,os,time
from pathlib import Path
children=[];error=None
try:
 for _ in range(32):
  pid=os.fork()
  if pid==0:
   end=time.monotonic()+4
   while time.monotonic()<end:pass
   os._exit(0)
  children.append(pid)
except OSError as exc:error=exc.errno
for pid in children:os.waitpid(pid,0)
assert error==errno.EAGAIN and 1<=len(children)<16,'pid_quota_not_enforced'
Path('/work/artifacts/pids.json').write_text(json.dumps({'errno':error,'children_started':len(children)}))
'''
MEMORY_PROGRAM = "x=bytearray(384*1024*1024)\n"
DESCENDANT_PROGRAM = HOLD_COMMAND.split("python - <<'PYCODE'\n", 1)[1].split('\nPYCODE', 1)[0]


def boundary_probes():
    """Actual HomeSandbox programs, before service threads in the same App context."""
    import shutil

    from agentic_host.coding_sandbox import Limits
    from agentic_runner import home_launcher as launcher
    from agentic_runner.home_sandbox import HomeSandbox

    result = {'status': 'running', 'observed_from': 'haos_app', 'checks': {}}
    root = Path('/data/coding/work')
    source = Path('/opt/agentic-worker/source')
    lock = json.loads(source.with_suffix('.json').read_text())
    revision = lock['commit']
    assert HEX40.fullmatch(revision), 'source_revision_invalid'
    sandbox = HomeSandbox(root, None, {'agentic-home': source},
        limits=Limits(disk_mib=256, memory_mib=128, pids=16, cpus=1, wall_seconds=45))
    result['runtime'] = launcher.runtime_observation()
    result['source_commit'] = revision
    phase = 'preflight'
    owned = []
    try:
        sandbox.preflight()
        for name, program in (('quota', QUOTA_PROGRAM), ('pid-cpu', PID_CPU_PROGRAM), ('memory', MEMORY_PROGRAM),
                              ('descendant', DESCENDANT_PROGRAM)):
            phase = name
            identifier = 'boundary-' + name + '-fixture'
            owned.append(identifier)
            sandbox.launch(identifier, 1, 'agentic-home', revision, program)
            deadline = time.monotonic() + 40
            cancelled_after_pulse = False
            while time.monotonic() < deadline:
                job = sandbox.status(identifier)
                pulse = root / identifier / 'workspace/scratch/descendant-pulse'
                if name == 'descendant' and pulse.exists():
                    started = time.monotonic()
                    job = sandbox.cancel(identifier)
                    before = pulse.read_bytes()
                    time.sleep(.2)
                    assert pulse.read_bytes() == before, 'descendant_writer_survived_cancel'
                    result['cancel_seconds'] = round(time.monotonic() - started, 3)
                    cancelled_after_pulse = True
                    break
                if job.get('stop_confirmed'):
                    break
                time.sleep(.05)
            assert job.get('stop_confirmed') and not job.get('cgroup_populated'), 'boundary_writer_stop_unconfirmed'
            counters = job.get('resource_counters', {})
            result[name + '_counters'] = counters
            result['last_job'] = {key: job.get(key) for key in ('state', 'stop_confirmed', 'exit_code', 'oom_killed', 'cgroup_populated')}
            if name == 'memory':
                assert job['state'] == 'failed' and job.get('oom_killed') and counters['memory.events']['oom_kill'] > 0, 'memory_limit_not_enforced'
                result['checks']['actual_memory_oom_kill'] = True
            elif name == 'descendant':
                assert cancelled_after_pulse and job['state'] == 'cancelled', 'descendant_cancel_not_tested'
                result['checks']['descendant_cancel_and_cgroup_empty'] = True
            else:
                assert job['state'] == 'succeeded', 'boundary_program_failed'
                entries = {entry['name']: json.loads(entry['content']) for entry in sandbox.artifacts(identifier)}
                if name == 'quota':
                    result['entry_boundary_checks'] = {key: value for key, value in entries['entry-boundaries.json'].items()
                        if key in ENTRY_CHECK_NAMES and type(value) is bool}
                    assert set(result['entry_boundary_checks']) == ENTRY_CHECK_NAMES and all(result['entry_boundary_checks'].values()), 'entry_boundary_failed'
                    quota = entries['quota.json']
                    result['quota'] = quota
                    assert quota['errno'] == 28 and quota['capacity_bytes'] <= 256 * 1024**2 and quota['tmp_same_volume']
                    result['quota'] = quota
                    result['checks']['actual_ext4_enospc'] = True
                else:
                    result['pids'] = entries['pids.json']
                    assert entries['pids.json']['errno'] == 11 and counters['pids.events']['max'] > 0
                    assert counters['cpu.stat']['nr_throttled'] > 0 and counters['cpu.stat']['throttled_usec'] > 0
                    result['pids'] = entries['pids.json']
                    result['checks'].update(actual_pid_limit=True, actual_cpu_throttling=True)
            result[name + '_counters'] = counters
            stopped = sandbox.dispose(identifier)
            assert stopped['state'] == 'disposed' and stopped['stop_confirmed']
            record = sandbox.record(identifier)
            assert not os.path.ismount(root / identifier / 'workspace'), 'fuse_mount_not_removed'
            assert all(not (launcher.ROOT / 'cgroups' / record[key]['name']).exists() for key in ('cgroup', 'fuse_cgroup'))
            shutil.rmtree(root / identifier)
        result.update(status='passed', cleanup='passed')
        result['checks']['worker_and_fuse_cgroups_reclaimed'] = True
    except Exception as error:
        result.update(status='failed', phase=phase, **failure_receipt(error))
        if owned:
            result['stderr_summary'] = stderr_summary(root / owned[-1] / 'workspace/stderr.txt')
            result['launcher_diagnostic'] = launcher_diagnostic(root / owned[-1] / 'launch-error.json')
            receipt = root / owned[-1] / 'workspace/artifacts/entry-boundaries.json'
            if receipt.is_file() and receipt.stat().st_size <= 4096:
                result['entry_boundary_checks'] = {key: value for key, value in json.loads(receipt.read_text()).items()
                    if key in ENTRY_CHECK_NAMES and type(value) is bool}
        # Retain the fixed failure receipt before any cleanup attempt.
        atomic(FIXTURE / 'boundaries.json', result)
        cleanup = True
        for identifier in owned:
            try:
                if sandbox.dispose(identifier).get('stop_confirmed'):
                    shutil.rmtree(root / identifier, ignore_errors=True)
                else:
                    cleanup = False
            except Exception:
                cleanup = False
        result['cleanup'] = 'passed' if cleanup else 'unconfirmed'
    return result


def job_proof(identifier, *, root=Path('/data/coding/work')):
    """Read-only App observation, including incomplete allocation and failed reads.

    Missing cgroup records are unknown, never evidence that a writer stopped.
    The optional root supports disk-only unit fixtures; the RPC always uses the
    fixed product root and cannot accept a caller-supplied path.
    """
    assert re.fullmatch(r'[a-f0-9]{32}', identifier), 'invalid_fixture_job_id'
    root = Path(root)
    directory = root / identifier
    result = {'launcher_diagnostic': launcher_diagnostic(directory / 'launch-error.json')}
    path = root / (identifier + '.json')
    try:
        result['record_present'] = path.is_file()
        if not result['record_present']:
            return result
        assert path.stat().st_size <= 65536, 'job_record_limit'
        value = json.loads(path.read_text())
        assert isinstance(value, dict), 'job_record_invalid'
    except (OSError, ValueError, AssertionError) as error:
        result['record_observation_error'] = failure_receipt(error)
        return result
    result.update(pulse_present=(directory / 'workspace/scratch/descendant-pulse').is_file(),
        workspace_mounted=os.path.ismount(directory / 'workspace'),
        workspace_image_present=(directory / 'workspace.ext4').is_file())
    if value.get('state') in {'preparing','running','succeeded','failed','cancelled','interrupted','disposed','stop_unconfirmed'}:
        result['state'] = value['state']
    for key in ('stop_confirmed', 'cleanup_fenced', 'previous_boot_stopped'):
        if type(value.get(key)) is bool:
            result[key] = value[key]
    if isinstance(value.get('launch_failure'), dict):
        result['launch_failure'] = launcher_receipt(value['launch_failure'])
    for key in ('cgroup', 'fuse_cgroup'):
        recorded = key in value
        result[key + '_recorded'] = recorded
        if not recorded:
            continue
        raw = value[key]
        name = raw.get('name') if isinstance(raw, dict) else None
        if not isinstance(name, str) or re.fullmatch(r'[wf]-[a-f0-9]{32}', name) is None:
            result[key + '_observation_error'] = {'error_code': 'invalid_fixture_cgroup'}
            continue
        group = root.parent / 'cgroups' / name
        try:
            result[key + '_present'] = group.exists()
            if result[key + '_present']:
                events = dict(line.split() for line in (group / 'cgroup.events').read_text().splitlines())
                if events.get('populated') in {'0', '1'}:
                    result[key + '_populated'] = events['populated'] == '1'
                else:
                    raise AssertionError('fixture_cgroup_events_invalid')
        except (OSError, ValueError, AssertionError) as error:
            result[key + '_observation_error'] = failure_receipt(error)
    result['stderr_summary'] = stderr_summary(directory / 'workspace/stderr.txt')
    return result


def main():
    import sys
    if sys.argv[1:] == ['serve']:
        asyncio.run(serve_fixture())
    elif len(sys.argv) == 3 and sys.argv[1] == 'job-proof':
        assert_fixture()
        print(json.dumps(diagnostic_client(sys.argv[2])))
    elif sys.argv[1:] == ['snapshot']:
        assert_fixture()
        print(json.dumps(namespace_snapshot()))
    else:
        raise SystemExit('unknown_fixed_probe')


if __name__ == '__main__':
    main()
