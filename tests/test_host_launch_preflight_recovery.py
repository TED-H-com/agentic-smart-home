"""Actual Host lifecycle with fixed Docker/environment replies; no containers or mounts."""
import hashlib
import json
import os
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'host'))
from agentic_host import coding_service  # noqa: E402
from agentic_host.coding_sandbox import CodingSandbox, SandboxError  # noqa: E402
from test_coding_job_budgets import bare_source, request  # noqa: E402

IMAGE = 'sha256:' + '1' * 64


@pytest.mark.parametrize('trigger', ['disk_reserve', 'resource_limit', 'retained_workspaces'])
@pytest.mark.parametrize('recovery', ['background', 'explicit_cancel', 'restart'])
def test_preallocation_refusal_keeps_identity_and_can_recover(tmp_path, monkeypatch, trigger, recovery):
    source = bare_source(tmp_path)
    root = tmp_path / 'coding'
    work = root / 'work'
    product = tmp_path / 'product'
    product.mkdir()
    (product / 'images.lock.json').write_text(json.dumps({'agentic-home-runner:0.11.1': IMAGE}))
    state = {'fail': False, 'daemon': True, 'calls': []}
    original_stat, original_space = Path.stat, os.statvfs

    def trusted_stat(path, *args, **kwargs):
        value = original_stat(path, *args, **kwargs)
        if path == work or path in work.parents:
            fields = list(value)
            fields[4] = 0
            fields[0] = stat.S_IFDIR | (0o700 if path == work else 0o755)
            return os.stat_result(fields)
        return value

    monkeypatch.setattr(Path, 'stat', trusted_stat)
    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    monkeypatch.setattr(os, 'statvfs', lambda path: SimpleNamespace(f_bavail=0 if state['fail'] and
        trigger == 'disk_reserve' else 64 * 1024**3, f_frsize=1) if path == work else original_space(path))
    monkeypatch.setattr(coding_service, 'credentials', lambda path: ('fixture', '/responses', {}))

    class Relay:
        path = None

        def __init__(self, *args):
            pass

        def start(self):
            state['fail'] = True
            # The failure to observe the daemon must keep the fresh ledger
            # fenced until the next trusted stop reconciliation.
            state['daemon'] = False

        def close(self):
            pass

    def execute(*args, **kwargs):
        state['calls'].append(args[:2])
        if args[:2] == ('docker', 'info'):
            value = {'MemoryLimit': not (state['fail'] and trigger == 'resource_limit'), 'SwapLimit': True,
                'PidsLimit': True, 'CpuCfsQuota': True, 'CgroupVersion': '2',
                'SecurityOptions': ['name=seccomp', 'name=apparmor']}
            return SimpleNamespace(returncode=0, stdout=json.dumps(value))
        if args[0] == 'findmnt':
            return SimpleNamespace(returncode=0, stdout='ext4\n')
        if args[:2] == ('docker', 'version'):
            return SimpleNamespace(returncode=0 if state['daemon'] else 1, stdout='fixture')
        if args[:2] == ('docker', 'ps'):
            return SimpleNamespace(returncode=0, stdout='')
        pytest.fail('preallocation refusal must not allocate, mount or launch')

    def factory(*args, **kwargs):
        return CodingSandbox(*args, execute=execute, **kwargs)

    manager = SimpleNamespace(root=tmp_path / 'host', product=product)
    def create_service():
        return coding_service.CodingService(manager, source=source, source_lock_path=source.with_suffix('.json'),
            sandbox_factory=factory, relay_factory=Relay)

    service = create_service()
    payload = request(worker_id='host-coder', expires=time.time() + 300)
    try:
        assert service.capabilities()['available']
        if trigger == 'retained_workspaces':
            for index in range(4):
                (work / ('retained-' + str(index))).mkdir()
        status, queued = service.submit(payload)
        assert status == 202 and queued['state'] == 'queued'
        service.threads[payload['id']].join(30)
        assert not service.threads[payload['id']].is_alive()
        job = service.items[payload['id']]
        assert job['sandbox_attempted'] and not job['execution_started']
        assert job['state'] == 'stop_unconfirmed' and not job['stop_confirmed']
        ledger = work / (payload['id'] + '.json')
        if not ledger.exists():
            # Baseline reproduction: a healthy daemon cannot reconcile a
            # launch that never received a sandbox identity.
            state['daemon'] = True
            service.prune()
            assert service.active() and not job['stop_confirmed']
            assert service.handle('POST', '/coding/jobs/' + payload['id'] + '/cancel', {})[0] == 409
            service.close()
            service = create_service()
            assert service.active() and not service.items[payload['id']]['stop_confirmed']
        assert ledger.exists(), 'preallocation rejection must retain exact identity for reconciliation'
        assert json.loads(ledger.read_text())['state'] == 'stop_unconfirmed'
        assert not (work / payload['id']).exists()
        state['daemon'] = True
        if recovery == 'background':
            service.prune()
        elif recovery == 'explicit_cancel':
            assert service.handle('POST', '/coding/jobs/' + payload['id'] + '/cancel', {})[0] == 200
        else:
            # close() reconciles a live writer; simulate process loss by keeping
            # its observations unavailable until its original threads stop.
            state['daemon'] = False
            service.close()
            state['daemon'] = True
            service = create_service()
        recovered = service.items[payload['id']]
        service.prune()
        assert recovered['state'] in {'failed', 'interrupted'} and recovered['stop_confirmed']
        assert recovered['execution_started'] is False and recovered['workspace_released'] is True
        assert not service.active() and json.loads(ledger.read_text())['state'] == 'disposed'
        assert not any(command in {('docker', 'create'), ('docker', 'start'), ('docker', 'kill')}
                       for command in state['calls'])
    finally:
        service.close()


