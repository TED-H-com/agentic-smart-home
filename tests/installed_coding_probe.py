"""Run via sudo python3 - only inside disposable installed ISO test VM; no accounts."""
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
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


def main():
    assert os.geteuid() == 0
    assert Path('/etc/hostname').read_text().strip() == 'agentic-disposable'
    serials = [*Path('/sys/class/block').glob('*/serial'), *Path('/sys/class/block').glob('*/device/serial')]
    assert any(p.read_text().strip() == 'AGENTIC_TEST_ONLY' for p in serials)
    assert subprocess.check_output(['systemd-detect-virt', '--vm'], text=True).strip() in {'kvm', 'qemu'}
    sys.path.insert(0, str(PRODUCT / 'host'))
    from agentic_host import coding_service
    from agentic_host.coding_relay import Relay
    from agentic_host.coding_volume import DiskVolume

    os.umask(0o077)
    root = Path(tempfile.mkdtemp(prefix='coding-probe-', dir=STATE))
    service = volume = None
    started, stage = time.monotonic(), 'prepare'
    evidence = {'observed_from': 'disposable_installed_linux_vm', 'cloud_account': 'not_used'}

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
        calls = []

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
        service = coding_service.CodingService(SimpleNamespace(root=root / 'host', product=PRODUCT))
        stage = 'coding_dispatch'
        body = {'work_contract_major': 1, 'id': 'a' * 32, 'case_id': 'b' * 32, 'worker_id': 'host-coder',
                'mode': 'development', 'provider': 'codex', 'model': '', 'repo_id': 'agentic-home',
                'goal': 'Repair add and prove tests', 'questions': [], 'base_commit': None,
                'expires': time.time() + JOB_EXPIRES_SECONDS, 'lease_epoch': 1, 'context_days': 1}
        status, job = service.handle('POST', '/coding/jobs', body)
        assert status == 202 and job['state'] in {'queued', 'running'}, job.get('error_code')
        assert service.handle('POST', '/coding/jobs', body)[1]['id'] == job['id']
        deadline = time.monotonic() + JOB_WAIT_SECONDS
        while time.monotonic() < deadline:
            _, job = service.handle('GET', '/coding/jobs/' + body['id'])
            if job['stop_confirmed']:
                break
            time.sleep(0.25)
        evidence['job'] = {k: job.get(k) for k in ('state', 'stop_confirmed', 'error_code', 'exit_code', 'verification')}
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
        print(json.dumps(evidence))
    return evidence.get('status') == 'passed' and evidence.get('cleanup') == 'passed'


if __name__ == '__main__':
    raise SystemExit(0 if main() else 1)
