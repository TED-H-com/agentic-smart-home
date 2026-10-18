"""Slow fixture observations remain bounded, attributable and free of raw text."""
import ast
import json
import subprocess
from pathlib import Path

import home_worker_haos_probe as probe
import pytest
import test_home_worker_haos as home
import test_linux_install as install


class Clock:
    value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def test_firstboot_probes_record_every_result_and_elapsed_without_private_streams():
    clock, rows, calls = Clock(), [], []
    def run(args, **kwargs):
        calls.append(kwargs)
        clock.advance(12)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(args, kwargs['timeout'], stderr=b'private-token connection timed out')
        return subprocess.CompletedProcess(args, 0 if len(calls) == 3 else 1, b'private-body', b'private-path')
    for expected in (False, False, True):
        assert install.remote_ok(['ssh', 'private-host'], 'private-command', run=run,
            observations=rows, clock=clock) is expected
    assert [row['status'] for row in rows] == ['timeout', 'not_ready', 'ready']
    assert [row['attempt'] for row in rows] == [1, 2, 3]
    assert all(row['elapsed_seconds'] == 12 for row in rows)
    assert rows[0]['process']['stderr_signatures']['ssh_transport']
    assert 'private' not in json.dumps(rows)
    assert install.FIRSTBOOT_CONNECT_SECONDS == 20 < calls[0]['timeout']


def test_firstboot_observation_cap_and_failed_checkpoint_preserve_readiness():
    rows = []
    def checkpoint():
        raise OSError('private path')
    def run(*args, **kwargs):
        return subprocess.CompletedProcess([], 0, b'', b'')
    assert install.remote_ok(['ssh'], 'true', run=run, observations=rows, checkpoint=checkpoint)
    assert rows[0]['checkpoint_failed']
    rows[:] = [rows[0]] * install.FIRSTBOOT_MAX_OBSERVATIONS
    assert install.remote_ok(['ssh'], 'true', run=run, observations=rows)
    assert len(rows) == install.FIRSTBOOT_MAX_OBSERVATIONS


@pytest.mark.parametrize('journal_status,expected', [('timeout', 'journal_timeout'), ('probe_failed', 'journal_unavailable')])
def test_firstboot_failure_retries_only_read_diagnostics_and_retains_prior_errors(tmp_path, journal_status, expected):
    clock, calls = Clock(), []
    report = tmp_path / 'failure.json'
    def run(args, **kwargs):
        calls.append((args, kwargs))
        assert json.loads(report.read_text())['error_type'] == 'AssertionError'
        assert args[-1] == 'sudo env AGENTIC_DISPOSABLE_VM=1 python3 -'
        assert 'support.PROBE_SECONDS=15' in kwargs['input']
        clock.advance(10)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired('private-command', 10, stderr=b'private-token')
        if len(calls) == 2:
            return subprocess.CompletedProcess(args, 0, json.dumps({'journal': {'observation': journal_status}}), '')
        return subprocess.CompletedProcess(args, 0, json.dumps({'journal': {'observation': 'ok',
            'signatures': {'disk_full': True}, 'private': 'private-token'}}), '')
    result = install.record_install_failure(AssertionError('Original firstboot failure'), 'installed_firstboot',
        report, ['ssh', 'private-host'], run=run, clock=clock, pause=clock.advance)
    assert [row['status'] for row in result['diagnostic_attempts']] == ['unavailable', expected, 'observed']
    assert result['remote']['journal']['signatures']['disk_full']
    assert 'diagnostic_error_type' not in result
    assert 'private' not in report.read_text()
    assert len(calls) == 3 and clock.value == 40


@pytest.mark.parametrize('status', ['permission_denied', 'command_missing', 'output_limit'])
def test_firstboot_meaningful_journal_failure_is_not_retried(tmp_path, status):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps({'journal': {'observation': status}}), '')
    result = install.record_install_failure(AssertionError('failed'), 'installed_firstboot',
        tmp_path / 'failure.json', ['ssh'], run=run)
    assert len(calls) == 1 and result['remote']['journal']['observation'] == status


def test_firstboot_diagnostic_retries_share_remaining_wall_clock_budget(tmp_path):
    clock, bounds = Clock(), []
    def run(args, **kwargs):
        bounds.append(kwargs['timeout'])
        clock.advance(kwargs['timeout'])
        raise subprocess.TimeoutExpired('private', kwargs['timeout'])
    result = install.record_install_failure(AssertionError('failed'), 'installed_firstboot', tmp_path / 'failure.json',
        ['ssh'], run=run, clock=clock, pause=clock.advance)
    assert bounds == [120, 120, 110]
    assert clock.value == 360 and result['remote'] == 'unavailable'
    assert len(result['diagnostic_attempts']) == 3


