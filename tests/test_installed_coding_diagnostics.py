"""Exercise installed fixture diagnostics with actual Host methods, without Docker."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import installed_coding_probe as probe
import pytest

JOB = probe.FIXTURE_JOB
NAME = 'agentic-coding-' + JOB
CID = '1' * 64
IMAGE = 'sha256:' + '2' * 64
PRIVATE = 'private-credential-and-path-never-exported'


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / 'host'))
    from agentic_host import coding_sandbox, coding_service
    return coding_sandbox, coding_service


@pytest.mark.parametrize('fault', ['timeout_once', 'nonzero_once', 'identity_mismatch'])
def test_first_inspect_failure_survives_actual_stop_recovery_and_event_churn(tmp_path, runtime, fault):
    sandbox_module, service_module = runtime
    calls, running, inspections = [], [True], [0]
    diagnostics = probe.CodingDiagnostics()

    def execute(*command, **kwargs):
        calls.append(command[:2])
        if command[:2] == ('docker', 'inspect'):
            inspections[0] += 1
            if fault == 'timeout_once' and inspections[0] == 1:
                raise sandbox_module.SandboxError(PRIVATE, code='command_timeout',
                    operation='docker.inspect', timeout_seconds=30)
            if fault == 'nonzero_once' and inspections[0] == 1:
                return subprocess.CompletedProcess(command, 1, '', 'Cannot connect to the Docker daemon: ' + PRIVATE)
            return subprocess.CompletedProcess(command, 0, json.dumps([{
                'Id': '9' * 64 if fault == 'identity_mismatch' else CID, 'Image': IMAGE,
                'Config': {'Labels': {'agentic.coding': NAME, 'private': PRIVATE}, 'Env': [PRIVATE]},
                'State': {'Running': running[0], 'Paused': False, 'OOMKilled': False,
                          'Status': 'running' if running[0] else 'exited',
                          'ExitCode': 0 if running[0] else 137, 'Error': PRIVATE}}]), '')
        if command[:2] == ('docker', 'kill'):
            running[0] = False
        return subprocess.CompletedProcess(command, 0, CID + '\n' if command[:2] == ('docker', 'ps') else '', '')

    observed = probe.observed_sandbox(sandbox_module.CodingSandbox, diagnostics, execute, sandbox_module.ERROR_CODES)
    sandbox = observed(tmp_path, IMAGE, {})
    sandbox.save({'job_id': JOB, 'lease_epoch': 1, 'container': NAME, 'container_id': CID, 'state': 'running'})
    first, second = sandbox.status(JOB), sandbox.status(JOB)
    assert first['state'] == second['state'] == 'stop_unconfirmed'
    service = service_module.CodingService.__new__(service_module.CodingService)
    service.threads, service.sandboxes = {}, {JOB: sandbox}
    service.sandbox = lambda: sandbox
    service.publish = lambda *args, **kwargs: None
    service.results_outstanding = lambda job: False
    service.release_workspace = lambda job: True
    job = {'id': JOB, 'lease_epoch': 1, 'state': 'running', 'phase': 'running', 'stop_confirmed': False,
           'error_code': None, 'exit_code': None, 'verification': None, 'results_collected': True}
    service.finish_stopped(job, sandbox, None, first, None)
    diagnostics.job(job)
    service.retry_stop(job)
    diagnostics.job(job)
    snapshot = diagnostics.snapshot(0)
    failure = snapshot['first_failures']['writer:inspect']
    if fault == 'identity_mismatch':
        assert failure['error']['inspection_reason'] == 'container_id_mismatch'
        assert not job['stop_confirmed'] and job['state'] == 'stop_unconfirmed'
        assert ('docker', 'kill') not in calls
    else:
        assert job['state'] == 'failed' and job['stop_confirmed']
        assert job['error_code'] == 'writer_stop_unconfirmed' and job['exit_code'] is None and job['verification'] is None
        assert calls == [('docker', 'inspect')] * 3 + [('docker', 'kill'), ('docker', 'wait'), ('docker', 'inspect')]
        direct = snapshot['first_failures']['writer:docker.inspect']
        if fault == 'timeout_once':
            assert direct['error']['error_code'] == 'command_timeout'
            assert direct['error']['timeout_seconds'] == 30
        else:
            assert direct['returncode'] == 1 and direct['stderr']['docker_unavailable']
        successful = [item for item in snapshot['events'] if item['operation'] == 'inspect' and 'identity' in item]
        assert successful[-1]['identity'] == {'label_matches': True, 'container_id_matches': True, 'image_matches': True}
        assert successful[-1]['container']['exit_code'] == 137
    for _ in range(100):
        diagnostics.event('writer', 'status', state='disposed')
    final = diagnostics.snapshot(2)
    assert len(final['events']) == probe.DIAGNOSTIC_EVENT_LIMIT and final['events_dropped'] > 0
    assert final['first_failures']['writer:inspect'] == failure
    assert final['first_failures']['job']['state'] == 'stop_unconfirmed'
    assert final['relay_requests'] == 2 and final['observer_errors'] == 0
    serialized = json.dumps(final)
    assert all(value not in serialized for value in (PRIVATE, CID, NAME, IMAGE, str(tmp_path)))


def test_pre_disposal_file_signatures_survive_cleanup_and_later_missing_read(tmp_path, runtime):
    sandbox_module, _ = runtime
    workspace = tmp_path / JOB / 'workspace'
    (workspace / 'scratch').mkdir(parents=True)
    (workspace / 'artifacts').mkdir()
    (workspace / 'stderr.txt').write_text('x' * 20000 + '\nPermission denied: ' + PRIVATE)
    (workspace / 'scratch/events.jsonl').write_text('ModuleNotFoundError: ' + PRIVATE)
    (workspace / 'artifacts/execution.json').write_text(json.dumps({'cli_exit_code': 9, 'secret': PRIVATE}))
    calls = []

    class Disposable:
        def __init__(self, root, image, repos, *, execute):
            self.root, self.image = root, image

        def dispose(self, job_id):
            calls.append(job_id)
            shutil.rmtree(self.root / job_id, ignore_errors=True)
            return {'job_id': job_id, 'lease_epoch': 1, 'state': 'disposed'}

    diagnostics = probe.CodingDiagnostics()
    observed = probe.observed_sandbox(Disposable, diagnostics, None, sandbox_module.ERROR_CODES)
    sandbox = observed(tmp_path, IMAGE, {})
    assert sandbox.dispose(JOB)['state'] == 'disposed'
    sandbox.dispose(JOB)
    assert calls == [JOB, JOB] and not (tmp_path / JOB).exists()
    files = diagnostics.snapshot(1)['pre_disposal_files']['writer']
    assert files['stderr']['permission_denied'] and files['stderr']['scanned_bytes'] == probe.DIAGNOSTIC_BYTES
    assert files['stderr']['truncated'] and files['cli_events']['import_error']
    assert files['execution']['cli_exit_code'] == 9
    assert files['stdout']['read_status'] == 'missing'
    assert PRIVATE not in json.dumps(diagnostics.snapshot(1))


@pytest.mark.parametrize('kind', ['file_symlink', 'parent_symlink', 'fifo'])
def test_bounded_file_refuses_links_and_special_files(tmp_path, kind):
    workspace = tmp_path / JOB / 'workspace'
    workspace.mkdir(parents=True)
    outside = tmp_path / 'private'
    outside.mkdir()
    (outside / 'events.jsonl').write_text('Permission denied: ' + PRIVATE)
    if kind == 'parent_symlink':
        (workspace / 'scratch').symlink_to(outside, target_is_directory=True)
    else:
        (workspace / 'scratch').mkdir()
        target = workspace / 'scratch/events.jsonl'
        if kind == 'file_symlink':
            target.symlink_to(outside / 'events.jsonl')
        else:
            os.mkfifo(target)
    result = probe.bounded_file(tmp_path, JOB, 'scratch/events.jsonl')
    assert result['read_status'] in {'unreadable', 'not_regular'}
    assert 'permission_denied' not in result


def test_diagnostic_failure_never_masks_original_exception_or_prevents_dispose(tmp_path, runtime, monkeypatch):
    sandbox_module, _ = runtime
    original = sandbox_module.SandboxError(PRIVATE, code='command_failed', operation='docker.start', exit_code=7)
    calls = []

    class BrokenLaunch:
        def __init__(self, root, image, repos, *, execute):
            self.root = root

        def launch(self, job_id):
            calls.append('launch')
            raise original

        def dispose(self, job_id):
            calls.append('dispose')
            return {'state': 'disposed'}

    diagnostics = probe.CodingDiagnostics()
    def broken(*args, **kwargs):
        raise OSError(PRIVATE)
    monkeypatch.setattr(diagnostics, 'event', broken)
    monkeypatch.setattr(diagnostics, 'workspace', broken)
    observed = probe.observed_sandbox(BrokenLaunch, diagnostics, None, sandbox_module.ERROR_CODES)
    sandbox = observed(tmp_path, IMAGE, {})
    try:
        with pytest.raises(sandbox_module.SandboxError) as caught:
            sandbox.launch(JOB)
        assert caught.value is original
    finally:
        assert sandbox.dispose(JOB) == {'state': 'disposed'}
    assert calls == ['launch', 'dispose'] and diagnostics.snapshot(0)['observer_errors'] == 3
    assert PRIVATE not in json.dumps(diagnostics.snapshot(0))


def test_job_projection_and_transition_history_are_typed_and_bounded():
    tick = [1.0]
    diagnostics = probe.CodingDiagnostics(clock=lambda: tick[0])
    for index in range(100):
        tick[0] += 0.5
        diagnostics.job({'state': 'running' if index % 2 else 'launching', 'phase': PRIVATE,
            'stop_confirmed': 'private', 'execution_started': True, 'exit_code': True,
            'verification': {'state': [PRIVATE], 'private': PRIVATE}, 'request': PRIVATE})
    value = diagnostics.snapshot(3)
    assert len(value['job_transitions']) == 32 and value['job_transitions_dropped'] == 68
    assert value['last_job']['phase'] == 'unknown' and value['last_job']['exit_code'] is None
    assert 'stop_confirmed' not in value['last_job']
    assert value['last_job']['verification'] == {'state': 'unknown'}
    assert value['job_transitions'][0]['at_seconds'] == 0.5
    assert PRIVATE not in json.dumps(value)


def test_successful_verifier_receipt_keeps_fixed_acceptance_fields():
    verification = {'state': 'passed', 'patch_sha256': 'a' * 64, 'test_command_sha256': 'b' * 64,
        'observed_from': 'independent_host_verifier', 'exit_code': 0, 'stop_confirmed': True,
        'tests_executed': 2, 'trusted_tests': 'pinned_baseline', 'network': 'none',
        'web_preview': {'state': 'not_applicable'}, 'private': PRIVATE}
    projected = probe.job_receipt({'state': 'succeeded', 'verification': verification})['verification']
    assert projected == {key: value for key, value in verification.items() if key != 'private'}
    assert PRIVATE not in json.dumps(projected)