@pytest.mark.parametrize('unsafe', ['nonroot', 'symlink', 'nonprivate', 'untrusted_ancestor', 'lock_unavailable'])
def test_untrusted_root_is_rejected_before_ledger_or_environment_probe(tmp_path, monkeypatch, unsafe):
    root = tmp_path / 'work'
    root.mkdir(mode=0o700)
    calls = []
    sandbox = CodingSandbox(root, IMAGE, {'product': tmp_path}, execute=lambda *args, **kwargs: calls.append(args))
    original_stat = Path.stat
    def trusted_stat(path, *args, **kwargs):
        value = original_stat(path, *args, **kwargs)
        fields = list(value)
        fields[4] = 0
        if path == root:
            fields[0] = stat.S_IFDIR | (0o755 if unsafe == 'nonprivate' else 0o700)
        elif path in root.parents:
            fields[0] = stat.S_IFDIR | (0o777 if unsafe == 'untrusted_ancestor' and path == tmp_path else 0o755)
        return os.stat_result(fields)
    monkeypatch.setattr(Path, 'stat', trusted_stat)
    monkeypatch.setattr(os, 'geteuid', lambda: 1000 if unsafe == 'nonroot' else 0)
    if unsafe == 'symlink':
        target = tmp_path / 'target'
        target.mkdir()
        root.rmdir()
        root.symlink_to(target, target_is_directory=True)
    if unsafe == 'lock_unavailable':
        def unavailable():
            raise OSError('fixture lock unavailable')
        monkeypatch.setattr(sandbox, 'lock', unavailable)
    with pytest.raises((SandboxError, OSError)):
        sandbox.launch('a' * 32, 1, 'product', 'b' * 40, 'pass')
    assert calls == [] and not list(root.glob('*.json'))
    assert not (root / 'writer.lock').exists()


@pytest.mark.parametrize('existing', ['same_request', 'different_request', 'previous_writer'])
def test_existing_identity_is_checked_before_new_environment_refusal(tmp_path, monkeypatch, existing):
    sandbox = CodingSandbox(tmp_path, IMAGE, {'product': tmp_path})
    monkeypatch.setattr(sandbox, '_require_work_root', lambda: None)
    def unexpected():
        pytest.fail('existing identity must be resolved before environment checks')
    monkeypatch.setattr(sandbox, 'preflight', unexpected)
    identifier, commit = 'a' * 32, 'b' * 40
    digest = hashlib.sha256(json.dumps([identifier, 1, 'product', commit, 'pass'], separators=(',', ':')).encode()).hexdigest()
    before = {'job_id': identifier, 'state': 'stop_unconfirmed', 'request_digest': digest}
    sandbox.save(before)
    if existing == 'same_request':
        assert sandbox.launch(identifier, 1, 'product', commit, 'pass')['state'] == 'stop_unconfirmed'
    elif existing == 'different_request':
        with pytest.raises(SandboxError, match='different payload'):
            sandbox.launch(identifier, 2, 'product', commit, 'pass')
    else:
        with pytest.raises(SandboxError, match='fenced'):
            sandbox.launch('c' * 32, 1, 'product', commit, 'pass')
    assert sandbox.record(identifier) == before
    assert len(list(tmp_path.glob('*.json'))) == 1