@pytest.mark.parametrize('failure', ['exit', 'timeout', 'oserror'])
def test_home_remote_failure_keeps_exact_step_but_never_repeats_effect_or_exports_text(failure):
    clock, calls, evidence = Clock(), [], {}
    def run(args, **kwargs):
        calls.append(args)
        clock.advance(9)
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(args, 30, output=b'private-body', stderr=b'private-token permission denied')
        if failure == 'oserror':
            raise OSError('private-token private-path')
        return subprocess.CompletedProcess(args, 1, b'private-body', b'private-token permission denied' + b'x' * 20000)
    with pytest.raises(AssertionError, match='^fixture_remote_failed$'):
        home.fixture_remote(['ssh', 'private-host'], 'docker kill private-container', evidence,
            step='app_kill', run=run, clock=clock)
    assert len(calls) == 1
    row = evidence['fixture_command_failures'][0]
    assert row['step'] == 'app_kill' and row['elapsed_seconds'] == 9
    if failure != 'oserror':
        assert row['stderr']['permission_denied']
    if failure == 'exit':
        assert row['stderr']['truncated'] and row['stderr']['scanned_bytes'] == 16384
    assert all(secret not in json.dumps(evidence) for secret in (
        'private-token', 'private-body', 'private-path', 'private-host', 'private-container'))


def test_home_remote_checkpoint_failure_preserves_original_error_and_retention_is_bounded():
    evidence = {}
    def fail_checkpoint():
        raise OSError('private path')
    for _ in range(65):
        with pytest.raises(AssertionError, match='fixture_remote_failed'):
            home.fixture_remote(['ssh'], 'private-command', evidence, step='job_proof',
                run=lambda *a, **k: subprocess.CompletedProcess([], 1, b'', b''), checkpoint=fail_checkpoint)
    assert len(evidence['fixture_command_failures']) == 64
    assert all(row['checkpoint_failed'] for row in evidence['fixture_command_failures'])


def test_every_home_remote_call_has_a_fixed_reviewed_step():
    tree = ast.parse(Path(home.__file__).read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == 'remote']
    assert len(calls) >= 20
    for call in calls:
        step = next((keyword.value for keyword in call.keywords if keyword.arg == 'step'), None)
        assert isinstance(step, ast.Constant) and step.value in home.REMOTE_STEPS


class DiagnosticSocket:
    def __init__(self, clock, pieces):
        self.clock, self.pieces, self.timeout = clock, iter(pieces), None
        self.bounds, self.sent, self.closed = [], [], False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def settimeout(self, value):
        self.timeout = value
        self.bounds.append(value)

    def connect(self, address):
        self.clock.advance(1)

    def sendall(self, value):
        self.sent.append(value)
        self.clock.advance(1)

    def recv(self, limit):
        delay, data = next(self.pieces)
        if delay > self.timeout:
            self.clock.advance(self.timeout)
            raise TimeoutError('private socket failure')
        self.clock.advance(delay)
        assert len(data) <= limit
        return data


def test_job_proof_client_tolerates_slow_first_byte_without_extending_deadline(monkeypatch):
    clock = Clock()
    connection = DiagnosticSocket(clock, [(6, b'{"ok":true}'), (1, b'')])
    monkeypatch.setattr(probe.socket, 'socket', lambda *args: connection)
    assert probe.diagnostic_client('a' * 32, clock=clock) == {'ok': True}
    assert clock.value == 9 and connection.closed and len(connection.sent) == 1
    assert connection.bounds == [15, 14, 13, 7]
    assert home.JOB_PROOF_SSH_SECONDS > probe.DIAGNOSTIC_CLIENT_SECONDS


def test_job_proof_chunked_reply_cannot_reset_total_client_deadline(monkeypatch):
    clock = Clock()
    connection = DiagnosticSocket(clock, [(5, b'{'), (5, b'"ok":'), (5, b'true}')])
    monkeypatch.setattr(probe.socket, 'socket', lambda *args: connection)
    with pytest.raises(TimeoutError):
        probe.diagnostic_client('a' * 32, clock=clock)
    assert clock.value == probe.DIAGNOSTIC_CLIENT_SECONDS
    assert connection.closed and len(connection.sent) == 1
