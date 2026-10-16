"""Run via sudo python3 - only inside disposable installed ISO test VM; no accounts."""
import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

STATE = Path('/var/lib/agentic-home')
PRODUCT = Path('/opt/agentic-home')
COMMAND_TIMEOUT_SECONDS = 60
# verify() shortens the verifier wall to the time left before expires; keep
# wait >= setup + execution + verification + shutdown budgets and expires > wait.
JOB_WAIT_SECONDS = 1800
JOB_EXPIRES_SECONDS = 2400
CLEANUP_WAIT_SECONDS = 60
DIAGNOSTIC_EVENT_LIMIT = 64
DIAGNOSTIC_BYTES = 16384
FIXTURE_JOB = 'a' * 32
JOB_STATES = {'queued', 'launching', 'running', 'stop_unconfirmed', 'succeeded', 'failed',
              'cancelled', 'expired', 'interrupted'}
JOB_ERRORS = {'writer_stop_unconfirmed', 'coding_setup_failed', 'coding_execution_failed',
    'setup_timeout', 'execution_timeout', 'verification_timeout', 'authorization_expired',
    'owner_cancelled', 'broker_restarted', 'checkpoint_export_failed', 'coding_needs_reauth',
    'coding_quota_reached', 'coding_turn_limit', 'coding_provider_rejected', 'coding_provider_unavailable',
    'patch_too_large', 'patch_not_text', 'retention_workspace_cleanup_pending'}


def number(value, low=-128, high=255):
    return value if type(value) is int and low <= value <= high else None


def signatures(value):
    """Only fixed flags from the newest bounded bytes can leave this process."""
    raw = value.encode(errors='replace') if isinstance(value, str) else value if isinstance(value, bytes) else b''
    text = raw[-DIAGNOSTIC_BYTES:].decode(errors='replace').lower()
    return {'scanned_bytes': min(len(raw), DIAGNOSTIC_BYTES), 'truncated': len(raw) > DIAGNOSTIC_BYTES,
        **{key: any(phrase in text for phrase in phrases) for key, phrases in {
            'permission_denied': ('permission denied', 'operation not permitted'),
            'import_error': ('modulenotfounderror', 'importerror'),
            'traceback': ('traceback (most recent call last)',),
            'timeout': ('timed out', 'timeout'), 'disk_full': ('no space left on device',),
            'io_error': ('input/output error',), 'oom': ('out of memory', 'oom-kill'),
            'docker_unavailable': ('cannot connect to the docker daemon', 'error during connect'),
            'container_missing': ('no such object', 'no such container'),
            'file_missing': ('no such file or directory',),
            'security_denied': ('apparmor="denied"', 'seccomp',),
        }.items()}}


def error_receipt(error, sandbox_codes):
    kinds = {'SandboxError', 'OSError', 'PermissionError', 'FileNotFoundError', 'TimeoutExpired',
             'ValueError', 'TypeError', 'KeyError', 'IndexError', 'AssertionError'}
    code = getattr(error, 'code', None)
    result = {'error_type': type(error).__name__ if type(error).__name__ in kinds else 'OtherError',
        'error_code': code if isinstance(code, str) and code in sandbox_codes else 'unclassified',
        'errno': number(getattr(error, 'errno', None), 1, 4095)}
    # These three literal product errors otherwise collapse to sandbox_rejected.
    reason = {'Container stop cannot be confirmed': 'container_inspection_failed',
              'Container identity mismatch': 'container_label_mismatch',
              'Container was replaced': 'container_id_mismatch'}.get(str(error))
    if reason:
        result['inspection_reason'] = reason
    diagnostic = getattr(error, 'diagnostics', {})
    if isinstance(diagnostic, dict):
        result['command_exit_code'] = number(diagnostic.get('command_exit_code'))
        result['timeout_seconds'] = number(diagnostic.get('timeout_seconds'), 1, 900)
        missing = diagnostic.get('missing_capabilities')
        if isinstance(missing, list):
            result['missing_capabilities'] = [name for name in ('MemoryLimit', 'SwapLimit', 'PidsLimit',
                'CpuCfsQuota', 'CgroupVersion', 'seccomp', 'apparmor') if name in missing]
    return result


