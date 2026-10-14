"""Opt-in real-HAOS proof of the one-click Home coding worker, run with --haos-vm, one heavy suite at a time.

The exact candidate commit is the `stable` branch (local git daemon, or the published branch with
AGENTIC_HOME_WORKER_PUBLIC=1). The sidebar auto-enrolls, the Owner enables the worker through the Host API, and both
Apps must survive a cold backup and a full HAOS reboot with a Controller restart: no repair, re-pair or App restart.
No cloud login, model call or coding run happens.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import hashlib
import json
import lzma
import math
import os
import re
import secrets
import shlex
import shutil
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path

import aiohttp
import pytest
import yaml
from agentic_controller import VERSION
from agentic_controller.bridge_maintenance import STABLE_REPOSITORY, STARTUP_GRACE, BridgeMaintenance
from agentic_controller.ha import HAError
from agentic_controller.home_worker_maintenance import JOB_TIMEOUT
from home_worker_haos_probe import failure_receipt
from test_bridge_haos import (
    BUILDER,
    CONTROLLER,
    HOST_CONTRACT_FIXTURE,
    PYTHON,
    ProbeHA,
    docker,
    lifecycle_observation,
    process,
    setting,
)
from test_home_worker_haos import container_contract, container_projection, failure_hold_seconds
from test_runner_supervisor_build import (
    HEX40,
    TLS_PROBE,
    dockerfile_bases,
    pairing_options,
    real_app_contract,
    upstream_flags,
)

ROOT = Path(__file__).parents[1]
LOCAL_URL = 'git://192.168.177.3/agentic-smart-home.git#stable'
LIMITATIONS = ('no_cloud_login_model_or_coding_executed', 'inference_is_protocol_fixture',
              'host_contract_is_unix_socket_fixture', 'builder_and_pinned_bases_preloaded_registry_not_blocked')
CODE_SOURCES = tuple(ROOT / 'tests' / name for name in (
    'test_home_worker_enable_haos.py', 'test_runner_supervisor_build.py',
    'test_home_worker_haos.py', 'test_bridge_haos.py'))
CHECK_NAMES = frozenset({'exact_head', 'stable_branch_is_exact_head', 'supervisor_healthy',
    'pinned_bases_preloaded', 'bridge_auto_enroll', 'bridge_heartbeat', 'home_worker_enable',
    'login_ready_contract', 'shared_repository', 'real_app_contract', 'persisted_record',
    'tls_fingerprint', 'container_contract', 'cold_backup', 'full_reboot', 'recovery_identity'})
CLEANUP_NAMES = frozenset({'outer_removed', 'git_removed', 'vm_removed', 'network_removed',
                          'disk_fixture_ownership_restored'})
PHASES = ('prepare_exact_head', 'local_images_present', 'prepare_disposable_disks', 'boot_official_haos',
          'git_daemon', 'native_disposable_owner', 'supervisor_readiness', 'preload_builder_and_pinned_bases',
          'outer_candidate_controller', 'bridge_auto_enroll', 'home_worker_enable', 'login_ready_contract',
          'cold_backup', 'full_reboot', 'completion')
# Home worker and sidebar bridge stages and states.
STAGES = frozenset({'repository', 'install', 'configure', 'start', 'reconnect', 'pair', 'login', 'monitor', 'invalid'})
STATES = frozenset({'pending', 'running', 'waiting', 'starting', 'healthy', 'login_ready', 'ready', 'unavailable',
    'blocked', 'disabled', 'needs_reconcile', 'user_disabled', 'user_removed', 'user_modified', 'update_available',
    'degraded', 'invalid'})
# Only the Owner can leave these; a household backup or reboot must never cause one.
FAILED = frozenset({'blocked', 'needs_reconcile', 'user_disabled', 'user_removed', 'user_modified', 'update_available'})
# Core drops in-flight WebSocket replies while HAOS stops; aiohttp raises a TypeError subclass for those.
TRANSIENT = (HAError, aiohttp.ClientError, TimeoutError, TypeError, ValueError, OSError)
SUPERVISOR_STATES = frozenset({'initialize', 'setup', 'startup', 'running', 'freeze', 'shutdown', 'stopping', 'close', 'unknown'})
RECORD_KEYS = frozenset({'id', 'owner', 'enabled', 'managed', 'state', 'stage', 'code', 'release', 'operation',
    'pair_restarts', 'start_sent', 'installed_by_us', 'setup_run_id', 'fingerprint', 'credential_state',
    'updated_at', 'repository_id', 'slug', 'app_state', 'version', 'observed_at', 'failed_code_hash',
    'owner_start', 'started_once', 'pair_deadline', 'grace_until', 'install_confirmation', 'privilege_view',
    'worker_available', 'runtime_location', 'release_store_reloaded', 'stopped_since'})


def repository_slug(url):
    return hashlib.sha1(url.lower().encode()).hexdigest()[:8]


def stable_commit(raw):
    if not isinstance(raw, bytes) or len(raw) > 256:
        return None
    match = re.fullmatch(rb'([a-f0-9]{40})\trefs/heads/stable\n?', raw)
    return match[1].decode() if match else None


def transition(values, raw, prefix='home_worker_'):
    stage, state, code = raw.get('stage'), raw.get('state'), raw.get('code')
    stage = stage if stage in STAGES else 'invalid'
    state = state if state in STATES else 'invalid'
    code = code if code is None or isinstance(code, str) and re.fullmatch(prefix + r'[a-z0-9_]{1,80}', code) else 'invalid'
    value = (stage, state, code)
    if len(values) < 64 and (not values or values[-1] != value):
        values.append(value)


def written_after(record, since):
    try:
        return datetime.fromisoformat(record['updated_at']).timestamp() > since
    except (KeyError, TypeError, ValueError):
        return False


def disruption_record(record, prefix='home_worker_', since=None):
    # Only the restarted Controller's fresh writes describe what its Owner sees.
    if since is not None and not written_after(record, since):
        return
    if prefix == 'home_worker_':
        check_ok = bool(record.get('state') in {'running', 'waiting', 'login_ready', 'ready'})
        assert check_ok, 'home_worker_disruption_showed_failure'
    else:
        check_ok = bool(record.get('state') in {'running', 'waiting', 'starting', 'healthy'})
        assert check_ok, 'bridge_disruption_showed_failure'


def recovered(record, state, stage, since):
    # A record persisted before the disruption is not recovery; it needs a later App observation.
    try:
        fresh = datetime.fromisoformat(record['observed_at']).timestamp() > since
    except (KeyError, TypeError, ValueError):
        fresh = False
    return fresh and record.get('state') == state and record.get('stage') == stage and record.get('enabled') is True


def login_ready_contract(raw, url, slug):
    # No raw input is an assertion operand, even with pytest rewriting enabled. Codes stay literal for evidence.
    def field(key, expected):
        value = raw.get(key)
        return type(value) is type(expected) and value == expected

    check_ok = bool(field('state', 'login_ready') and field('stage', 'login'))
    assert check_ok, 'home_worker_status_state'
    check_ok = bool(field('managed', True) and field('installed_by_us', True) and field('enabled', True)
                    and field('slug', slug) and field('runtime_location', 'haos_app'))
    assert check_ok, 'home_worker_status_ownership'
    check_ok = bool(field('credential_state', 'needs_auth') and field('pair_restarts', 0))
    assert check_ok, 'home_worker_status_pairing'
    check_ok = bool(field('release', {'repository': url, 'app': 'agentic_runner', 'version': VERSION}))
    assert check_ok, 'home_worker_status_release'
    fingerprint = raw.get('fingerprint')
    valid = isinstance(fingerprint, str) and re.fullmatch(r'[a-f0-9]{64}', fingerprint) is not None
    check_ok = bool(valid)
    assert check_ok, 'home_worker_status_fingerprint'
    login = raw.get('login')
    show = login.get('show') if isinstance(login, dict) else None
    check_ok = bool(show is True)
    assert check_ok, 'home_worker_status_login'
    absent = 'operation' not in raw
    check_ok = bool(absent)
    assert check_ok, 'home_worker_status_operation'


def persisted_record_contract(raw):
    allowed = isinstance(raw, dict) and set(raw) <= RECORD_KEYS
    check_ok = bool(allowed)
    assert check_ok, 'home_worker_record_unknown_key'
    clean = not any('pairing' in key.lower() for key in raw)
    check_ok = bool(clean)
    assert check_ok, 'home_worker_record_pairing_key'
    failed = raw.get('failed_code_hash')
    check_ok = bool(failed is None)
    assert check_ok, 'home_worker_record_failed_code_hash'


def sample(summary, supervisor, *apps, boot=False):
    summary['samples'] += 1
    supervisor = supervisor if supervisor in SUPERVISOR_STATES else 'unknown'
    summary['supervisor_states'] = sorted(set(summary['supervisor_states']) | {supervisor})
    key = 'app_stopped_while_supervisor_not_running' if boot else 'app_stopped_seen'
    summary[key] |= 'stopped' in apps and (not boot or supervisor != 'running')


def sample_summary(*, boot=False):
    return {'samples': 0, 'supervisor_states': [],
            'app_stopped_while_supervisor_not_running' if boot else 'app_stopped_seen': False}


def outer_script(public):
    # Retain the reviewed inference fixture, but remove manual bridge pairing.
    from test_bridge_haos import OUTER
    text = OUTER.replace("        options = app.state.ingress_bridge.prepare_pairing()\n        Path('/lab/pairing.json').write_text(json.dumps(options))\n", '')
    text = text.replace("        if settings['public']:\n            store.put", '        store.put')
    if not public:
        text = text.replace('async def main():',
            "from agentic_controller.bridge_maintenance import BridgeRelease\n"
            "from agentic_controller.home_worker_maintenance import HomeWorkerRelease\n"
            f"app.state.bridge_maintenance.release = BridgeRelease(repository={LOCAL_URL!r})\n"
            f"app.state.home_worker_maintenance.release = HomeWorkerRelease(repository={LOCAL_URL!r})\n"
            "assert app.state.bridge_maintenance.release.app == BridgeRelease().app\n"
            "assert app.state.bridge_maintenance.release.version == BridgeRelease().version\n"
            "assert app.state.home_worker_maintenance.release.app == HomeWorkerRelease().app\n"
            "assert app.state.home_worker_maintenance.release.version == HomeWorkerRelease().version\n"
            'async def main():')
    return text


def bool_map(names):
    return lambda v: isinstance(v, dict) and set(v) <= names and all(type(x) is bool for x in v.values())


def number(v):
    return type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 86400


def summaries(v):
    keys = {'samples', 'supervisor_states', 'app_stopped_seen', 'app_stopped_while_supervisor_not_running'}
    return (isinstance(v, dict) and set(v) <= keys and type(v.get('samples')) is int
        and 0 <= v['samples'] <= 10000 and isinstance(v.get('supervisor_states'), list)
        and all(x in SUPERVISOR_STATES for x in v['supervisor_states'])
        and all(type(x) is bool for k, x in v.items() if k.startswith('app_stopped')))


def transitions(prefix):
    return lambda v: (isinstance(v, list) and len(v) <= 64 and all(isinstance(x, (list, tuple)) and len(x) == 3
        and x[0] in STAGES and x[1] in STATES and (x[2] in (None, 'invalid') or isinstance(x[2], str)
        and re.fullmatch(prefix + r'[a-z0-9_]{1,80}', x[2])) for x in v))


def container_observation_valid(value):
    if not isinstance(value, dict):
        return False
    fields = {
        'apparmor_profile': lambda v: isinstance(v, str) and (v in {'unconfined', 'docker-default', '', 'unrecognized'}
            or re.fullmatch(r'[a-f0-9]{8}_agentic_runner', v) is not None),
        'cap_add': lambda v: v in ([], ['SYS_ADMIN'], ['CAP_SYS_ADMIN']),
        'device_cgroup_rules': lambda v: isinstance(v, list) and len(v) <= 2 and all(x in {'c 10:229 rwm', 'a *:* rwm'} for x in v),
        'privileged': lambda v: type(v) is bool,
        'host_pid': lambda v: type(v) is bool,
        'host_network': lambda v: type(v) is bool,
        'state': lambda v: isinstance(v, dict) and set(v) <= {'Running', 'Paused', 'Restarting', 'OOMKilled', 'Dead', 'Status', 'ExitCode'}
            and all((k == 'Status' and x in {'created', 'running', 'paused', 'restarting', 'removing', 'exited', 'dead'})
                or (k == 'ExitCode' and type(x) is int and -255 <= x <= 255)
                or (k not in {'Status', 'ExitCode'} and type(x) is bool) for k, x in v.items()),
    }
    for name in ('unknown_cap_count', 'unrecognized_device_rule_count', 'direct_device_mapping_count'):
        fields[name] = lambda v: type(v) is int and 0 <= v <= 1000
    return set(value) <= set(fields) and all(fields[k](v) for k, v in value.items())


def lifecycle_valid(value):
    if not isinstance(value, dict) or not set(value) <= {'vm', 'outer', 'git'}:
        return False
    for observation in value.values():
        if not isinstance(observation, dict):
            return False
        for key, item in observation.items():
            if key in {'state_returncode', 'events_returncode'}:
                valid = type(item) is int and -255 <= item <= 255
            elif key in {'state_error_type', 'events_error_type'}:
                valid = item in {'TimeoutExpired', 'OSError', 'ValueError', 'JSONDecodeError', 'KeyError', 'TypeError'}
            elif key == 'state':
                valid = isinstance(item, dict) and set(item) <= {'Running', 'Paused', 'Restarting', 'OOMKilled',
                    'Dead', 'Pid', 'ExitCode', 'Status'} and all(
                    (k == 'Status' and x in {'created', 'running', 'paused', 'restarting', 'removing', 'exited', 'dead'})
                    or (k in {'Pid', 'ExitCode'} and type(x) is int and -255 <= x < 2**31)
                    or (k not in {'Pid', 'ExitCode', 'Status'} and type(x) is bool) for k, x in item.items())
            elif key == 'events':
                valid = isinstance(item, list) and len(item) <= 256 and all(isinstance(x, dict)
                    and set(x) == {'action', 'timeNano'} and x['action'] in {'create', 'start', 'die', 'destroy', 'kill', 'stop'}
                    and type(x['timeNano']) is int and 0 <= x['timeNano'] < 10**20 for x in item)
            else:
                valid = False
            if not valid:
                return False
    return True


FIELDS = {
    'lifecycle_before_cleanup': lifecycle_valid,
    'kind': lambda v: v == 'real_haos_home_worker_enable',
    'observed_from': lambda v: v == 'supervisor_git_store_outer_controller_and_app_container',
    'target': lambda v: v == 'disposable_official_haos_vm',
    'method': lambda v: v == 'one_click_enable_cold_partial_backup_full_host_reboot',
    'observed_at_epoch': lambda v: type(v) in (int, float) and math.isfinite(v) and 0 < v < 10**11,
    'haos_image': lambda v: v == '18.3',
    'haos_vm_profile': lambda v: isinstance(v, dict) and set(v) == {'memory_mib', 'vcpus', 'disk_gib'}
        and all(type(x) is int and 0 < x < 65536 for x in v.values()),
    'app_container_observation': container_observation_valid,
    'preloaded_images': lambda v: isinstance(v, list) and len(v) == 3 and v[0] == BUILDER and v[2] == PYTHON
        and isinstance(v[1], str) and re.fullmatch(r'node:[a-z0-9.-]+@sha256:[a-f0-9]{64}', v[1]) is not None,
    'delivery': lambda v: v in ('local_git_daemon_exact_head_stable_branch', 'published_stable_branch'),
    'status': lambda v: v in ('running', 'passed', 'failed', 'interrupted'),
    'phase': lambda v: v in PHASES,
    'limitations': lambda v: v in (list(LIMITATIONS), [*LIMITATIONS, 'local_git_transport_not_github']),
    'checks': bool_map(CHECK_NAMES), 'cleanup': bool_map(CLEANUP_NAMES),
    'source_commit': lambda v: isinstance(v, str) and HEX40.fullmatch(v) is not None,
    'error_type': lambda v: v in ('AssertionError', 'HAError', 'TimeoutError', 'ClientError', 'BaseException', 'CancelledError', 'Exception', 'OSError', 'PermissionError', 'FileNotFoundError',
        'ModuleNotFoundError', 'ImportError', 'KeyError', 'ValueError', 'TimeoutExpired', 'KeyboardInterrupt'),
    'error_code': lambda v: v in failure_codes(),
    'enable_seconds': number, 'duration_seconds': number,
    'failure_hold_seconds': lambda v: type(v) is int and 0 <= v <= 180,
    'cold_backup_observation': summaries, 'reboot_observation': summaries,
    'enable_transitions': transitions('home_worker_'),
    'home_backup_transitions': transitions('home_worker_'), 'bridge_backup_transitions': transitions('bridge_'),
    'home_recovery_transitions': transitions('home_worker_'), 'bridge_recovery_transitions': transitions('bridge_'),
    'preload_digest_resolution': bool_map({'builder_tag', 'node_digest', 'python_digest'}),
    'upstream_error': bool_map({'job_missing', 'update_required', 'upstream_unknown_error'}),
    'failure_app_states': lambda v: isinstance(v, dict) and set(v) <= {'home', 'bridge'}
        and all(x in ('started', 'stopped', 'error', 'unknown') for x in v.values()),
    'supervisor_log_signatures': bool_map({'update_required', 'job_missing', 'build_failure', 'registry_failure',
        'no_space', 'permission', 'network_failure', 'not_installed', 'unavailable'}),
}
for _key in ('runner_git_tree', 'bridge_git_tree', 'controller_git_tree'):
    FIELDS[_key] = FIELDS['source_commit']
for _key in ('core', 'supervisor'):
    FIELDS[_key] = lambda v: isinstance(v, str) and re.fullmatch(r'[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}', v) is not None


def failure_codes():
    # Literal codes only; dynamic private exception messages are never evidence.
    codes = {'unclassified_fixture_error'}
    for path in CODE_SOURCES:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Assert) and isinstance(node.msg, ast.Constant):
                codes.add(node.msg.value)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'AssertionError' and node.args and isinstance(node.args[0], ast.Constant):
                codes.add(node.args[0].value)
    return codes


def evidence_projection(raw):
    result = {}
    for key, value in raw.items():
        if key not in FIELDS:
            continue
        try:
            valid = bool(FIELDS[key](value))
        except (TypeError, ValueError):
            valid = False
        if valid:
            result[key] = value
    return result


def delivery_contract(repositories, info, record, url):
    repo = repository_slug(url)
    matches = [x for x in repositories if isinstance(x, dict) and x.get('source') == url]
    valid = len(matches) == 1 and matches[0].get('slug') == repo
    check_ok = bool(valid)
    assert check_ok, 'shared_repository_identity_invalid'
    slug, repository, version = info.get('slug'), info.get('repository'), info.get('version')
    check_ok = bool(slug == repo + '_agentic_bridge' and repository == repo and version == VERSION)
    assert check_ok, 'bridge_delivery_identity_invalid'
    valid = (record.get('managed') is True and record.get('slug') == slug and record.get('repository_id') == repo
        and record.get('release') == {'repository': url, 'app': 'agentic_bridge', 'version': VERSION})
    check_ok = bool(valid)
    assert check_ok, 'bridge_manager_identity_invalid'
    BridgeMaintenance(None, None, None).validate_app(info, {'slug': slug, 'repository_id': repo}, installed=True)


async def test_real_home_worker_enable_backup_reboot(tmp_path, request):
    if not request.config.getoption('--haos-vm'):
        pytest.skip('Real HAOS one-click enable needs --haos-vm; run heavy suites serially')
    check_ok = bool(str(tmp_path).startswith(str(Path.home() / 'agent-work/tmp/pytest')))
    assert check_ok, 'disk_basetemp_required'
    public = os.environ.get('AGENTIC_HOME_WORKER_PUBLIC') == '1'
    url = STABLE_REPOSITORY if public else LOCAL_URL
    repo = repository_slug(url)
    slug, bridge_slug = repo + '_agentic_runner', repo + '_agentic_bridge'
    label = uuid.uuid4().hex[:10]
    vm, outer, daemon, network = ('agentic-enable-' + x + '-' + label for x in ('vm', 'outer', 'git', 'net'))
    cleaner = 'agentic-enable-clean-' + label
    started, wall_started = time.monotonic(), time.time()
    hold_seconds = failure_hold_seconds(os.environ.get('AGENTIC_HOME_WORKER_ENABLE_FAILURE_HOLD_SECONDS', '0'))
    report = ROOT / 'evidence/home-worker-enable-haos.json'
    progress = ROOT / 'evidence/home-worker-enable-haos-progress.json'
    evidence = {'kind': 'real_haos_home_worker_enable', 'status': 'running', 'checks': {},
        'delivery': 'published_stable_branch' if public else 'local_git_daemon_exact_head_stable_branch',
        'limitations': list(LIMITATIONS) + ([] if public else ['local_git_transport_not_github']),
        'observed_from': 'supervisor_git_store_outer_controller_and_app_container',
        'target': 'disposable_official_haos_vm', 'method': 'one_click_enable_cold_partial_backup_full_host_reboot',
        'observed_at_epoch': wall_started, 'haos_image': '18.3'}
    ha, ssh = None, None

    def phase(name):
        evidence['phase'] = name
        progress.write_text(json.dumps(evidence_projection(evidence), indent=2) + '\n')

    def git(*args):
        return process(['git', '-C', str(ROOT), *args])

    def remote(command, *, timeout=30):
        return process([*ssh, command], timeout=timeout)

    def write_report():
        report.write_text(json.dumps(evidence_projection(evidence), indent=2) + '\n')

    async def pause(seconds):
        for _ in range(seconds):
            await asyncio.sleep(1)

    async def app_state(app_slug):
        raw = await ha.request('GET', f'/addons/{app_slug}/info', supervisor=True)
        state = raw.get('state')
        return state if state in ('started', 'stopped', 'startup', 'error') else 'unknown'

    def outer_alive():
        # The Controller under test: its exit fails now instead of after a long wait on a stale database.
        inspected = json.loads(docker('inspect', outer))[0]
        check_ok = bool(inspected['State']['Running'] is True)
        assert check_ok, 'outer_controller_exited'
        return inspected

    async def outer_ready():
        # Serving means the lifespan, with both maintenance loops, started; the host port changes on restart.
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as probe:
            for _ in range(120):
                ports = outer_alive()['NetworkSettings']['Ports']
                address = 'http://127.0.0.1:' + ports['8099/tcp'][0]['HostPort']
                with contextlib.suppress(aiohttp.ClientError, TimeoutError, ValueError):
                    async with probe.get(address + '/api/deployment', headers={'Host': 'localhost:8099'}) as response:
                        if response.status == 200 and (await response.json()).get('mode') == 'host':
                            return address
                await asyncio.sleep(1)
        raise AssertionError('outer_controller_not_ready')

    try:
        phase('prepare_exact_head')
        head = git('rev-parse', 'HEAD').decode().strip()
        evidence['source_commit'] = head
        for directory in ('runner', 'bridge', 'controller'):
            evidence[directory + '_git_tree'] = git('rev-parse', 'HEAD:' + directory).decode().strip()
        dirty = bool(git('status', '--porcelain', '--', 'controller/agentic_controller', 'controller/static',
                         'bridge', 'runner', 'repository.yaml').strip())
        check_ok = bool(not dirty)
        assert check_ok, 'candidate_source_uncommitted'
        if public:
            published = stable_commit(process(['git', 'ls-remote', 'https://github.com/teddashh/agentic-smart-home',
                                               'refs/heads/stable']))
            check_ok = bool(published == head)
            assert check_ok, 'published_stable_differs_from_head'
        else:
            bare = tmp_path / 'git/agentic-smart-home.git'
            bare.parent.mkdir()
            process(['git', 'init', '--bare', str(bare)])
            git('push', '--no-verify', '--quiet', str(bare), 'HEAD:refs/heads/stable')
            process(['git', '-C', str(bare), 'symbolic-ref', 'HEAD', 'refs/heads/stable'])
            delivered = process(['git', '-C', str(bare), 'rev-parse', 'refs/heads/stable']).decode().strip()
            check_ok = bool(delivered == head)
            assert check_ok, 'stable_branch_is_exact_head'
        evidence['checks']['exact_head'] = True
        profile = json.loads(git('show', 'HEAD:host/profiles/laptop-amd64.json'))
        size = {key: profile.get(key) for key in ('memory_mib', 'vcpus', 'disk_gib')}
        check_ok = bool(all(type(x) is int and x > 0 for x in size.values()))
        assert check_ok, 'laptop_vm_profile_invalid'
        evidence['haos_vm_profile'] = size
        config = yaml.safe_load(git('show', 'HEAD:runner/config.yaml'))
        backup_mode, boot_mode = config.get('backup'), config.get('boot')
        check_ok = backup_mode == 'cold' and boot_mode == 'auto'
        assert check_ok, 'home_worker_cold_backup_boot_contract_changed'
        bases = dockerfile_bases(git('show', 'HEAD:runner/Dockerfile').decode())
        providers = json.loads(git('show', 'HEAD:providers.lock.json'))
        check_ok = bool(bases == [providers['bases']['node'], providers['bases']['python']] and PYTHON in bases)
        assert check_ok, 'runner_dockerfile_bases_differ_from_lock'
        node, builder_tag = providers['bases']['node'], BUILDER.split('@', 1)[0]
        phase('local_images_present')
        for image in (BUILDER, node, PYTHON, CONTROLLER, *([] if public else ['agentic-home-runner:' + VERSION])):
            try:
                docker('image', 'inspect', '--format', '{{.Id}}', image)
            except AssertionError:
                raise AssertionError('pinned_image_not_present_locally') from None
        docker('save', '-o', str(tmp_path / 'bases.tar'), builder_tag, node, PYTHON, timeout=600)
        phase('prepare_disposable_disks')
        with lzma.open(ROOT / 'build/cache/haos_ova-18.3.qcow2.xz', 'rb') as src, (tmp_path / 'haos.qcow2').open('wb') as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        process(['ssh-keygen', '-t', 'ed25519', '-N', '', '-f', str(tmp_path / 'key')])
        (tmp_path / 'config').mkdir()
        shutil.copyfile(tmp_path / 'key.pub', tmp_path / 'config/authorized_keys')
        with (tmp_path / 'config.ext4').open('wb') as stream:
            stream.truncate(32 * 1024**2)
        process(['/usr/sbin/mkfs.ext4', '-q', '-F', '-L', 'CONFIG', '-d', str(tmp_path / 'config'), str(tmp_path / 'config.ext4')])
        docker('network', 'create', '--subnet', '192.168.177.0/24', '--gateway', '192.168.177.254', network)
        phase('boot_official_haos')
        # The shipped laptop profile's HAOS VM size, so the build sees a user's memory and CPUs.
        docker('run', '-d', '--name', vm, '--network', network, '--ip', '192.168.177.2',
            '--device', '/dev/kvm', '--memory', f"{size['memory_mib'] + 1024}m", '--cpus', str(size['vcpus'] + 1),
            '--pids-limit', '256', '-p', '127.0.0.1::8123', '-p', '127.0.0.1::2222', '-v', f'{tmp_path}:/lab',
            'agentic-home-builder:0.2.0', 'bash', '-ec',
            f"cp /usr/share/OVMF/OVMF_VARS_4M.fd /lab/nvram.fd\nqemu-img resize /lab/haos.qcow2 {size['disk_gib']}G\n"
            f"exec qemu-system-x86_64 -enable-kvm -cpu host -machine q35 -m {size['memory_mib']} -smp {size['vcpus']} "
            '-drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd '
            '-drive if=pflash,format=raw,file=/lab/nvram.fd '
            '-drive file=/lab/haos.qcow2,format=qcow2,if=none,id=disk '
            '-device virtio-scsi-pci -device scsi-hd,drive=disk '
            '-drive file=/lab/config.ext4,format=raw,if=virtio '
            '-netdev user,id=net,hostfwd=tcp:0.0.0.0:8123-:80,hostfwd=tcp:0.0.0.0:2222-:22222,hostfwd=tcp:0.0.0.0:8443-:8443 '
            '-device virtio-net-pci,netdev=net -display none -serial file:/lab/serial.log', timeout=600)
        ports = json.loads(docker('inspect', vm))[0]['NetworkSettings']['Ports']
        base = 'http://127.0.0.1:' + ports['8123/tcp'][0]['HostPort']
        ssh = ['ssh', '-i', str(tmp_path / 'key'), '-p', ports['2222/tcp'][0]['HostPort'], '-o', 'BatchMode=yes',
            '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=no', '-o', f'UserKnownHostsFile={tmp_path / "known_hosts"}',
            '-o', 'ConnectTimeout=5', 'root@127.0.0.1']
        if not public:
            phase('git_daemon')
            docker('run', '-d', '--name', daemon, '--network', network, '--ip', '192.168.177.3',
                '--user', f'{os.getuid()}:{os.getgid()}', '-e', 'HOME=/tmp', '--memory', '256m', '--cpus', '1',
                '--pids-limit', '64', '-v', f'{tmp_path / "git"}:/srv/git:ro', '--entrypoint', 'git',
                'agentic-home-runner:' + VERSION, 'daemon', '--reuseaddr', '--export-all', '--base-path=/srv/git',
                '--listen=0.0.0.0', '--port=9418', '/srv/git', timeout=600)
            for _ in range(30):
                try:
                    published = stable_commit(process(['git', 'ls-remote', LOCAL_URL.split('#')[0],
                                                       'refs/heads/stable'], timeout=5))
                    if published == head:
                        break
                except (AssertionError, subprocess.TimeoutExpired):
                    pass
                await asyncio.sleep(1)
            else:
                raise AssertionError('stable_branch_is_exact_head')
        evidence['checks']['stable_branch_is_exact_head'] = True
        # Bounds a hung request only: the shared test disk can stall writes in the VM and the Controller for seconds.
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30),
                                         cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
            for _ in range(240):
                try:
                    async with client.get(base + '/api/onboarding') as response:
                        if response.status == 200 and isinstance(await response.json(), list):
                            break
                except (aiohttp.ClientError, TimeoutError, ValueError):
                    pass
                await asyncio.sleep(3)
            else:
                raise AssertionError('haos_core_readiness_timeout')
            phase('native_disposable_owner')
            async with client.post(base + '/api/onboarding/users', json={'client_id': base + '/',
                    'name': 'Disposable Runner Build Owner', 'username': 'runner-build-owner',
                    'password': secrets.token_urlsafe(32), 'language': 'en'}) as response:
                assert response.status == 200, 'native_owner_creation_failed'
                onboarding = await response.json()
            async with client.post(base + '/auth/token', data={'grant_type': 'authorization_code',
                    'code': onboarding['auth_code'], 'client_id': base + '/'}) as response:
                assert response.status == 200, 'native_token_exchange_failed'
                credentials = await response.json()
            for step in ('core_config', 'analytics', 'integration'):
                fields = {'client_id': base + '/', 'redirect_uri': base + '/'} if step == 'integration' else {}
                async with client.post(base + '/api/onboarding/' + step, json=fields,
                        headers={'Authorization': 'Bearer ' + credentials['access_token']}) as response:
                    assert response.status == 200, 'native_onboarding_failed'
        ha = ProbeHA(credentials['access_token'], base + '/api', base.replace('http:', 'ws:') + '/api/websocket', external=True)
        await ha.start()
        owner = await ha.ws('auth/current_user')
        owner_confirmed = bool(await ha.owner(owner['id']))
        assert owner_confirmed, 'native_owner_not_confirmed'
        test_token = await ha.ws('auth/long_lived_access_token', client_name='Enable probe', lifespan=1)
        outer_token = await ha.ws('auth/long_lived_access_token', client_name='Enable outer', lifespan=1)
        await ha.close()
        ha = ProbeHA(test_token, base + '/api', base.replace('http:', 'ws:') + '/api/websocket', external=True)
        await ha.start()
        phase('supervisor_readiness')
        for _ in range(120):
            supervisor = await ha.request('GET', '/supervisor/info', supervisor=True)
            if supervisor.get('healthy') is True:
                break
            await asyncio.sleep(3)
        else:
            raise AssertionError('supervisor_not_healthy_no_automatic_update')
        evidence.update(core=(await ha.request('GET', '/config'))['version'], supervisor=supervisor['version'])
        evidence['checks']['supervisor_healthy'] = True
        phase('preload_builder_and_pinned_bases')
        for _ in range(5):
            # A probe that times out on a loaded host is only not ready yet.
            with contextlib.suppress(subprocess.TimeoutExpired):
                if subprocess.run([*ssh, 'true'], capture_output=True, timeout=15).returncode == 0:
                    break
            await asyncio.sleep(3)
        else:
            raise AssertionError('haos_ssh_readiness_timeout')
        with (tmp_path / 'bases.tar').open('rb') as stream:
            loaded = subprocess.run([*ssh, 'docker load'], stdin=stream, capture_output=True, timeout=600)
        assert loaded.returncode == 0, 'haos_base_preload_failed'
        evidence['preloaded_images'] = [BUILDER, node, PYTHON]
        evidence['checks']['pinned_bases_preloaded'] = True
        # Observation only: whether the loaded digests resolve locally on HAOS.
        # An unresolved digest would make the builder fetch that base live.
        evidence['preload_digest_resolution'] = {role: subprocess.run(
            [*ssh, 'docker image inspect --format ' + shlex.quote('{{.Id}}') + ' ' + shlex.quote(reference)],
            capture_output=True, timeout=30).returncode == 0
            for role, reference in (('builder_tag', builder_tag), ('node_digest', node), ('python_digest', PYTHON))}
        phase('outer_candidate_controller')
        access_key = secrets.token_urlsafe(32)
        identity = tmp_path / 'identity.json'
        identity.write_text(json.dumps({'owner': owner['id'], 'token': outer_token, 'access_key': access_key, 'public': public}))
        identity.chmod(0o600)
        (tmp_path / 'outer.py').write_text(outer_script(public))
        (tmp_path / 'host-socket').mkdir(mode=0o700)
        docker('run', '-d', '--name', outer, '--network', network, '--ip', '192.168.177.1',
            '--memory', '768m', '--cpus', '1', '--pids-limit', '128', '--user', f'{os.getuid()}:{os.getgid()}',
            '-p', '127.0.0.1::8099', '-e', 'PYTHONPATH=/app', '-e', 'AGENTIC_DATA=/lab/outer-data',
            '-e', 'AGENTIC_HOST_MODE=1', '-v', f'{tmp_path}:/lab',
            '-v', f'{tmp_path / "host-socket"}:/run/agentic-home',
            '-v', f'{ROOT / "controller/agentic_controller"}:/app/agentic_controller:ro',
            '-v', f'{ROOT / "controller/static"}:/app/static:ro', CONTROLLER, 'python', '/lab/outer.py', timeout=600)
        outer_url = await outer_ready()
        database = tmp_path / 'outer-data/controller.sqlite3'
        phase('bridge_auto_enroll')
        for _ in range((JOB_TIMEOUT + 600) // 5):
            outer_alive()
            record = setting(database, 'bridge_maintenance')
            stage, state = record.get('stage'), record.get('state')
            check_ok = bool(state not in FAILED)
            assert check_ok, 'bridge_auto_enroll_blocked'
            if stage == 'monitor' and state == 'healthy':
                break
            await asyncio.sleep(5)
        else:
            raise AssertionError('bridge_auto_enroll_timeout')
        info = await ha.request('GET', f'/addons/{bridge_slug}/info', supervisor=True)
        repositories = await ha.request('GET', '/store/repositories', supervisor=True)
        delivery_contract(repositories, info, record, url)
        evidence['checks']['bridge_auto_enroll'] = True
        for _ in range(45):
            outer_alive()
            seen = setting(database, 'ingress_bridge').get('service_last_seen')
            if seen:
                break
            await asyncio.sleep(2)
        check_ok = bool(bool(seen))
        assert check_ok, 'bridge_heartbeat_timeout'
        evidence['checks']['bridge_heartbeat'] = True
        headers = {'Host': 'localhost:8099', 'Origin': 'http://localhost:8099', 'Sec-Fetch-Site': 'same-origin'}
        # A hung Controller request, not latency: synchronous SQLite commits wait for the slow test disk.
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60),
                cookie_jar=aiohttp.CookieJar(unsafe=True), headers=headers) as client:
            async def status():
                async with client.get(outer_url + '/api/host/home-worker/maintenance') as response:
                    check_ok = bool(response.status == 200)
                    assert check_ok, 'home_worker_status_http_failed'
                    return await response.json()

            phase('home_worker_enable')
            async with client.post(outer_url + '/api/host/login', json={'key': access_key}) as response:
                check_ok = bool(response.status == 200)
                assert check_ok, 'outer_login_failed'
            async with client.get(outer_url + '/api/session') as response:
                check_ok = bool(response.status == 200)
                assert check_ok, 'outer_session_failed'
                session = await response.json()
            enable_started = time.monotonic()
            async with client.post(outer_url + '/api/host/home-worker/maintenance/authorize', json={},
                    headers={'X-Agentic-CSRF': session['csrf']}) as response:
                check_ok = bool(response.status == 200)
                assert check_ok, 'home_worker_authorize_failed'
                current = await response.json()
            state, stage = current.get('state'), current.get('stage')
            check_ok = bool(state == 'running' and stage == 'repository')
            assert check_ok, 'home_worker_authorize_contract'
            evidence['enable_transitions'] = []
            deadline = time.monotonic() + JOB_TIMEOUT + 600
            while True:
                transition(evidence['enable_transitions'], current)
                state, stage = current.get('state'), current.get('stage')
                check_ok = bool(state not in FAILED)
                assert check_ok, 'home_worker_enable_stopped'
                if state == 'login_ready' and stage == 'login':
                    break
                check_ok = bool(time.monotonic() < deadline)
                assert check_ok, 'home_worker_enable_timeout'
                await asyncio.sleep(5)
                outer_alive()
                current = await status()
            evidence['enable_seconds'] = round(time.monotonic() - enable_started, 3)
            # A normal enable only shows progress; any failure state would offer the Owner a needless repair.
            smooth = all(step[1] in {'running', 'waiting', 'login_ready'} for step in evidence['enable_transitions'])
            check_ok = bool(smooth)
            assert check_ok, 'home_worker_enable_showed_failure'
            evidence['checks']['home_worker_enable'] = True
            phase('login_ready_contract')
            login_ready_contract(current, url, slug)
            evidence['checks']['login_ready_contract'] = True
            repositories = await ha.request('GET', '/store/repositories', supervisor=True)
            matching = sum(isinstance(x, dict) and x.get('source') == url for x in repositories)
            check_ok = bool(matching == 1)
            assert check_ok, 'shared_repository_not_unique'
            evidence['checks']['shared_repository'] = True
            info = await ha.request('GET', f'/addons/{slug}/info', supervisor=True)
            real_app_contract(info, config, slug)
            options_state = pairing_options(info.get('options'))
            state, version, repository = info.get('state'), info.get('version'), info.get('repository')
            check_ok = bool(options_state == 'absent' and state == 'started' and version == VERSION and repository == repo)
            assert check_ok, 'home_worker_installed_identity_invalid'
            evidence['checks']['real_app_contract'] = True
            record = setting(database, 'home_worker_maintenance')
            persisted_record_contract(record)
            peer = setting(database, 'runner_haos')
            fingerprint = current.get('fingerprint')
            peer_host, peer_port, peer_pin = peer.get('host'), peer.get('port'), peer.get('fingerprint')
            check_ok = bool(peer_host == '192.168.177.2' and peer_port == 8443 and peer_pin == fingerprint)
            assert check_ok, 'home_worker_peer_mismatch'
            evidence['checks']['persisted_record'] = True
            probe = json.loads(remote('docker exec app_' + slug + ' python -c ' + shlex.quote(TLS_PROBE)))
            pin = probe.get('tls_sha256')
            check_ok = bool(pin == fingerprint)
            assert check_ok, 'home_worker_tls_fingerprint_mismatch'
            evidence['checks']['tls_fingerprint'] = True
            installed = json.loads(remote('docker inspect app_' + slug))[0]
            container_contract(installed, slug)
            evidence['app_container_observation'] = container_projection(installed, slug)
            evidence['checks']['container_contract'] = True
            phase('cold_backup')

            def identities():
                # A repair, re-pair or new pairing always changes one of these.
                home, bridge = setting(database, 'home_worker_maintenance'), setting(database, 'bridge_maintenance')
                return (tuple(home.get(k) for k in ('slug', 'fingerprint', 'pair_restarts')),
                        tuple(bridge.get(k) for k in ('slug', 'repair_count', 'repairs', 'pairing_id')))

            async def recover(name, since, seconds):
                # Both managers return from fresh observations; a failed state never clears by itself.
                homes = evidence.setdefault('home_' + name + '_transitions', [])
                bridges = evidence.setdefault('bridge_' + name + '_transitions', [])
                deadline = time.monotonic() + seconds
                while True:
                    outer_alive()
                    home, bridge = setting(database, 'home_worker_maintenance'), setting(database, 'bridge_maintenance')
                    transition(homes, home)
                    transition(bridges, bridge, 'bridge_')
                    disruption_record(home, since=reboot_since if name == 'recovery' else None)
                    disruption_record(bridge, 'bridge_', since=reboot_since if name == 'recovery' else None)
                    check_ok = bool(home.get('state') not in FAILED)
                    assert check_ok, 'home_worker_failed_after_disruption'
                    check_ok = bool(bridge.get('state') not in FAILED)
                    assert check_ok, 'bridge_failed_after_disruption'
                    check_ok = bool(home.get('stage') == 'login' and bridge.get('stage') == 'monitor')
                    assert check_ok, 'pairing_repeated_after_disruption'
                    home_ok = recovered(home, 'login_ready', 'login', since)
                    bridge_ok = recovered(bridge, 'healthy', 'monitor', since)
                    if home_ok and bridge_ok:
                        return
                    if time.monotonic() >= deadline:
                        check_ok = bool(home_ok)
                        assert check_ok, 'home_worker_not_recovered'
                        check_ok = bool(bridge_ok)
                        assert check_ok, 'bridge_not_recovered'
                    await asyncio.sleep(2)

            before = identities()
            # Like a household automatic backup: both cold Apps stop and restart inside Supervisor's freeze.
            backup = await ha.request('POST', '/backups/new/partial', {'name': 'Disposable enable cold backup',
                'addons': [slug, bridge_slug], 'homeassistant': False, 'folders': [], 'background': True},
                supervisor=True)
            job_id = backup.get('job_id')
            check_ok = bool(isinstance(job_id, str) and re.fullmatch(r'[a-f0-9-]{32,36}', job_id) is not None)
            assert check_ok, 'cold_backup_job_missing'
            summary = sample_summary()
            evidence['cold_backup_observation'] = summary
            deadline = time.monotonic() + 600
            while True:
                disruption_record(setting(database, 'home_worker_maintenance'))
                disruption_record(setting(database, 'bridge_maintenance'), 'bridge_')
                supervisor_state = (await ha.request('GET', '/info', supervisor=True)).get('state')
                runner_state, sidebar_state = await app_state(slug), await app_state(bridge_slug)
                sample(summary, supervisor_state, runner_state, sidebar_state)
                job = await ha.request('GET', '/jobs/' + job_id, supervisor=True)
                identity_ok, done, errors_empty = job.get('uuid') == job_id, job.get('done') is True, job.get('errors') == []
                check_ok = bool(identity_ok and errors_empty)
                assert check_ok, 'cold_backup_job_failed'
                if done:
                    break
                check_ok = bool(time.monotonic() < deadline)
                assert check_ok, 'cold_backup_timeout'
                await asyncio.sleep(1)
            # The job ends after both Apps restarted and Supervisor left freeze.
            await recover('backup', time.time(), 300)
            check_ok = bool(identities() == before)
            assert check_ok, 'maintenance_changed_apps_after_cold_backup'
            evidence['checks']['cold_backup'] = True
            phase('full_reboot')
            # The Controller keeps watching while HAOS stops, then restarts like the host's own reboot.
            with contextlib.suppress(*TRANSIENT):
                await ha.request('POST', '/host/reboot', supervisor=True, timeout=15)
            # Observe actual Core loss, rather than mistaking an old healthy reply for reboot.
            deadline, down = time.monotonic() + 180, False
            while time.monotonic() < deadline:
                try:
                    await ha.request('GET', '/config', timeout=3)
                except TRANSIENT:
                    down = True
                    break
                await asyncio.sleep(2)
            check_ok = bool(down)
            assert check_ok, 'core_reboot_down_not_observed'
            reboot_since = time.time()
            docker('restart', outer)
            homes = evidence['home_recovery_transitions'] = []
            bridges = evidence['bridge_recovery_transitions'] = []

            def reboot_sample():
                # The old Controller's last write during HAOS shutdown is not what the restarted one shows.
                home, bridge = setting(database, 'home_worker_maintenance'), setting(database, 'bridge_maintenance')
                for record, values, prefix in ((home, homes, 'home_worker_'), (bridge, bridges, 'bridge_')):
                    if written_after(record, reboot_since):
                        transition(values, record, prefix)
                        disruption_record(record, prefix)

            outer_url = await outer_ready()
            deadline = time.monotonic() + 600
            while True:
                outer_alive()
                reboot_sample()
                try:
                    await ha.request('GET', '/config', timeout=3)
                    break
                except TRANSIENT:
                    check_ok = bool(time.monotonic() < deadline)
                    assert check_ok, 'core_reboot_return_timeout'
                    await asyncio.sleep(2)
            summary = sample_summary(boot=True)
            evidence['reboot_observation'] = summary
            deadline = time.monotonic() + 600
            while True:
                outer_alive()
                reboot_sample()
                try:
                    supervisor_state = (await ha.request('GET', '/info', supervisor=True)).get('state')
                    runner_state, sidebar_state = await app_state(slug), await app_state(bridge_slug)
                    sample(summary, supervisor_state, runner_state, sidebar_state, boot=True)
                    if supervisor_state == 'running' and runner_state == sidebar_state == 'started':
                        break
                except TRANSIENT:
                    pass
                check_ok = bool(time.monotonic() < deadline)
                assert check_ok, 'apps_not_started_after_reboot'
                await asyncio.sleep(2)
            apps_up = time.time()
            containers = remote('docker inspect --format ' + shlex.quote('{{.Id}} {{.State.StartedAt}}')
                                + ' app_' + slug + ' app_' + bridge_slug)
            await recover('recovery', apps_up, 600)
            evidence['checks']['full_reboot'] = True
            # Stay past the bridge's fresh startup grace: no delayed repair or restart may follow.
            while time.time() < apps_up + STARTUP_GRACE + 30:
                await asyncio.sleep(5)
                await recover('recovery', apps_up, 60)
            check_ok = bool(identities() == before)
            assert check_ok, 'maintenance_changed_apps_after_reboot'
            check_ok = bool(remote('docker inspect --format ' + shlex.quote('{{.Id}} {{.State.StartedAt}}')
                                   + ' app_' + slug + ' app_' + bridge_slug) == containers)
            assert check_ok, 'app_restarted_after_reboot'
            probe = json.loads(remote('docker exec app_' + slug + ' python -c ' + shlex.quote(TLS_PROBE)))
            check_ok = bool(probe.get('tls_sha256') == fingerprint)
            assert check_ok, 'home_worker_reboot_tls_fingerprint_changed'
            info = await ha.request('GET', f'/addons/{slug}/info', supervisor=True)
            options_state = pairing_options(info.get('options'))
            check_ok = bool(options_state == 'absent')
            assert check_ok, 'home_worker_reboot_options_changed'
            # What the Owner sees after logging in again.
            async with client.post(outer_url + '/api/host/login', json={'key': access_key}) as response:
                check_ok = bool(response.status == 200)
                assert check_ok, 'outer_login_failed'
            login_ready_contract(await status(), url, slug)
            evidence['checks']['recovery_identity'] = True
        phase('completion')
        complete = set(evidence['checks']) == CHECK_NAMES and all(evidence['checks'].values())
        check_ok = bool(complete)
        assert check_ok, 'enable_checks_incomplete'
        evidence['status'] = 'passed'
    except BaseException as error:
        evidence.update(status='failed', **failure_receipt(error, source_files=CODE_SOURCES))
        evidence['upstream_error'] = upstream_flags(ha.last_error) if ha else {}
        evidence['lifecycle_before_cleanup'] = lifecycle_observation({'vm': vm, 'outer': outer, 'git': daemon}, wall_started)
        if ha:
            evidence['failure_app_states'] = {}
            for role, app_slug in (('home', slug), ('bridge', bridge_slug)):
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(10):
                        state = await app_state(app_slug)
                    evidence['failure_app_states'][role] = state if state in {'started', 'stopped', 'error'} else 'unknown'
        if ssh:
            with contextlib.suppress(Exception):
                evidence['app_container_observation'] = container_projection(
                    json.loads(remote('docker inspect app_' + slug, timeout=10))[0], slug)
        if ssh:
            try:
                logs = subprocess.run([*ssh, 'ha supervisor logs | tail -80'], capture_output=True, timeout=15)
                text = logs.stdout[-65536:].decode(errors='replace').lower()
                evidence['supervisor_log_signatures'] = {key: value in text for key, value in {
                    'update_required': 'needs to be updated', 'job_missing': 'job does not exist',
                    'build_failure': 'build failed', 'registry_failure': 'failed to resolve', 'no_space': 'no space left',
                    'permission': 'permission denied', 'network_failure': 'temporary failure', 'not_installed': 'not installed'}.items()}
            except Exception:
                evidence['supervisor_log_signatures'] = {'unavailable': True}
        interrupted = isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError))
        evidence['failure_hold_seconds'] = 0 if interrupted else hold_seconds
        write_report()
        if not interrupted:
            await pause(hold_seconds)
        raise
    finally:
        if ha:
            with contextlib.suppress(Exception):
                await ha.close()
        cleanup = {}
        for role, name in (('outer', outer), ('git', daemon), ('vm', vm)):
            try:
                subprocess.run(['docker', 'rm', '-fv', name], capture_output=True, timeout=30)
                check = subprocess.run(['docker', 'ps', '-aq', '--filter', 'name=^/' + name + '$'], capture_output=True, timeout=5)
                cleanup[role + '_removed'] = check.returncode == 0 and not check.stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                cleanup[role + '_removed'] = False
        try:
            owned = subprocess.run(['docker', 'run', '--rm', '--name', cleaner, '-v', f'{tmp_path}:/lab',
                'agentic-home-builder:0.2.0', 'chown', '-R', f'{os.getuid()}:{os.getgid()}', '/lab'], capture_output=True, timeout=60)
            subprocess.run(['docker', 'rm', '-fv', cleaner], capture_output=True, timeout=30)
            check = subprocess.run(['docker', 'ps', '-aq', '--filter', 'name=^/' + cleaner + '$'], capture_output=True, timeout=5)
            cleanup['disk_fixture_ownership_restored'] = owned.returncode == check.returncode == 0 and not check.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            cleanup['disk_fixture_ownership_restored'] = False
            with contextlib.suppress(Exception):
                subprocess.run(['docker', 'rm', '-fv', cleaner], capture_output=True, timeout=30)
        try:
            subprocess.run(['docker', 'network', 'rm', network], capture_output=True, timeout=15)
            check = subprocess.run(['docker', 'network', 'ls', '-q', '--filter', 'name=^' + network + '$'], capture_output=True, timeout=5)
            cleanup['network_removed'] = check.returncode == 0 and not check.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            cleanup['network_removed'] = False
        leaked = evidence['status'] == 'passed' and not all(cleanup.values())
        if leaked:
            evidence.update(status='failed', error_type='AssertionError', error_code='owned_enable_resources_not_removed')
        evidence.update(cleanup=cleanup, duration_seconds=round(time.monotonic() - started, 3))
        write_report()
        progress.unlink(missing_ok=True)
        assert bool(not leaked), 'owned_enable_resources_not_removed'


@pytest.mark.parametrize('url', [LOCAL_URL, STABLE_REPOSITORY, 'https://github.com/hassio-addons/repository'])
def test_repository_slug(url):
    assert re.fullmatch(r'[a-f0-9]{8}', repository_slug(url))
    assert repository_slug(url.upper()) == repository_slug(url)
    assert repository_slug('https://github.com/hassio-addons/repository') == 'a0d7b954'


def test_transition_recorder():
    values = []
    raw = {'stage': 'login', 'state': 'login_ready', 'code': 'home_worker_login_required'}
    transition(values, raw)
    transition(values, raw)
    assert len(values) == 1
    transition(values, {**raw, 'code': 'private/token'})
    assert values[-1][2] == 'invalid'
    for i in range(100):
        transition(values, {**raw, 'code': 'home_worker_' + str(i)})
    assert len(values) == 64
    bridge = []
    transition(bridge, {'stage': 'monitor', 'state': 'starting', 'code': 'bridge_heartbeat_pending'}, 'bridge_')
    transition(bridge, {'stage': 'configure', 'state': 'running', 'code': 'home_worker_login_required'}, 'bridge_')
    assert bridge == [('monitor', 'starting', 'bridge_heartbeat_pending'), ('configure', 'running', 'invalid')]
    assert evidence_projection({'bridge_recovery_transitions': bridge, 'home_recovery_transitions': bridge}) == {
        'bridge_recovery_transitions': bridge}


def test_recovery_needs_an_observation_after_the_disruption():
    record = {'state': 'healthy', 'stage': 'monitor', 'enabled': True, 'observed_at': '2026-10-13T18:00:10+00:00'}
    moment = datetime.fromisoformat('2026-10-13T18:00:00+00:00').timestamp()
    assert recovered(record, 'healthy', 'monitor', moment)
    assert not recovered(record, 'healthy', 'monitor', moment + 10)
    assert not recovered({**record, 'enabled': False}, 'healthy', 'monitor', moment)
    assert not recovered({**record, 'state': 'starting'}, 'healthy', 'monitor', moment)
    for observed in (None, 'private', 1760378410):
        assert not recovered({**record, 'observed_at': observed}, 'healthy', 'monitor', moment)
    assert not recovered({k: v for k, v in record.items() if k != 'observed_at'}, 'healthy', 'monitor', moment)


def ready_status():
    return {'state': 'login_ready', 'stage': 'login', 'managed': True, 'installed_by_us': True,
        'enabled': True, 'slug': repository_slug(LOCAL_URL) + '_agentic_runner',
        'release': {'repository': LOCAL_URL, 'app': 'agentic_runner', 'version': VERSION},
        'fingerprint': 'a' * 64, 'login': {'show': True}, 'credential_state': 'needs_auth',
        'runtime_location': 'haos_app', 'pair_restarts': 0}


def test_login_ready_accepts_realistic_status():
    raw = ready_status()
    login_ready_contract(raw, LOCAL_URL, raw['slug'])


@pytest.mark.parametrize('key', [*ready_status(), 'operation'])
def test_login_ready_rejects_each_wrong_field_without_secrets(key):
    private = ('fake_access_key', 'fake_token', 'fake_pairing_code', '/api/hassio_ingress/private')
    raw = {**ready_status(), 'private': list(private), key: {'secret': list(private)}}
    with pytest.raises(AssertionError) as caught:
        login_ready_contract(raw, LOCAL_URL, ready_status()['slug'])
    message = str(caught.value)
    assert all(value not in message for value in private)


@pytest.mark.parametrize('extra', [{'unknown': None}, {'pairing_code': None}, {'failed_code_hash': 'a' * 64}])
def test_persisted_record_rejects_unsafe_fields(extra):
    with pytest.raises(AssertionError):
        persisted_record_contract({'state': 'login_ready', **extra})


def test_persisted_record_accepts_cleared_pairing():
    persisted_record_contract({'state': 'login_ready', 'failed_code_hash': None, 'privilege_view': None})


def test_boot_and_backup_summary():
    backup, boot = sample_summary(), sample_summary(boot=True)
    for supervisor, app in [('freeze', 'stopped'), ('startup', 'stopped'), ('running', 'started')]:
        sample(backup, supervisor, app)
        sample(boot, supervisor, app, boot=True)
    assert backup == {'samples': 3, 'app_stopped_seen': True, 'supervisor_states': ['freeze', 'running', 'startup']}
    assert boot == {'samples': 3, 'app_stopped_while_supervisor_not_running': True,
                    'supervisor_states': ['freeze', 'running', 'startup']}
    running = sample_summary(boot=True)
    sample(running, 'running', 'stopped', boot=True)
    assert running['app_stopped_while_supervisor_not_running'] is False


def test_evidence_drops_unknown_and_private_values():
    result = evidence_projection({'status': 'passed', 'token': 'private', 'core': 'private',
        'source_commit': 'private', 'error_code': 'fake_private', 'checks': {'token': True},
        'enable_transitions': [('private', 'ready', None)], 'cold_backup_observation': {'token': 'private'}})
    assert result == {'status': 'passed'}
    assert evidence_projection({'checks': {name: True for name in CHECK_NAMES}})['checks']


@pytest.mark.parametrize('public', [False, True])
def test_outer_script(public):
    text = outer_script(public)
    compile(text, '<outer-fixture>', 'exec')
    assert ('BridgeRelease(repository=' in text) is (not public)
    assert ('HomeWorkerRelease(repository=' in text) is (not public)
    assert 'prepare_pairing' not in text
    assert "'bridge_auto_enroll':True" in text
    assert HOST_CONTRACT_FIXTURE in text


@pytest.mark.parametrize('raw,expected', [(('a' * 40 + '\trefs/heads/stable\n').encode(), 'a' * 40),
    (b'', None), (b'private refs/heads/stable', None), (('a' * 40 + '\trefs/heads/main\n').encode(), None),
    (('a' * 40 + '\trefs/heads/stable\n') .encode() * 2, None)])
def test_stable_ls_remote_parser(raw, expected):
    assert stable_commit(raw) == expected


def test_projection_rejects_malformed_nested_and_unhashable_values():
    result = evidence_projection({'status': {'token': 'private'}, 'delivery': [],
        'app_container_observation': {'env': 'SUPERVISOR_TOKEN=private'},
        'cold_backup_observation': {'samples': 1, 'supervisor_states': [{'token': 'private'}]}})
    assert result == {}


def test_failure_receipt_recognizes_enable_assertion_codes():
    for code in ('home_worker_failed_after_disruption', 'bridge_not_recovered', 'app_restarted_after_reboot',
                 'maintenance_changed_apps_after_cold_backup', 'home_worker_status_ownership',
                 'owned_enable_resources_not_removed', 'outer_controller_exited', 'outer_controller_not_ready',
                 'home_worker_enable_showed_failure', 'home_worker_disruption_showed_failure',
                 'bridge_disruption_showed_failure'):
        receipt = failure_receipt(AssertionError(code), source_files=CODE_SOURCES)
        assert receipt['error_code'] == code
        assert evidence_projection(receipt)['error_code'] == code
    receipt = failure_receipt(AssertionError('private-token-value'), source_files=CODE_SOURCES)
    assert 'private-token-value' not in json.dumps(evidence_projection(receipt))


@pytest.mark.parametrize('prefix,allowed', [
    ('home_worker_', {'running', 'waiting', 'login_ready', 'ready'}),
    ('bridge_', {'running', 'waiting', 'starting', 'healthy'}),
])
def test_disruption_records_only_allow_calm_states(prefix, allowed):
    for state in allowed:
        disruption_record({'state': state}, prefix)
    for state in {'unavailable', 'needs_reconcile', 'user_disabled', None}:
        with pytest.raises(AssertionError, match=prefix + 'disruption_showed_failure'):
            disruption_record({'state': state}, prefix)


@pytest.mark.parametrize('updated_at', [None, 'invalid', '1970-01-01T00:00:09+00:00', '1970-01-01T00:00:10+00:00'])
def test_disruption_ignores_records_before_controller_restart(updated_at):
    disruption_record({'state': 'unavailable', 'updated_at': updated_at}, since=10)


def test_disruption_checks_new_controller_writes():
    with pytest.raises(AssertionError, match='home_worker_disruption_showed_failure'):
        disruption_record({'state': 'unavailable', 'updated_at': '1970-01-01T00:00:11+00:00'}, since=10)