def job_receipt(job, sandbox_codes=()):
    job = job if isinstance(job, dict) else {}
    state, phase, code = job.get('state'), job.get('phase'), job.get('error_code')
    result = {'state': state if isinstance(state, str) and state in JOB_STATES else 'unknown',
        'phase': phase if isinstance(phase, str) and phase in JOB_STATES | {'collecting', 'verifying'} else 'unknown',
        'error_code': code if code is None or isinstance(code, str) and code in JOB_ERRORS | set(sandbox_codes) else 'unclassified',
        'exit_code': number(job.get('exit_code'))}
    for key in ('stop_confirmed', 'execution_started', 'workspace_released', 'sandbox_attempted', 'results_collected'):
        if type(job.get(key)) is bool:
            result[key] = job[key]
    verification = job.get('verification')
    result['verification'] = None
    if isinstance(verification, dict):
        value = verification.get('state')
        result['verification'] = {'state': value if isinstance(value, str) and value in
            {'running', 'passed', 'failed', 'not_run'} else 'unknown'}
        target = result['verification']
        for key in ('patch_sha256', 'test_command_sha256'):
            value = verification.get(key)
            if isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value):
                target[key] = value
        for key, allowed in {'observed_from': {'independent_host_verifier', 'independent_home_verifier'},
                             'trusted_tests': {'pinned_baseline'}, 'network': {'none'}}.items():
            value = verification.get(key)
            if isinstance(value, str) and value in allowed:
                target[key] = value
        for key, maximum in (('exit_code', 255), ('tests_executed', 1_000_000)):
            value = number(verification.get(key), -128 if key == 'exit_code' else 0, maximum)
            if value is not None:
                target[key] = value
        if type(verification.get('stop_confirmed')) is bool:
            target['stop_confirmed'] = verification['stop_confirmed']
        preview = verification.get('web_preview')
        if isinstance(preview, dict) and preview.get('state') == 'not_applicable':
            target['web_preview'] = {'state': 'not_applicable'}
    return result


def bounded_file(root, job_id, relative):
    """Read only fixed fixture files; reject links/devices and retain no raw bytes."""
    descriptors = []
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        current = os.open(root, flags | os.O_DIRECTORY)
        descriptors.append(current)
        parts = (job_id, 'workspace', *relative.split('/'))
        for part in parts[:-1]:
            current = os.open(part, flags | os.O_DIRECTORY, dir_fd=current)
            descriptors.append(current)
        descriptor = os.open(parts[-1], flags, dir_fd=current)
        descriptors.append(descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return {'read_status': 'not_regular'}
        os.lseek(descriptor, max(0, info.st_size - DIAGNOSTIC_BYTES), os.SEEK_SET)
        raw = os.read(descriptor, DIAGNOSTIC_BYTES)
        result = {'read_status': 'ok', 'bytes': number(info.st_size, 0, 2**40),
                  **signatures(raw), 'truncated': info.st_size > DIAGNOSTIC_BYTES}
        if relative == 'artifacts/execution.json' and info.st_size <= 4096:
            try:
                value = json.loads(raw)
                if isinstance(value, dict):
                    result['cli_exit_code'] = number(value.get('cli_exit_code'))
            except (ValueError, TypeError):
                result['json_invalid'] = True
        return result
    except FileNotFoundError:
        return {'read_status': 'missing'}
    except OSError:
        return {'read_status': 'unreadable'}
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


class CodingDiagnostics:
    """Fixture-only bounded receipts, independent of product cleanup and verdict."""
    def __init__(self, *, clock=time.monotonic):
        self.clock, self.started = clock, clock()
        self.lock = threading.Lock()
        self.events, self.first_failures, self.jobs, self.workspaces = [], {}, [], {}
        self.last_job, self.dropped, self.job_dropped, self.observer_errors = None, 0, 0, 0
        self.sandbox_codes = set()

    def safe(self, function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            self.observer_errors = min(self.observer_errors + 1, 1_000_000)
            return None

    def event(self, role, operation, *, started=None, failure=False, **fields):
        now = self.clock()
        row = {'role': role, 'operation': operation, 'at_seconds': round(max(0, now - self.started), 3), **fields}
        if started is not None:
            row['duration_seconds'] = round(max(0, now - started), 3)
        with self.lock:
            self.events.append(row)
            if len(self.events) > DIAGNOSTIC_EVENT_LIMIT:
                self.events.pop(0)
                self.dropped += 1
            if failure and len(self.first_failures) < 32:
                self.first_failures.setdefault(role + ':' + operation, row)

    def job(self, raw):
        value = job_receipt(raw, self.sandbox_codes)
        with self.lock:
            if value == self.last_job:
                return
            self.last_job = value
            row = {'at_seconds': round(max(0, self.clock() - self.started), 3), **value}
            if len(self.jobs) < 32:
                self.jobs.append(row)
            else:
                self.job_dropped += 1
            if 'job' not in self.first_failures and len(self.first_failures) < 32 and (
                    value.get('state') == 'stop_unconfirmed' or value.get('error_code')):
                self.first_failures.setdefault('job', row)

    def workspace(self, root, job_id, role):
        if job_id not in {FIXTURE_JOB, FIXTURE_JOB + '-verify'}:
            return
        value = {name: bounded_file(root, job_id, path) for name, path in {
            'stdout': 'stdout.txt', 'stderr': 'stderr.txt', 'cli_events': 'scratch/events.jsonl',
            'execution': 'artifacts/execution.json'}.items()}
        with self.lock:
            # A later already-disposed call must not erase an earlier readable log.
            previous = self.workspaces.setdefault(role, {})
            for name, item in value.items():
                if previous.get(name, {}).get('read_status') != 'ok':
                    previous[name] = item

    def snapshot(self, relay_count):
        with self.lock:
            return json.loads(json.dumps({'events': self.events, 'events_dropped': self.dropped,
                'first_failures': self.first_failures, 'job_transitions': self.jobs,
                'job_transitions_dropped': self.job_dropped,
                'last_job': self.last_job, 'pre_disposal_files': self.workspaces,
                'relay_requests': number(relay_count, 0, 1_000_000), 'observer_errors': self.observer_errors}))


def observed_sandbox(base, diagnostics, command, sandbox_codes):
    """Instrument existing calls only; preserve returns, exceptions and command count."""
    diagnostics.sandbox_codes = set(sandbox_codes)
    operations = {'docker': {'info', 'create', 'start', 'inspect', 'kill', 'wait', 'rm', 'version', 'ps'},
                  'git': {'clone', 'checkout', 'remote'}, 'findmnt': set(), 'mkfs.ext4': set(),
                  'mount': set(), 'umount': set()}

    class ObservedSandbox(base):
        def __init__(self, *args, **kwargs):
            self.observation_context = threading.local()
            delegate = kwargs.pop('execute', command)
            def execute(*argv, **options):
                role = getattr(self.observation_context, 'role', 'preflight')
                executable = Path(argv[0]).name
                action = next((arg for arg in argv[1:] if arg in operations.get(executable, ())), None)
                operation = executable + ('.' + action if action else '') if executable in operations else 'other'
                started = diagnostics.safe(diagnostics.clock)
                try:
                    result = delegate(*argv, **options)
                except BaseException as error:
                    diagnostics.safe(lambda error=error: diagnostics.event(role, operation, started=started, failure=True,
                        error=error_receipt(error, sandbox_codes)))
                    raise
                diagnostics.safe(lambda: diagnostics.event(role, operation, started=started,
                    failure=result.returncode != 0, returncode=number(result.returncode),
                    stderr=signatures(result.stderr)))
                return result
            super().__init__(*args, execute=execute, **kwargs)

        def observed(self, operation, job_id, call, *args, **kwargs):
            previous = getattr(self.observation_context, 'role', 'preflight')
            role = ('writer' if job_id == FIXTURE_JOB else 'verifier' if job_id == FIXTURE_JOB + '-verify'
                    else previous if job_id is None else 'unknown')
            self.observation_context.role = role
            started = diagnostics.safe(diagnostics.clock)
            try:
                result = call(*args, **kwargs)
            except BaseException as error:
                diagnostics.safe(lambda error=error: diagnostics.event(role, operation, started=started, failure=True,
                    error=error_receipt(error, sandbox_codes)))
                raise
            else:
                def record():
                    fields = {}
                    if operation == 'inspect':
                        job = args[0]
                        state = result.get('State', {})
                        fields['container'] = {key: state[key] for key in ('Running', 'Paused', 'OOMKilled', 'Dead')
                            if type(state.get(key)) is bool}
                        value = state.get('Status')
                        fields['container']['status'] = value if value in {'created', 'running', 'paused', 'restarting', 'removing', 'exited', 'dead'} else 'unknown'
                        fields['container']['exit_code'] = number(state.get('ExitCode'))
                        fields['identity'] = {
                            'label_matches': result.get('Config', {}).get('Labels', {}).get('agentic.coding') == job.get('container'),
                            'container_id_matches': result.get('Id') == job.get('container_id') if job.get('container_id') else None,
                            'image_matches': result.get('Image') == self.image}
                        fields['container']['error_signatures'] = signatures(state.get('Error'))
                    elif isinstance(result, dict):
                        state = result.get('state')
                        fields['state'] = state if isinstance(state, str) and state in JOB_STATES | {'preparing', 'disposed'} else 'unknown'
                        fields['exit_code'] = number(result.get('exit_code'))
                        fields['lease_matches'] = result.get('lease_epoch') == 1
                    diagnostics.event(role, operation, started=started,
                        failure=fields.get('state') == 'stop_unconfirmed', **fields)
                diagnostics.safe(record)
                return result
            finally:
                self.observation_context.role = previous

        def launch(self, job_id, *args, **kwargs):
            return self.observed('launch', job_id, super().launch, job_id, *args, **kwargs)

        def preflight(self):
            return self.observed('preflight', None, super().preflight)

        def prepare(self, job, *args, **kwargs):
            return self.observed('prepare', job.get('job_id'), super().prepare, job, *args, **kwargs)

        def status(self, job_id):
            return self.observed('status', job_id, super().status, job_id)

        def inspect(self, job):
            return self.observed('inspect', job.get('job_id'), super().inspect, job)

        def cancel(self, job_id):
            return self.observed('cancel', job_id, super().cancel, job_id)

        def save(self, job):
            return self.observed('save', job.get('job_id'), super().save, job)

        def dispose(self, job_id):
            role = 'writer' if job_id == FIXTURE_JOB else 'verifier' if job_id == FIXTURE_JOB + '-verify' else 'unknown'
            diagnostics.safe(diagnostics.workspace, self.root, job_id, role)
            return self.observed('dispose', job_id, super().dispose, job_id)

    return ObservedSandbox


def main():
    assert os.geteuid() == 0
    assert Path('/etc/hostname').read_text().strip() == 'agentic-disposable'
    serials = [*Path('/sys/class/block').glob('*/serial'), *Path('/sys/class/block').glob('*/device/serial')]
    assert any(p.read_text().strip() == 'AGENTIC_TEST_ONLY' for p in serials)
    assert subprocess.check_output(['systemd-detect-virt', '--vm'], text=True).strip() in {'kvm', 'qemu'}
    sys.path.insert(0, str(PRODUCT / 'host'))
    from agentic_host import coding_service
    from agentic_host.coding_relay import Relay
    from agentic_host.coding_sandbox import ERROR_CODES, CodingSandbox, command
    from agentic_host.coding_volume import DiskVolume

    os.umask(0o077)
    root = Path(tempfile.mkdtemp(prefix='coding-probe-', dir=STATE))
    service = volume = None
    started, stage = time.monotonic(), 'prepare'
    evidence = {'observed_from': 'disposable_installed_linux_vm', 'cloud_account': 'not_used'}
    diagnostics = CodingDiagnostics()
    calls = []

    def run(*args):
        return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL,
            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'},
            timeout=COMMAND_TIMEOUT_SECONDS).strip()

    try:
        stage = 'volume_quota'
        volume = DiskVolume(root / 'volumes', 'probe-quota', 64)
        mount = volume.create()
        capacity = os.statvfs(mount).f_blocks * os.statvfs(mount).f_frsize
        assert capacity <= 64 * 1024**2
        written, error = 0, None
        try:
            with (mount / 'capacity.bin').open('wb', buffering=0) as stream:
                for _ in range(80):
                    written += stream.write(b'x' * 1024**2)
        except OSError as exc:
            error = exc.errno
        assert error == 28 and written > 32 * 1024**2
        volume.close()
        assert not volume.directory.exists()
        evidence['disk_volume'] = {'capacity_bytes': capacity, 'written_bytes': written, 'errno': error, 'cleaned': True}
        stage = 'repository'
        checkout = root / 'fixture'
        checkout.mkdir()
        (checkout / 'calculator.py').write_text('def add(a,b): return a-b\n')
        (checkout / 'test_calculator.py').write_text('import unittest\nfrom calculator import add\nclass Test(unittest.TestCase):\n def test_add(self): self.assertEqual(add(4,2),6)\n')
        (checkout / 'tests').mkdir()
        (checkout / 'tests/test_engine.py').write_text('from calculator import add\ndef test_add(): assert add(4,2)==6\n')
        (checkout / 'tests/test_api.py').write_text('from calculator import add\ndef test_other(): assert add(4,-2)==2\n')
        (checkout / 'tests/conftest.py').write_text('import sys\nsys.path.insert(0,"/work/source")\n')
        run('git', 'init', '-q', str(checkout))
        run('git', '-C', str(checkout), 'add', '.')
        run('git', '-C', str(checkout), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
        commit = run('git', '-C', str(checkout), 'rev-parse', 'HEAD')
        (root / 'coding').mkdir()
        run('git', 'clone', '--bare', str(checkout), str(root / 'coding/source'))
        (root / 'coding/source.json').write_text(json.dumps({'repo_id': 'agentic-home', 'commit': commit}))
        profile = root / 'runner/coding-auth/profiles/codex'
        profile.mkdir(parents=True)
        (profile / 'auth.json').write_text(json.dumps({'OPENAI_API_KEY': 'fixture-role-key-not-a-cloud-account'}))
        (profile / 'auth.json').chmod(0o600)
        def transport(host, path, method, body, headers, **kwargs):
            assert host == 'api.openai.com' and path == '/v1/responses'
            assert headers['Authorization'] == 'Bearer fixture-role-key-not-a-cloud-account'
            assert b'fixture-role-key' not in body
            value = json.loads(body)
            calls.append(value)
            if len(calls) == 1:
                cmd = "python -c \"from pathlib import Path; p=Path('calculator.py'); p.write_text(p.read_text().replace('a-b','a+b'))\" && python -m unittest -v"
                item = {'type': 'function_call', 'id': 'fc_fixture', 'call_id': 'call_fixture', 'name': 'exec_command',
                        'namespace': 'functions', 'arguments': json.dumps({'cmd': cmd, 'max_output_tokens': 2000}), 'status': 'completed'}
            else:
                assert any(x.get('type') == 'function_call_output' and 'OK' in str(x) for x in value['input'])
                item = {'id': 'msg_fixture', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                        'content': [{'type': 'output_text', 'text': 'Fixed add; tests pass. No deployment.', 'annotations': []}]}
            events = [{'type': 'response.created', 'response': {'id': 'resp_fixture'}},
                      {'type': 'response.output_item.added', 'output_index': 0, 'item': item},
                      {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                      {'type': 'response.completed', 'response': {'id': 'resp_fixture', 'status': 'completed',
                       'output': [item], 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}}]
            return 200, 'text/event-stream', ''.join('event: ' + e['type'] + '\ndata: ' + json.dumps(e) + '\n\n' for e in events).encode()

        coding_service.Relay = lambda path, credentials, deadline: Relay(path, credentials, deadline, transport=transport)
        service = coding_service.CodingService(SimpleNamespace(root=root / 'host', product=PRODUCT),
            sandbox_factory=observed_sandbox(CodingSandbox, diagnostics, command, ERROR_CODES))
        stage = 'coding_dispatch'
        body = {'work_contract_major': 1, 'id': FIXTURE_JOB, 'case_id': 'b' * 32, 'worker_id': 'host-coder',
                'mode': 'development', 'provider': 'codex', 'model': '', 'repo_id': 'agentic-home',
                'goal': 'Repair add and prove tests', 'questions': [], 'base_commit': None,
                'expires': time.time() + JOB_EXPIRES_SECONDS, 'lease_epoch': 1, 'context_days': 1}
        status, job = service.handle('POST', '/coding/jobs', body)
        diagnostics.safe(diagnostics.job, job)
        assert status == 202 and job['state'] in {'queued', 'running'}, job.get('error_code')
        assert service.handle('POST', '/coding/jobs', body)[1]['id'] == job['id']
        deadline = time.monotonic() + JOB_WAIT_SECONDS
        while time.monotonic() < deadline:
            _, job = service.handle('GET', '/coding/jobs/' + body['id'])
            diagnostics.safe(diagnostics.job, job)
            if job['stop_confirmed']:
                break
            time.sleep(0.25)
        evidence['job'] = diagnostics.safe(job_receipt, job, ERROR_CODES) or {'observation': 'failed'}
        stage = 'verification_evidence'
        for metadata in job.get('artifacts', []):
            if metadata.get('name') not in {'verification.json', 'verification.log'}:
                continue
            code, report = service.handle('GET', '/coding/jobs/' + body['id'] + '/artifacts/' + metadata['id'])
            assert code == 200
            raw = base64.b64decode(report['content_base64'], validate=True)
            assert len(raw) <= 180 * 1024 and hashlib.sha256(raw).hexdigest() == metadata['sha256']
            if metadata['name'] == 'verification.json':
                value = json.loads(raw)
                # Only typed, fixed completion fields; no generated output or paths.
                evidence['verifier_completion'] = {key: value[key] for key in
                    ('completion_verified', 'tests_executed', 'exit_code')
                    if key in value and type(value[key]) in {int, bool}}
            else:
                text = raw[:8192].decode(errors='replace')
                evidence['verifier_log_summary'] = {'bytes': len(raw), 'scanned_bytes': min(len(raw), 8192),
                    'permission_error': 'PermissionError' in text or 'Permission denied' in text,
                    'collection_error': 'ERROR collecting' in text, 'no_tests_ran': 'no tests ran' in text,
                    'import_error': 'ModuleNotFoundError' in text or 'ImportError' in text}
        stage = 'coding_verified'
        assert job['state'] == 'succeeded' and job['stop_confirmed'], job.get('error_code')
        assert job['verification']['state'] == 'passed' and len(calls) == 2
        patch = next(a for a in job['artifacts'] if a['kind'] == 'patch')
        _, artifact = service.handle('GET', '/coding/jobs/' + body['id'] + '/artifacts/' + patch['id'])
        raw = base64.b64decode(artifact['content_base64'])
        assert hashlib.sha256(raw).hexdigest() == job['verification']['patch_sha256']
        assert b'+def add(a,b): return a+b' in raw
        assert run('git', '--git-dir', str(root / 'coding/source'), 'rev-parse', 'HEAD') == commit
        handoff = service.handle('POST', '/coding/jobs/' + body['id'] + '/handoff')[1]
        assert handoff['stop_confirmed'] and handoff['live_session'] is False
        assert patch['id'] in handoff['checkpoint']['artifact_ids']
        cleanup_deadline = time.monotonic() + CLEANUP_WAIT_SECONDS
        while time.monotonic() < cleanup_deadline:
            if service.handle('GET', '/coding/jobs/' + body['id'])[1].get('workspace_released'):
                break
            time.sleep(0.1)
        assert not list((root / 'coding/work').glob('*/workspace.ext4'))
        evidence.update(status='passed', actual_cli='0.157.1', independent_verifier='passed', tool_calls=1,
                        source_unchanged=True, private_credentials_not_exposed=True, handoff='fenced_checkpoint',
                        stopped_workspaces_reclaimed=True)
    except Exception as exc:
        evidence.update(status='failed', stage=stage, error=type(exc).__name__)
    finally:
        try:
            if service:
                service.close()
                for item in service.items.values():
                    if item['execution_started']:
                        for sandbox_id in {item['id'], item.get('sandbox_job_id')} - {None}:
                            service.sandbox().dispose(sandbox_id)
            if volume:
                volume.close()
            shutil.rmtree(root)
            evidence['cleanup'] = 'passed'
        except Exception as exc:
            evidence['cleanup'] = type(exc).__name__
        evidence['duration_seconds'] = round(time.monotonic() - started, 2)
        evidence['diagnostics'] = diagnostics.safe(diagnostics.snapshot, len(calls)) or {'observation': 'failed'}
        print(json.dumps(evidence))
    return evidence.get('status') == 'passed' and evidence.get('cleanup') == 'passed'


if __name__ == '__main__':
    raise SystemExit(0 if main() else 1)
