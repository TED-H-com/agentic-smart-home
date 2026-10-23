"""Actual HAOS Home coding worker; opt-in VM, explicit fixed cloud transport.

No HA/Supervisor upgrade, dev-host networking change, or cloud credential is used.
The derived test App contains the exact product Runner layers/config/AppArmor and
one test helper. It is deliberately not called an unmodified release image.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import io
import json
import lzma
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
import uuid
from datetime import datetime
from pathlib import Path

import aiohttp
import pytest
import yaml
from agentic_controller.ha import HAError
from home_worker_haos_probe import (
    DIAGNOSTIC_CLIENT_SECONDS,
    ENTRY_CHECK_NAMES,
    FIXTURE_KEY,
    ModelFixture,
    command_diagnostics,
    failure_receipt,
    launcher_receipt,
    stderr_summary,
    stderr_summary_bytes,
)
from test_bridge_haos import BUILDER, ProbeHA, docker, lifecycle_observation, process

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'host'))
TERMINAL = {'succeeded', 'failed', 'cancelled', 'blocked', 'expired', 'interrupted'}
STATES = TERMINAL | {'accepted', 'queued', 'launching', 'dispatching', 'running', 'waiting_for_user',
                     'cancel_requested', 'stop_unconfirmed', 'needs_reconcile'}
HEX32 = re.compile(r'^[a-f0-9]{32}$')
HEX64 = re.compile(r'^[a-f0-9]{64}$')
REGISTRY = 'registry:3.0.0@sha256:6c5666b861f3505b116bb9aa9b25175e71210414bd010d92035ff64018f9457e'
# Hang bounds only. The slow shared test disk stalls Supervisor and Controller writes for tens of seconds;
# App start and restart must outlast the product's own 150 s wait.
APP_OPERATION_SECONDS = 300
JOB_STOP_SECONDS = 120
JOB_PROOF_SSH_SECONDS = DIAGNOSTIC_CLIENT_SECONDS + 15
REMOTE_STEPS = frozenset({'app_json', 'job_proof', 'set_fixture_mode', 'registry_readiness',
    'runner_image', 'registry_image', 'registry_create', 'runner_tag', 'registry_state', 'registry_start',
    'registry_manifest', 'app_container', 'app_image', 'app_readiness', 'host_namespaces', 'source_lock',
    'app_restart_generation', 'app_before_graceful_stop', 'app_before_abrupt_stop', 'app_kill',
    'app_after_kill', 'failure_app_inspect', 'failure_app_logs'})


def fixture_remote(ssh, command, evidence, *, step, timeout=30, input=None,
                   run=subprocess.run, clock=time.monotonic, checkpoint=lambda: None):
    """Run once; retain a fixed step and bounded stderr signatures on failure."""
    assert step in REMOTE_STEPS, 'fixture_remote_step_invalid'
    started = clock()
    record = {'step': step, 'timeout_seconds': timeout}
    try:
        result = run([*ssh, command], timeout=timeout, input=input, capture_output=True)
    except (subprocess.TimeoutExpired, OSError) as error:
        record.update(error_type=type(error).__name__ if type(error).__name__ in {
            'TimeoutExpired', 'OSError', 'PermissionError', 'FileNotFoundError'} else 'OSError',
            stderr=stderr_summary_bytes(getattr(error, 'stderr', None) or b''))
        result = None
    if result is not None and result.returncode == 0:
        return result.stdout
    if result is not None:
        record.update(returncode=result.returncode if type(result.returncode) is int
            and -255 <= result.returncode <= 255 else None,
            stderr=stderr_summary_bytes(result.stderr or b''))
    record['elapsed_seconds'] = round(clock() - started, 3)
    failures = evidence.setdefault('fixture_command_failures', [])
    if len(failures) < 64:
        failures.append(record)
    try:
        checkpoint()
    except OSError:
        record['checkpoint_failed'] = True
    # This fixed assertion contains no private command, path or subprocess text.
    raise AssertionError('fixture_remote_failed') from None


def failure_hold_seconds(value):
    if not re.fullmatch(r'(?:0|[1-9][0-9]{0,2})', value) or int(value) > 180:
        raise AssertionError('failure_hold_must_be_0_to_180_seconds')
    return int(value)


# A 500 that names blob unknown is the registry's reason, so that phrase wins.
_PUSH_HTTP_5XX = re.compile(
    r'(?:http(?:/\d(?:\.\d)?)?\s+5\d\d\b|status(?:\s+code)?[: ]+5\d\d\b|'
    r'\b5\d\d\b\s+(?:internal server error|bad gateway|service unavailable|gateway time-?out))')


def classify_registry_push_failure(output, *, timed_out=False):
    """One fixed code from captured push bytes. Those bytes never enter evidence or the assertion."""
    if timed_out:
        return 'timeout'
    if isinstance(output, bytes):
        text = output[:65536].decode(errors='replace').lower()
    elif isinstance(output, str):
        text = output[:65536].lower()
    else:
        return 'other'
    if 'connection refused' in text:
        return 'connection_refused'
    if 'blob unknown' in text or 'blob_unknown' in text:
        return 'blob_unknown'
    if (_PUSH_HTTP_5XX.search(text) or 'internal server error' in text or 'bad gateway' in text
            or 'service unavailable' in text or 'gateway timeout' in text or 'gateway time-out' in text):
        return 'http_5xx'
    if (any(phrase in text for phrase in ('connection reset', 'unexpected eof', 'broken pipe', 'closed network connection'))
            or re.search(r'\beof\b', text)):
        return 'connection_reset_or_eof'
    if 'timeout' in text or 'deadline exceeded' in text:
        return 'timeout'
    return 'other'


def registry_container_state(raw):
    """Running, Restarting, OOMKilled, Dead, ExitCode and Status only. No logs or error prose."""
    if not isinstance(raw, dict):
        return {}
    result = {key: raw[key] for key in ('Running', 'Restarting', 'OOMKilled', 'Dead') if type(raw.get(key)) is bool}
    if raw.get('Status') in {'created', 'running', 'paused', 'restarting', 'removing', 'exited', 'dead'}:
        result['Status'] = raw['Status']
    if type(raw.get('ExitCode')) is int and -255 <= raw['ExitCode'] <= 255:
        result['ExitCode'] = raw['ExitCode']
    return result


def container_projection(raw, slug):
    """Project inspect without environment, mounts, labels or arbitrary names."""
    known = {'CHOWN', 'DAC_OVERRIDE', 'DAC_READ_SEARCH', 'FOWNER', 'FSETID', 'KILL', 'SETGID', 'SETUID', 'SETPCAP',
        'LINUX_IMMUTABLE', 'NET_BIND_SERVICE', 'NET_BROADCAST', 'NET_ADMIN', 'NET_RAW', 'IPC_LOCK', 'IPC_OWNER',
        'SYS_MODULE', 'SYS_RAWIO', 'SYS_CHROOT', 'SYS_PTRACE', 'SYS_PACCT', 'SYS_ADMIN', 'SYS_BOOT', 'SYS_NICE',
        'SYS_RESOURCE', 'SYS_TIME', 'SYS_TTY_CONFIG', 'MKNOD', 'LEASE', 'AUDIT_WRITE', 'AUDIT_CONTROL', 'SETFCAP',
        'MAC_OVERRIDE', 'MAC_ADMIN', 'SYSLOG', 'WAKE_ALARM', 'BLOCK_SUSPEND', 'AUDIT_READ', 'PERFMON', 'BPF', 'CHECKPOINT_RESTORE'}
    host, state = raw.get('HostConfig', {}), raw.get('State', {})
    caps = host.get('CapAdd') or []
    safe_caps = [item for item in caps if isinstance(item, str) and item.removeprefix('CAP_') in known]
    profile = raw.get('AppArmorProfile')
    result = {'apparmor_profile': profile if profile in {slug, 'unconfined', 'docker-default', ''} else 'unrecognized',
        'cap_add': safe_caps, 'unknown_cap_count': len(caps) - len(safe_caps),
        'privileged': host.get('Privileged') if type(host.get('Privileged')) is bool else None,
        'host_pid': host.get('PidMode') == 'host', 'host_network': host.get('NetworkMode') == 'host'}
    rules = host.get('DeviceCgroupRules') or []
    result['device_cgroup_rules'] = [rule for rule in rules if rule in {'c 10:229 rwm', 'a *:* rwm'}]
    result['unrecognized_device_rule_count'] = len(rules) - len(result['device_cgroup_rules'])
    result['direct_device_mapping_count'] = len(host.get('Devices') or [])
    result['state'] = {key: state[key] for key in ('Running', 'Paused', 'Restarting', 'OOMKilled', 'Dead') if type(state.get(key)) is bool}
    if state.get('Status') in {'created', 'running', 'paused', 'restarting', 'removing', 'exited', 'dead'}:
        result['state']['Status'] = state['Status']
    if type(state.get('ExitCode')) is int and -255 <= state['ExitCode'] <= 255:
        result['state']['ExitCode'] = state['ExitCode']
    return result


def container_contract(raw, slug):
    value = container_projection(raw, slug)
    assert value['apparmor_profile'] == slug, 'actual_custom_apparmor_profile_required'
    assert value['privileged'] is False and not value['host_pid'], 'actual_container_privilege_changed'
    assert not value['host_network'], 'actual_container_host_network_forbidden'
    assert (value['unknown_cap_count'] == 0 and
        {item.removeprefix('CAP_') for item in value['cap_add']} == {'SYS_ADMIN'}), 'actual_capability_set_changed'
    assert (value['device_cgroup_rules'] == ['c 10:229 rwm'] and value['unrecognized_device_rule_count'] == 0
        and value['direct_device_mapping_count'] == 0), 'actual_fuse_device_cgroup_contract_changed'
    return value


def container_generation(raw):
    """Only the owned App's fixed Docker process-generation fields are public."""
    state = raw.get('State', {})
    identifier, started, pid = raw.get('Id'), state.get('StartedAt'), state.get('Pid')
    assert isinstance(identifier, str) and HEX64.fullmatch(identifier), 'app_container_identity_invalid'
    assert isinstance(started, str) and re.fullmatch(r'20[0-9]{2}-[0-9TZ:.-]{15,40}', started), 'app_start_generation_invalid'
    try:
        timestamp = datetime.fromisoformat(started.replace('Z', '+00:00')).timestamp()
    except ValueError:
        raise AssertionError('app_start_generation_invalid') from None
    assert timestamp > 0 and type(pid) is int and 0 <= pid < 2**31, 'app_start_generation_invalid'
    assert type(state.get('Running')) is bool, 'app_running_state_invalid'
    return {'container_id': identifier, 'started_at': started, 'started_at_epoch': timestamp,
            'host_pid': pid, 'running': state['Running']}


def verified_app_generation(raw, slug, expected_image):
    container_contract(raw, slug)
    assert raw.get('Name') == '/app_' + slug, 'restarted_app_name_changed'
    assert raw.get('Image') == expected_image, 'restarted_app_image_changed'
    return container_generation(raw)


def fresh_app_restart(previous, current, before, after, *, replacement_verified=False):
    """Namespace inodes can repeat; require a new real process plus Docker start."""
    old_ticks, new_ticks = previous.get('process_start_ticks'), current.get('process_start_ticks')
    return (type(old_ticks) is int and type(new_ticks) is int and 0 < old_ticks < new_ticks < 2**63
        and isinstance(previous.get('boot_id_sha256'), str) and HEX64.fullmatch(previous['boot_id_sha256']) is not None
        and current.get('boot_id_sha256') == previous['boot_id_sha256']
        and (before['container_id'] == after['container_id'] or replacement_verified)
        and before['running'] and after['running']
        and after['started_at_epoch'] > before['started_at_epoch'])


def copied_json(raw):
    assert len(raw) <= 140 * 1024, 'fixture_archive_limit'
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as archive:
        entries = archive.getmembers()
        assert len(entries) == 1 and entries[0].isfile() and entries[0].size <= 128 * 1024, 'fixture_archive_invalid'
        return json.loads(archive.extractfile(entries[0]).read())


def diagnostic_projection(raw):
    """Startup receipts have no authority to add arbitrary public fields."""
    from agentic_host.coding_sandbox import ERROR_CODES as SANDBOX_CODES
    from agentic_runner.home_launcher import ERROR_CODES
    result = {}
    enums = {'state': {'failed', 'ready'}, 'status': {'running', 'passed', 'failed'},
        'phase': {'initialize_home_runtime', 'preflight', 'quota', 'pid-cpu', 'memory', 'descendant'},
        'cleanup': {'passed', 'unconfirmed'}, 'observed_from': {'haos_app'},
        'read_status': {'missing', 'unreadable', 'invalid', 'oversize'},
        'mode': {'prepare', 'execute', 'supervise', 'fuse', 'unknown'},
        'error_type': {'BoundaryError', 'SandboxError', 'OSError', 'AssertionError', 'PermissionError', 'FileNotFoundError', 'Exception'}}
    for key, choices in enums.items():
        if isinstance(raw.get(key), str) and raw[key] in choices:
            result[key] = raw[key]
    startup_codes = {'home_app_root_required', 'home_private_record_invalid', 'home_private_root_required',
        'home_private_ancestor_required', 'home_init_before_threads_required', 'home_private_app_cgroup_required',
        'home_cgroup_controllers_unavailable', 'home_fuse_device_required', 'home_fuse_device_unavailable', 'home_unshare_unavailable',
        'home_mount_unavailable', 'home_cgroup_delegation_unavailable', 'home_runtime_identity_changed'}
    code = raw.get('error_code')
    if isinstance(code, str) and len(code) <= 128:
        fixed = failure_receipt(AssertionError(code), source_files=(Path(__file__),))['error_code']
        result['error_code'] = code if code in startup_codes | ERROR_CODES | SANDBOX_CODES else fixed
    result.update(command_diagnostics(raw))
    if type(raw.get('errno')) is int and 0 < raw['errno'] < 4096:
        result['errno'] = raw['errno']
    if type(raw.get('available')) is bool:
        result['available'] = raw['available']
    if isinstance(raw.get('launcher_diagnostic'), dict):
        result['launcher_diagnostic'] = diagnostic_projection(raw['launcher_diagnostic'])
    checks = {'actual_memory_oom_kill', 'descendant_cancel_and_cgroup_empty', 'actual_ext4_enospc',
              'actual_pid_limit', 'actual_cpu_throttling', 'worker_and_fuse_cgroups_reclaimed'}
    result['checks'] = {key: value for key, value in raw.get('checks', {}).items()
                        if key in checks and type(value) is bool} if isinstance(raw.get('checks'), dict) else {}
    if isinstance(raw.get('entry_boundary_checks'), dict):
        result['entry_boundary_checks'] = {key: value for key, value in raw['entry_boundary_checks'].items()
            if key in ENTRY_CHECK_NAMES and type(value) is bool}
    if isinstance(raw.get('last_job'), dict):
        result['last_job'] = job_projection(raw['last_job'])
        for key in ('oom_killed', 'cgroup_populated'):
            if type(raw['last_job'].get(key)) is bool:
                result['last_job'][key] = raw['last_job'][key]
        if type(raw['last_job'].get('exit_code')) is int and -255 <= raw['last_job']['exit_code'] <= 255:
            result['last_job']['exit_code'] = raw['last_job']['exit_code']
    signatures = set(stderr_summary_bytes(b'')) - {'scanned_bytes'}
    if isinstance(raw.get('stderr_summary'), dict):
        result['stderr_summary'] = {key: value for key, value in raw['stderr_summary'].items() if key in signatures and type(value) is bool}
        size = raw['stderr_summary'].get('scanned_bytes')
        if type(size) is int and 0 <= size <= 16384:
            result['stderr_summary']['scanned_bytes'] = size
    counter_keys = {'low', 'high', 'max', 'oom', 'oom_kill', 'oom_group_kill', 'sock_throttled',
        'usage_usec', 'user_usec', 'system_usec', 'nr_periods', 'nr_throttled', 'throttled_usec', 'nr_bursts',
        'burst_usec', 'populated', 'frozen'}
    for name in ('quota_counters', 'pid-cpu_counters', 'memory_counters', 'descendant_counters'):
        if isinstance(raw.get(name), dict):
            result[name] = {group: {key: count for key, count in values.items()
                if key in counter_keys and type(count) is int and 0 <= count <= 2**63}
                for group, values in raw[name].items()
                if group in {'memory.events', 'pids.events', 'cpu.stat', 'cgroup.events'} and isinstance(values, dict)}
    return result

OUTER = r'''
import asyncio,json,os
from pathlib import Path
import uvicorn
from agentic_controller.app import create_app
from agentic_controller.ha import HomeAssistant
from agentic_controller.store import Store
os.umask(0o077)
root=Path('/lab/outer-data');root.mkdir(mode=0o700,exist_ok=True)
identity=json.loads(Path('/lab/identity.json').read_text())
(root/'access-key').write_text(identity['access_key'])
store=Store(root/'controller.sqlite3');store.set_setting('owner',identity['owner'])
class InferenceFixture:
    def __init__(self):self.runs={}
    async def call(self,method,path,body=None,key=None):
        if path=='/v1/capabilities':return {'state':'ready','auth_verified_at':'fixture-only'}
        if path=='/v1/sessions':return {'session_id':'a'*32,**body}
        if path.endswith('/runs'):
            value={'run_id':body['run_id'],'state':'succeeded','answer':'Home worker integration fixture case'}
            self.runs[body['run_id']]=value;return value
        if path.startswith('/v1/runs/'):return self.runs.get(path.split('/')[3].split('?')[0],{'state':'cancelled'})
        return {'state':'configured','auth_verified_at':None}
ha=HomeAssistant(identity['token'],'http://192.168.177.2:8123/api',
    'ws://192.168.177.2:8123/api/websocket',external=True)
app=create_app(store=store,ha=ha,host_mode=True,hermes_client=InferenceFixture())
try:
    asyncio.run(uvicorn.Server(uvicorn.Config(app,host='0.0.0.0',port=8099,
        proxy_headers=False,access_log=False,log_level='warning')).serve())
finally:
    store.close()
'''


def outer_scope_observation(container):
    """Probe the actual outer container; exec failure is not negative DNS evidence."""
    value = json.loads(docker('exec', container, 'python', '-c',
        "import socket,json\ntry: socket.getaddrinfo('homeassistant',8123); value=True\nexcept OSError: value=False\nprint(json.dumps({'app_internal_dns_resolved':value}))",
        timeout=5))
    assert value['app_internal_dns_resolved'] is False, 'outer_scope_fixture_unexpected'
    return value


def saved_image_identity(path, reference):
    """Docker29 may expose an index as Id; compare exported OCI config bytes."""
    with tarfile.open(path, 'r:') as archive:
        manifest = archive.getmember('manifest.json')
        if not manifest.isfile() or manifest.size > 262144:
            raise AssertionError('saved_manifest_invalid')
        entries = json.load(archive.extractfile(manifest))
        matches = [item for item in entries if reference in item.get('RepoTags', [])]
        if len(matches) != 1:
            raise AssertionError('saved_image_identity_ambiguous')
        config = archive.getmember(matches[0]['Config'])
        if not config.isfile() or config.size > 2 * 1024**2:
            raise AssertionError('saved_config_invalid')
        raw = archive.extractfile(config).read()
        value = json.loads(raw)
        layers = value.get('rootfs', {}).get('diff_ids')
        if (not isinstance(layers, list) or not 1 <= len(layers) <= 100 or
                any(not isinstance(layer, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', layer) for layer in layers)):
            raise AssertionError('saved_rootfs_invalid')
        return {'config_digest': 'sha256:' + hashlib.sha256(raw).hexdigest(), 'layers': layers}


def job_projection(raw):
    """Never serialize arbitrary model fields, prompts, logs or private responses."""
    from agentic_host.coding_sandbox import ERROR_CODES as SANDBOX_CODES
    from agentic_runner.home_launcher import ERROR_CODES as LAUNCHER_CODES

    value = {}
    for key in ('id', 'job_id', 'case_id'):
        if isinstance(raw.get(key), str) and HEX32.fullmatch(raw[key]):
            value[key] = raw[key]
    if raw.get('state') in STATES:
        value['state'] = raw['state']
    if raw.get('phase') in STATES | {'collecting', 'verifying'}:
        value['phase'] = raw['phase']
    for key in ('stop_confirmed', 'execution_started', 'workspace_released'):
        if type(raw.get(key)) is bool:
            value[key] = raw[key]
    if raw.get('runtime_location') in {'haos_app', 'host_container'}:
        value['runtime_location'] = raw['runtime_location']
    value['artifact_count'] = len(raw.get('artifacts', [])) if isinstance(raw.get('artifacts'), list) else 0
    value['evidence_count'] = len(raw.get('evidence_ids', [])) if isinstance(raw.get('evidence_ids'), list) else 0
    for key in ('reason', 'error_code'):
        if isinstance(raw.get(key), str) and raw[key] in SANDBOX_CODES | LAUNCHER_CODES | {
                'coding_needs_auth', 'worker_unavailable', 'hard_boundary_unavailable',
                'coding_source_unavailable', 'execution_failed', 'verification_failed', 'writer_stop_unconfirmed',
                'broker_restarted', 'source_or_image_unavailable', 'checkpoint_export_failed',
                'coding_operation_rejected', 'verification_deadline_exhausted', 'worker_contract_mismatch',
                'setup_timeout', 'execution_timeout', 'verification_timeout', 'authorization_expired',
                'owner_cancelled', 'coding_execution_failed', 'coding_needs_reauth', 'coding_quota_reached',
                'coding_turn_limit', 'coding_provider_rejected', 'coding_provider_unavailable'}:
            value[key] = raw[key]
    if isinstance(raw.get('launch_failure'), dict):
        value['launch_failure'] = launcher_receipt(raw['launch_failure'])
    verification = raw.get('verification', {})
    if isinstance(verification, dict):
        verified = {}
        for key, allowed in {'state': {'passed', 'failed', 'running'},
                             'observed_from': {'independent_home_verifier', 'independent_host_verifier'},
                             'network': {'none'}, 'trusted_tests': {'pinned_baseline'}}.items():
            if verification.get(key) in allowed:
                verified[key] = verification[key]
        if isinstance(verification.get('patch_sha256'), str) and HEX64.fullmatch(verification['patch_sha256']):
            verified['patch_sha256'] = verification['patch_sha256']
        for key in ('exit_code',):
            if type(verification.get(key)) is int and -255 <= verification[key] <= 255:
                verified[key] = verification[key]
        if type(verification.get('stop_confirmed')) is bool:
            verified['stop_confirmed'] = verification['stop_confirmed']
        value['verification'] = verified
    return value


def finish_phase(evidence, started, now):
    phases = evidence.get('phase_timings', [])
    if phases and 'duration_seconds' not in phases[-1]:
        phases[-1]['duration_seconds'] = round(now - started - phases[-1]['started_seconds'], 3)


def begin_phase(evidence, value, started, now):
    finish_phase(evidence, started, now)
    evidence['phase'] = value
    evidence.setdefault('phase_timings', []).append({'phase': value, 'started_seconds': round(now - started, 3)})


def workspace_reclaimed(proof):
    """A missing/incomplete diagnostic must never count as confirmed cleanup."""
    return (proof.get('record_present') is True and proof.get('stop_confirmed') is True
        and proof.get('state') == 'disposed'
        and not any(key.endswith('_observation_error') for key in proof)
        and not any(key in proof for key in ('cgroup_populated', 'fuse_cgroup_populated'))
        and all(proof.get(key) is False for key in (
            'workspace_mounted', 'workspace_image_present', 'cgroup_present', 'fuse_cgroup_present')))


async def observe_job(api, identifier, evidence, proof, *, stopped=True, timeout=280,
                      clock=time.monotonic, sleep=asyncio.sleep, checkpoint=lambda: None):
    """Keep elapsed time and read-only failure evidence before the client closes.

    This never cancels/retries a launch or clears a fence. Diagnostics have their
    own outcome so a failed read cannot replace the original timeout/failure.
    """
    started = clock()
    observation = {'job_id': identifier, 'target': 'stop_confirmed' if stopped else 'running',
        'timeout_seconds': timeout, 'state_changes': []}
    evidence.setdefault('job_waits', []).append(observation)

    def retain():
        try:
            checkpoint()
        except (OSError, ValueError) as error:
            observation['checkpoint_error'] = failure_receipt(error)

    async def diagnose():
        retain()
        diagnostic_started = clock()
        try:
            observation['boundary'] = proof(identifier)
        except Exception as diagnostic_error:
            observation['boundary_observation_error'] = failure_receipt(diagnostic_error, source_files=(Path(__file__),))
        try:
            workers = await asyncio.wait_for(api('GET', '/api/workers'), timeout=5)
            home = next((item for item in workers.get('workers', []) if item.get('worker_id') == 'home-coder'), {})
            observation['lease'] = {}
            if 'active_job_id' in workers:
                observation['lease'].update(exact_job_retained=workers['active_job_id'] == identifier,
                    no_active_job=workers['active_job_id'] is None)
            if type(home.get('available')) is bool:
                observation['lease']['worker_available'] = home['available']
        except Exception as diagnostic_error:
            observation['lease_observation_error'] = failure_receipt(diagnostic_error, source_files=(Path(__file__),))
        observation['diagnostic_seconds'] = round(clock() - diagnostic_started, 3)

    try:
        while clock() - started < timeout:
            result = await api('GET', '/api/worker/jobs/' + identifier)
            snapshot = job_projection(result)
            evidence['last_job'] = snapshot
            previous = observation['state_changes'][-1]['job'] if observation['state_changes'] else None
            if snapshot != previous:
                transition = {'elapsed_seconds': round(clock() - started, 3), 'job': snapshot}
                if len(observation['state_changes']) < 128:
                    observation['state_changes'].append(transition)
                else:
                    observation['state_changes'][-1] = transition
                    observation['state_changes_omitted'] = observation.get('state_changes_omitted', 0) + 1
            if result.get('stop_confirmed') is True if stopped else result.get('state') == 'running':
                observation.update(outcome='observed', elapsed_seconds=round(clock() - started, 3))
                if result.get('state') in {'failed', 'blocked', 'expired'}:
                    observation['job_failed'] = True
                    await diagnose()
                return result
            await sleep(.5)
        raise AssertionError('home_worker_state_timeout')
    except Exception as error:
        observation.update(outcome='failed', elapsed_seconds=round(clock() - started, 3),
            error=failure_receipt(error, source_files=(Path(__file__),)))
        await diagnose()
        raise
    finally:
        observation.setdefault('elapsed_seconds', round(clock() - started, 3))
        retain()


def artifact_bytes(metadata, result):
    assert result.get('id') == metadata['id'] and result.get('job_id') == metadata['job_id'], 'artifact_identity_changed'
    assert type(metadata.get('size')) is int and 0 <= metadata['size'] <= 180 * 1024, 'artifact_size_invalid'
    encoded = result.get('content_base64')
    assert isinstance(encoded, str) and len(encoded) <= 250000, 'artifact_encoding_invalid'
    raw = base64.b64decode(encoded, validate=True)
    assert len(raw) == metadata['size'] and hashlib.sha256(raw).hexdigest() == metadata['sha256'], 'artifact_digest_changed'
    return raw


async def require_source_job_success(job, case_id, evidence, api, proof):
    """Supplement a failed job with best-effort diagnostics, keeping its verdict."""
    evidence['source_job'] = job_projection(job)
    if job.get('state') != 'succeeded':
        observed = next((item for item in reversed(evidence.get('job_waits', []))
            if item.get('job_id') == job.get('id')), {})
        if 'boundary' in observed:
            evidence['source_job_boundary'] = observed['boundary']
        elif 'boundary_observation_error' in observed:
            evidence['source_job_boundary_observation_error'] = observed['boundary_observation_error']
        else:
            try:
                evidence['source_job_boundary'] = proof(job['id'])
            except Exception as error:
                evidence['source_job_boundary_observation_error'] = failure_receipt(error, source_files=(Path(__file__),))
        try:
            for meta in job.get('artifacts', []):
                if meta['name'] != 'home-boundaries.json':
                    continue
                content = await api('GET', '/api/worker/jobs/' + job['id'] + '/artifacts/' + meta['id'])
                checks = json.loads(artifact_bytes(meta, content)).get('checks', {})
                safe_keys = {'uid_unprivileged', 'supervisor_env_absent', 'hassio_env_absent', 'fixture_parent_env_absent',
                    'docker_socket_absent', 'host_socket_absent', 'role_auth_secret_unreadable',
                    'runner_key_secret_unreadable', 'private_canary_secret_unreadable', 'parent_environ_secret_unreadable',
                    'lan_direct_network_denied', 'public_direct_network_denied', 'tmp_on_bounded_volume',
                    'root_outside_volume_readonly', 'run_outside_volume_readonly',
                    'shm_outside_volume_readonly', 'dev_outside_volume_readonly', 'git_metadata_read_only',
                    'worker_pid_ns_distinct_from_app', 'worker_cgroup_ns_distinct_from_app', 'worker_mnt_ns_distinct_from_app'} | ENTRY_CHECK_NAMES
                evidence['failed_job_boundary_checks'] = {key: checks[key] for key in safe_keys if type(checks.get(key)) is bool}
        except Exception as error:
            evidence['failed_job_boundary_observation_error'] = failure_receipt(error, source_files=(Path(__file__),))
    if job.get('state') != 'succeeded' or job.get('case_id') != case_id:
        raise AssertionError('home_source_job_failed')


def stored_observation_projection(rows, case_id, base_commit):
    """Publish fixed comparison results, never stored identifiers or arbitrary content."""
    probes = [row for row in rows if row.get('collection_method') == 'fixed_read_only_app_probe']
    result = {'records_present': bool(rows), 'record_count': min(len(rows), 33),
        'same_case': bool(rows) and all(row.get('case_id') == case_id for row in rows),
        'all_from_haos_app': bool(rows) and all(row.get('observed_from') == 'haos_app' for row in rows),
        'single_fixed_probe': len(probes) == 1}
    if len(probes) != 1:
        return result
    observed = probes[0]
    content = observed.get('content') if isinstance(observed.get('content'), dict) else {}
    raw = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    core = content.get('core_api_from_app') if isinstance(content.get('core_api_from_app'), dict) else {}
    status = core.get('http_status')
    result['core_api_from_app'] = {key: core[key] if type(core.get(key)) is bool else None
        for key in ('dns_resolved', 'reachable', 'timed_out')}
    result['core_api_from_app']['http_status'] = status if type(status) is int and 100 <= status <= 599 else None
    port, attempted, endpoints = core.get('http_port'), core.get('attempted_ports'), core.get('endpoints')
    result['core_api_from_app']['http_port'] = port if type(port) is int and port in (80, 8123) else None
    result['core_api_from_app']['attempted_ports'] = attempted if attempted in ([], [80], [80, 8123]) else None
    safe_endpoints = None
    if isinstance(endpoints, list) and len(endpoints) <= 2:
        safe_endpoints = []
        for endpoint in endpoints:
            if not isinstance(endpoint, dict):
                safe_endpoints = None
                break
            port, status = endpoint.get('port'), endpoint.get('http_status')
            error = endpoint.get('error_code')
            safe_endpoints.append({
                'port': port if type(port) is int and port in (80, 8123) else None,
                'reachable': endpoint.get('reachable') if type(endpoint.get('reachable')) is bool else None,
                'http_status': status if type(status) is int and 100 <= status <= 599 else None,
                'error_code': error if error in (None, 'timeout', 'connection_failed', 'http_protocol_error') else None})
    result['core_api_from_app']['endpoints'] = safe_endpoints
    result.update(content_digest_matches=hashlib.sha256(raw).hexdigest() == observed.get('content_digest'),
        app_runtime_from_haos_app=isinstance(content.get('app_runtime'), dict)
            and content['app_runtime'].get('observed_from') == 'haos_app',
        limitations_present=isinstance(observed.get('limitations'), list) and bool(observed['limitations']),
        source_commit_matches=isinstance(observed.get('version_refs'), dict)
            and observed['version_refs'].get('source') == base_commit)
    return result


def app_contract(info, config, slug, *, configured=True):
    assert info.get('slug') == slug and info.get('version') == config['version'], 'supervisor_app_identity_changed'
    for key in ('hassio_api', 'homeassistant_api', 'docker_api', 'full_access', 'host_network', 'host_pid'):
        assert info.get(key) is False, 'supervisor_app_excess_permission'
    assert info.get('apparmor') == 'profile', 'supervisor_apparmor_policy_changed'
    assert sorted(info.get('privileged', [])) == sorted(config.get('privileged', [])), 'supervisor_capabilities_changed'
    assert config.get('devices') == ['/dev/fuse'], 'declared_fuse_device_contract_changed'
    assert info.get('devices') == ['/dev/fuse'], 'supervisor_fuse_device_contract_changed'
    assert info.get('options') == {}, 'supervisor_options_changed'
    assert type(info.get('auto_update')) is bool and type(info.get('watchdog')) is bool, 'supervisor_update_policy_invalid'
    if configured:
        assert info['auto_update'] is False and info['watchdog'] is False, 'supervisor_update_policy_changed'
    return {'slug': slug, 'version': info['version'], 'apparmor_policy': 'profile',
            'privileged': sorted(config.get('privileged', [])), 'no_ha_supervisor_or_docker_api': True,
            'host_pid': False, 'host_network': False, 'full_access': False, 'devices': ['/dev/fuse']}


async def observe_install(ha, slug, job_id, config, *, attempts=120, delay=5):
    """An ACK/transport loss is never a reason to repeat an installation effect."""
    for _ in range(attempts):
        confirmation = 'supervisor_job_and_app'
        try:
            value = await ha.request('GET', '/jobs/' + job_id, supervisor=True)
        except HAError:
            confirmation = 'app_postcondition_job_unavailable'
        else:
            assert value.get('uuid') == job_id and value.get('reference') == slug, 'install_job_identity_invalid'
            assert value.get('done') is None or type(value.get('done')) is bool, 'install_job_contract_invalid'
            assert isinstance(value.get('errors'), list), 'install_job_contract_invalid'
            if value['errors']:
                raise AssertionError('supervisor_app_install_failed')
            if value['done'] is not True:
                await asyncio.sleep(delay)
                continue
        listed = await ha.request('GET', '/addons', supervisor=True)
        if any(item.get('slug') == slug for item in listed['addons']):
            info = await ha.request('GET', f'/addons/{slug}/info', supervisor=True)
            app_contract(info, config, slug, configured=False)
            assert info['state'] == 'stopped', 'new_app_must_not_run_before_configured'
            return confirmation
        await asyncio.sleep(delay)
    raise AssertionError('supervisor_install_postcondition_timeout')


async def test_real_home_coding_worker_in_haos(tmp_path, request):
    if not request.config.getoption('--haos-vm'):
        pytest.skip('Actual Home Worker App needs --haos-vm; run heavy suites serially')
    assert str(tmp_path).startswith(str(Path.home() / 'agent-work/tmp/pytest'))
    config = yaml.safe_load((ROOT / 'runner/config.yaml').read_text())
    version = config['version']
    runner_image = os.environ.get('AGENTIC_HOME_TEST_RUNNER_IMAGE', 'agentic-home-runner:home-worker-dev')
    assert re.fullmatch(r'agentic-home-runner:[a-zA-Z0-9._-]{1,100}', runner_image), 'fixed_runner_image_required'
    controller_image = 'agentic-home-controller:' + version
    label = uuid.uuid4().hex[:10]
    vm, outer, network = ('agentic-home-worker-' + kind + '-' + label for kind in ('vm', 'outer', 'net'))
    slug, app_container = 'local_agentic_runner', 'app_local_agentic_runner'
    hold_seconds = failure_hold_seconds(os.environ.get('AGENTIC_HOME_TEST_FAILURE_HOLD_SECONDS', '0'))
    registry_container = 'agentic-home-test-registry'
    started, wall_started = time.monotonic(), time.time()
    report = ROOT / 'evidence/home-worker-haos.json'
    progress = ROOT / 'evidence/home-worker-haos-progress.json'
    evidence = {'kind': 'real_haos_home_coding_worker', 'status': 'running', 'observed_from': 'haos_app',
        'haos_image': '18.3', 'delivery': 'local_supervisor_derived_transport_fixture',
        'cloud': 'fixed_model_transport_no_real_cloud_account', 'source_task': 'documentation_only_patch_on_shipped_source',
        'not_acceptance': ['real_model_auth', 'autonomous_research_quality', 'physical_household', 'E2'], 'checks': {}}
    ha, ssh = None, None

    def phase(value):
        now = time.monotonic()
        begin_phase(evidence, value, started, now)
        progress.write_text(json.dumps({'phase': value, 'elapsed_seconds': round(now - started),
            'phase_timings': evidence['phase_timings']}, indent=2) + '\n')

    def remote(command, *, step, timeout=30, input=None):
        return fixture_remote(ssh, command, evidence, step=step, timeout=timeout, input=input,
            checkpoint=lambda: progress.write_text(json.dumps({'phase': evidence.get('phase'),
                'phase_timings': evidence.get('phase_timings', []),
                'fixture_command_failures': evidence.get('fixture_command_failures', [])}, indent=2) + '\n'))

    def app_json(path):
        assert re.fullmatch(r'/data/home-worker-fixture/[a-z-]+\.json', path)
        # Works for failed/stopped App processes; output remains byte bounded.
        raw = remote('docker cp ' + app_container + ':' + shlex.quote(path) + ' - | head -c 143361', step='app_json')
        return copied_json(raw)

    def stopped_transport():
        return app_json('/data/home-worker-fixture/transport.json')

    def job_proof(identifier):
        assert HEX32.fullmatch(identifier)
        command = 'docker exec ' + app_container + ' python -m home_worker_haos_probe job-proof ' + identifier
        value = json.loads(remote(command, step='job_proof', timeout=JOB_PROOF_SSH_SECONDS))
        expected = app_json('/data/home-worker-fixture/app-observation.json')['namespaces']
        assert value['diagnostic_namespace_ids'] == {name + '_ns': expected[name] for name in ('pid', 'cgroup', 'mnt')}, 'diagnostic_app_namespace_changed'
        return value

    def mode(value):
        assert value in {'source', 'cancel', 'restart', 'crash'}
        remote('docker exec -i ' + app_container + " sh -c 'cat > /data/home-worker-fixture/mode'",
               step='set_fixture_mode', input=value.encode())

    async def wait_registry_endpoint():
        # Empty JSON from /v2/ is the registry's own ready response. A failed exec is not ready yet.
        for _ in range(20):
            try:
                ready = json.loads(remote(
                    'docker exec ' + registry_container + ' wget -q -O - http://127.0.0.1:5000/v2/', step='registry_readiness', timeout=5))
                if ready == {}:
                    return
            except (AssertionError, ValueError):
                pass
            await asyncio.sleep(.25)
        raise AssertionError('disposable_registry_readiness_timeout')

    # upstream_flags lives in the Supervisor build test. A module-level import cycles:
    # that module imports the contracts defined above.
    from test_runner_supervisor_build import upstream_flags

    try:
        phase('prepare_disposable_disks')
        evidence['source_commit'] = process(['git', '-C', str(ROOT), 'rev-parse', 'HEAD']).decode().strip()
        evidence['runtime_source_uncommitted'] = bool(process(['git', '-C', str(ROOT), 'status', '--porcelain', '--',
            'runner', 'host/agentic_host/coding_service.py', 'host/agentic_host/coding_program.py', 'controller/agentic_controller']))
        base_image = json.loads(docker('image', 'inspect', runner_image))[0]
        evidence['runner_base_image_id'] = base_image['Id']
        evidence['controller_image_id'] = docker('image', 'inspect', controller_image, '--format', '{{.Id}}')
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
        docker('run', '-d', '--name', vm, '--network', network, '--ip', '192.168.177.2',
            '--device', '/dev/kvm', '--memory', '4096m', '--cpus', '3', '--pids-limit', '256',
            '-p', '127.0.0.1::8123', '-p', '127.0.0.1::2222', '-v', f'{tmp_path}:/lab',
            'agentic-home-builder:0.2.0', 'bash', '-ec',
            'cp /usr/share/OVMF/OVMF_VARS_4M.fd /lab/nvram.fd\nqemu-img resize /lab/haos.qcow2 64G\n'
            'exec qemu-system-x86_64 -enable-kvm -cpu host -machine q35 -m 3072 -smp 2 '
            '-drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd '
            '-drive if=pflash,format=raw,file=/lab/nvram.fd '
            '-drive file=/lab/haos.qcow2,format=qcow2,if=none,id=disk '
            '-device virtio-scsi-pci -device scsi-hd,drive=disk '
            '-drive file=/lab/config.ext4,format=raw,if=virtio '
            '-netdev user,id=net,hostfwd=tcp:0.0.0.0:8123-:80,hostfwd=tcp:0.0.0.0:2222-:22222,'
            'hostfwd=tcp:0.0.0.0:8443-:8443 -device virtio-net-pci,netdev=net -display none -serial file:/lab/serial.log', timeout=600)
        ports = json.loads(docker('inspect', vm))[0]['NetworkSettings']['Ports']
        base = 'http://127.0.0.1:' + ports['8123/tcp'][0]['HostPort']
        ssh = ['ssh', '-i', str(tmp_path / 'key'), '-p', ports['2222/tcp'][0]['HostPort'], '-o', 'BatchMode=yes',
            '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=no', '-o', f'UserKnownHostsFile={tmp_path / "known_hosts"}',
            '-o', 'ConnectTimeout=5', 'root@127.0.0.1']
        # Blocking SSH calls run inside this loop; a pooled connection could outlive uvicorn's 5 s keep-alive.
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=50), cookie_jar=aiohttp.CookieJar(unsafe=True),
                                         connector=aiohttp.TCPConnector(force_close=True)) as client:
            for _ in range(240):
                try:
                    async with client.get(base + '/api/onboarding', timeout=aiohttp.ClientTimeout(total=5)) as response:
                        if response.status == 200 and isinstance(await response.json(), list):
                            break
                except (aiohttp.ClientError, TimeoutError, ValueError):
                    pass
                await asyncio.sleep(3)
            else:
                raise AssertionError('haos_core_readiness_timeout')
            phase('native_disposable_owner')
            async with client.post(base + '/api/onboarding/users', json={'client_id': base + '/', 'name': 'Disposable Home Worker Owner',
                'username': 'home-worker-owner', 'password': secrets.token_urlsafe(32), 'language': 'en'}) as response:
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
            assert await ha.owner(owner['id']), 'native_owner_not_confirmed'
            phase('supervisor_readiness')
            for _ in range(120):
                supervisor = await ha.request('GET', '/supervisor/info', supervisor=True)
                if supervisor.get('healthy') is True:
                    break
                await asyncio.sleep(3)
            else:
                raise AssertionError('supervisor_not_healthy_no_automatic_update')
            evidence.update(core=(await ha.request('GET', '/config'))['version'], supervisor=supervisor['version'])
            phase('deliver_exact_runner_layers_and_test_transport')
            staging = tmp_path / 'runner-fixture'
            staging.mkdir()
            for name in ('config.yaml', 'apparmor.txt'):
                shutil.copyfile(ROOT / 'runner' / name, staging / name)
            shutil.copyfile(ROOT / 'tests/home_worker_haos_probe.py', staging / 'probe.py')
            (staging / 'fixture-marker').write_text('derived_model_transport_fixture_v1\n')
            evidence['app_config_sha256'] = hashlib.sha256((staging / 'config.yaml').read_bytes()).hexdigest()
            evidence['app_apparmor_sha256'] = hashlib.sha256((staging / 'apparmor.txt').read_bytes()).hexdigest()
            evidence['fixture_helper_sha256'] = hashlib.sha256((staging / 'probe.py').read_bytes()).hexdigest()
            docker('image', 'inspect', BUILDER)
            registry_host = json.loads(docker('image', 'inspect', REGISTRY))[0]
            docker('save', '-o', str(tmp_path / 'bases.tar'), runner_image, 'docker:29.7.2-cli', 'registry:3.0.0', timeout=600)
            base_identity = saved_image_identity(tmp_path / 'bases.tar', runner_image)
            registry_identity = saved_image_identity(tmp_path / 'bases.tar', 'registry:3.0.0')
            assert base_identity['layers'] == base_image['RootFS']['Layers'], 'exported_base_layers_changed'
            assert registry_identity['layers'] == registry_host['RootFS']['Layers'], 'exported_registry_layers_changed'
            evidence['runner_oci_config_digest'] = base_identity['config_digest']
            with (tmp_path / 'bases.tar').open('rb') as stream:
                result = subprocess.run([*ssh, 'docker load'], stdin=stream, capture_output=True, timeout=600)
            assert result.returncode == 0, 'haos_product_image_preload_failed'
            phase('disposable_loopback_registry')
            evidence['registry_step'] = 'inspect_preloaded'
            loaded_base = json.loads(remote('docker image inspect ' + shlex.quote(runner_image), step='runner_image'))[0]
            loaded_registry = json.loads(remote('docker image inspect registry:3.0.0', step='registry_image'))[0]
            assert loaded_base['RootFS']['Layers'] == base_identity['layers'], 'loaded_base_layers_changed'
            assert loaded_registry['RootFS']['Layers'] == registry_identity['layers'], 'loaded_registry_layers_changed'
            # 512m and 256 pids: multi-gigabyte layer uploads on the shared slow disk outgrew 128m/64.
            # This registry is loopback-only inside the disposable VM.
            evidence['registry_step'] = 'start_registry'
            remote('docker run -d --name ' + registry_container + ' --network host --memory 512m --cpus 1 '
                '--pids-limit 256 -e REGISTRY_HTTP_ADDR=127.0.0.1:5000 registry:3.0.0', step='registry_create')
            evidence['registry_step'] = 'registry_ready'
            await wait_registry_endpoint()
            evidence['registry_step'] = 'tag_base'
            local_tag = '127.0.0.1:5000/agentic-home-runner:fixture'
            remote('docker tag ' + shlex.quote(runner_image) + ' ' + local_tag, step='runner_tag')
            # The product is amd64. Do not publish a Docker29 index containing
            # attestations or rely on local image Id/RepoDigests semantics.
            # Push has its own known idempotent registry protocol and bounded retry.
            evidence['registry_step'] = 'push_base'
            evidence['registry_push_failures'] = []
            push_output = None
            for attempt in range(1, 4):
                evidence['registry_push_attempts'] = attempt
                if attempt > 1:
                    # Disk pressure can exit the registry. Start it only when it is not running,
                    # then use the same /v2/ probe as the first readiness wait.
                    running = False
                    with contextlib.suppress(AssertionError, ValueError, subprocess.TimeoutExpired):
                        raw = json.loads(remote(
                            "docker inspect --format '{{json .State}}' " + registry_container, step='registry_state', timeout=10))
                        running = registry_container_state(raw).get('Running') is True
                    if not running:
                        remote('docker start ' + registry_container, step='registry_start', timeout=30)
                    await wait_registry_endpoint()
                try:
                    completed = subprocess.run([*ssh, 'docker push --platform linux/amd64 ' + local_tag],
                                               capture_output=True, timeout=600)
                except subprocess.TimeoutExpired:
                    evidence['registry_push_failures'].append(classify_registry_push_failure(b'', timed_out=True))
                    continue
                if completed.returncode == 0:
                    push_output = completed.stdout
                    break
                # The error is on stderr; a long progress log must not push it past the classifier cap.
                captured = (completed.stderr or b'')[:32768] + b'\n' + (completed.stdout or b'')[:32768]
                evidence['registry_push_failures'].append(classify_registry_push_failure(captured))
            else:
                raise AssertionError('local_registry_push_failed')
            evidence['registry_step'] = 'inspect_manifest'
            digests = re.findall(r'digest: (sha256:[a-f0-9]{64})(?:\s|$)', push_output.decode())
            assert len(set(digests)) == 1, 'local_registry_manifest_identity_missing'
            refs = ['127.0.0.1:5000/agentic-home-runner@' + digests[-1]]
            registry_manifest = json.loads(remote('docker manifest inspect --insecure ' + refs[0], step='registry_manifest'))
            assert registry_manifest['config']['digest'] == base_identity['config_digest'], 'registry_config_digest_changed'
            evidence['registry_fixture'] = {'scope': 'disposable_haos_loopback_only', 'upstream': REGISTRY,
                'registry_config_digest': registry_identity['config_digest'], 'base_reference': refs[0],
                'base_config_digest_verified': True}
            evidence['registry_step'] = 'deliver_app_source'
            (staging / 'Dockerfile').write_text(f'FROM {refs[0]}\nCOPY probe.py /app/home_worker_haos_probe.py\n'
                'COPY fixture-marker /opt/home-worker-test-fixture\nCMD ["python","-m","home_worker_haos_probe","serve"]\n')
            with tarfile.open(tmp_path / 'runner.tar.gz', 'w:gz') as archive:
                archive.add(staging, arcname='agentic_runner')
            with (tmp_path / 'runner.tar.gz').open('rb') as stream:
                result = subprocess.run([*ssh, 'mkdir -p /mnt/data/supervisor/apps/local && tar xzf - -C /mnt/data/supervisor/apps/local'],
                    stdin=stream, capture_output=True, timeout=60)
            assert result.returncode == 0, 'haos_fixture_source_delivery_failed'
            # Issue #59: one store reload/read failed transiently and only HAError was kept.
            # Same bound as the Supervisor build catalog: 10 tries, 3 s apart, fixed flags only.
            reloaded, catalog, failures = False, None, []
            for attempt in range(1, 11):
                ha.last_error = None
                try:
                    if not reloaded:
                        evidence['registry_step'] = 'reload_store'
                        await ha.request('POST', '/store/reload', supervisor=True, timeout=60)
                        reloaded = True
                    evidence['registry_step'] = 'read_catalog'
                    catalog = await ha.request('GET', f'/store/addons/{slug}/info', supervisor=True)
                    break
                except (HAError, aiohttp.ClientError, TimeoutError) as error:
                    failures.append({'attempt': attempt, 'step': 'read' if reloaded else 'reload',
                        'error_type': type(error).__name__ if type(error) in (HAError, TimeoutError) else 'ClientError',
                        **upstream_flags(ha.last_error)})
                    if attempt < 10:
                        await asyncio.sleep(3)
            ha.last_error = None
            evidence['store_catalog'] = {'attempts': len(failures) + (catalog is not None), 'failures': failures}
            assert isinstance(catalog, dict), 'runner_store_catalog_unavailable'
            assert catalog.get('version_latest') == version and catalog.get('build') is True, 'runner_catalog_changed'
            evidence['registry_step'] = 'done'
            phase('official_supervisor_install')
            receipt = await ha.request('POST', f'/store/addons/{slug}/install', {'background': True}, supervisor=True)
            evidence['install_confirmation'] = await observe_install(ha, slug, receipt['job_id'], config)
            await ha.request('POST', f'/addons/{slug}/options', {'options': {}, 'boot': 'auto', 'watchdog': False,
                'auto_update': False}, supervisor=True)
            await ha.request('POST', f'/addons/{slug}/start', supervisor=True, timeout=APP_OPERATION_SECONDS)
            info = await ha.request('GET', f'/addons/{slug}/info', supervisor=True)
            evidence['app_contract'] = app_contract(info, config, slug)
            installed = json.loads(remote('docker inspect ' + app_container, step='app_container'))[0]
            evidence['app_container_observation'] = container_projection(installed, slug)
            container_contract(installed, slug)
            evidence['actual_apparmor_profile'] = installed['AppArmorProfile']
            derived = json.loads(remote('docker image inspect ' + shlex.quote(installed['Image']), step='app_image'))[0]
            assert derived['RootFS']['Layers'][:len(base_image['RootFS']['Layers'])] == base_image['RootFS']['Layers'], 'runner_base_layers_changed'
            loaded_again = json.loads(remote('docker image inspect ' + shlex.quote(runner_image), step='runner_image'))[0]
            assert loaded_again['RootFS']['Layers'] == base_identity['layers'], 'runner_base_identity_changed'
            assert loaded_again['Id'] == loaded_base['Id'], 'loaded_base_identity_changed'
            evidence['derived_fixture_image_id'] = derived['Id']
            evidence['checks']['product_image_layers_verified'] = True
            phase('actual_app_identity')
            for _ in range(240):
                try:
                    pairing = app_json('/data/home-worker-fixture/pairing.json')
                    app_observation = app_json('/data/home-worker-fixture/app-observation.json')
                    break
                except (AssertionError, ValueError, tarfile.TarError):
                    try:
                        startup = app_json('/data/home-worker-fixture/startup.json')
                    except (AssertionError, ValueError, tarfile.TarError):
                        startup = {}
                    if startup.get('state') == 'failed':
                        evidence['startup_failure'] = diagnostic_projection(startup)
                        raise AssertionError('runner_fixture_initialization_failed') from None
                    state = json.loads(remote('docker inspect ' + app_container, step='app_readiness'))[0]
                    evidence['app_container_observation'] = container_projection(state, slug)
                    if not state['State']['Running']:
                        raise AssertionError('runner_fixture_process_exited') from None
                    await asyncio.sleep(1)
            else:
                raise AssertionError('runner_fixture_start_timeout')
            host_ns = json.loads(remote("printf '{\"pid\":%s,\"cgroup\":%s}' \"$(stat -Lc %i /proc/self/ns/pid)\" \"$(stat -Lc %i /proc/self/ns/cgroup)\"", step='host_namespaces'))
            assert app_observation['namespaces']['pid'] != host_ns['pid'], 'app_shares_haos_pid_namespace'
            assert app_observation['namespaces']['cgroup'] != host_ns['cgroup'], 'app_shares_haos_cgroup_namespace'
            assert (app_observation['apparmor_enforcing'] and app_observation['fuse_device_present']
                and app_observation['fuse_device_openable']), 'app_boundary_unavailable'
            assert not app_observation['docker_socket_present'] and not app_observation['host_socket_present'], 'app_root_broker_socket_exposed'
            startup_boundaries = app_json('/data/home-worker-fixture/boundaries.json')
            evidence['startup_boundaries'] = startup_boundaries
            assert startup_boundaries['status'] == 'passed' and startup_boundaries['cleanup'] == 'passed', 'actual_boundary_limits_failed'
            evidence['app_observation'] = app_observation
            evidence['checks']['distinct_actual_haos_app_namespaces'] = True
            phase('outer_controller_real_tls_pairing')
            identity = tmp_path / 'identity.json'
            access_key = secrets.token_urlsafe(32)
            identity.write_text(json.dumps({'owner': owner['id'], 'token': credentials['access_token'], 'access_key': access_key}))
            identity.chmod(0o600)
            (tmp_path / 'outer.py').write_text(OUTER)
            # This outer fixture uses numeric-IP HA/Runner connections. Keep its
            # negative App-DNS comparison independent of host DNS/search domains.
            docker('run', '-d', '--name', outer, '--network', network, '--ip', '192.168.177.1',
                '--dns', '127.0.0.1', '--dns-search', '.', '--dns-option', 'timeout:1', '--dns-option', 'attempts:1',
                '--memory', '768m', '--cpus', '1', '--pids-limit', '128', '--user', f'{os.getuid()}:{os.getgid()}',
                '-p', '127.0.0.1::8099', '-e', 'PYTHONPATH=/app', '-e', 'AGENTIC_DATA=/lab/outer-data', '-e', 'AGENTIC_HOST_MODE=1',
                '-v', f'{tmp_path}:/lab', controller_image, 'python', '/lab/outer.py', timeout=600)
            evidence['outer_scope_preflight'] = outer_scope_observation(outer)
            outer_ports = json.loads(docker('inspect', outer))[0]['NetworkSettings']['Ports']
            origin = 'http://127.0.0.1:' + outer_ports['8099/tcp'][0]['HostPort']
            # The random host port is only a disposable transport forward.
            # Preserve the product's fixed local Web origin/Host contract.
            client.headers['Host'] = '127.0.0.1:8099'
            client.headers['Origin'] = 'http://127.0.0.1:8099'

            async def api(method, path, body=None, *, expected=200, key=None):
                headers = {'Idempotency-Key': key} if key else {}
                async with client.request(method, origin + path, json=body, headers=headers) as response:
                    assert response.status == expected, 'controller_' + method.lower() + '_status_' + str(response.status)
                    return await response.json()

            for _ in range(60):
                try:
                    await api('POST', '/api/host/login', {'key': access_key})
                    break
                except (aiohttp.ClientError, TimeoutError):
                    await asyncio.sleep(1)
            else:
                raise AssertionError('outer_controller_readiness_timeout')
            client.headers['X-Agentic-CSRF'] = (await api('GET', '/api/session'))['csrf']
            assert (await api('POST', '/api/runtimes/haos/pair', pairing))['paired']
            evidence['checks']['outer_controller_actual_pinned_tls_pairing'] = True
            phase('independent_home_role_login')
            auth = await api('POST', '/api/workers/home-coder/providers/codex/auth',
                {'method': 'api_key', 'secret': FIXTURE_KEY}, expected=202, key='home-role-fixture-login')
            for _ in range(60):
                status = await api('GET', '/api/workers/home-coder/auth/' + auth['id'])
                if status.get('status') == 'connected':
                    break
                await asyncio.sleep(1)
            else:
                raise AssertionError('home_role_login_timeout')
            capabilities = await api('GET', '/api/workers')
            home = next(item for item in capabilities['workers'] if item['worker_id'] == 'home-coder')
            assert home['available'] and home['runtime_location'] == 'haos_app', 'home_worker_not_available'
            assert not next(item for item in capabilities['workers'] if item['worker_id'] == 'host-coder')['available']
            evidence['checks']['home_role_login_separate_from_unpaired_host'] = True
            phase('actual_case_and_home_source_task')
            message = await api('POST', '/api/gateway/messages', {'message': 'Inspect the Home App and document canonical serialization.',
                'runtime': 'host', 'provider': 'codex', 'coordinator': 'hermes'}, expected=202, key='home-fixture-case')
            for _ in range(60):
                messages = (await api('GET', '/api/gateway/messages'))['messages']
                current = next(item for item in messages if item['id'] == message['id'])
                if current.get('state') == 'succeeded' and current.get('case_id'):
                    break
                await asyncio.sleep(.5)
            else:
                raise AssertionError('fixture_hermes_case_timeout')
            case_id = current['case_id']
            body = {'worker_id': 'home-coder', 'mode': 'development', 'case_id': case_id,
                    'goal': 'Inspect App facts, document stable serialization in models.py, and run baseline tests.'}
            job = await api('POST', '/api/worker/jobs', body, expected=202, key='home-source-fixture')
            assert (await api('POST', '/api/worker/jobs', body, expected=202, key='home-source-fixture'))['id'] == job['id']

            async def wait_job(identifier, *, stopped=True, timeout=280):
                return await observe_job(api, identifier, evidence, job_proof, stopped=stopped, timeout=timeout,
                    checkpoint=lambda: report.write_text(json.dumps(evidence, indent=2) + '\n'))

            job = await wait_job(job['id'])
            await require_source_job_success(job, case_id, evidence, api, job_proof)
            assert job['verification']['state'] == 'passed', 'home_independent_verification_failed'
            assert job['verification']['observed_from'] == 'independent_home_verifier', 'home_verifier_location_wrong'
            assert job['evidence_ids'] and job['runtime_location'] == 'haos_app', 'home_evidence_missing'
            fetched = {}
            for artifact in job['artifacts']:
                reply = await api('GET', '/api/worker/jobs/' + job['id'] + '/artifacts/' + artifact['id'])
                fetched[artifact['name']] = artifact_bytes(artifact, reply)
            assert b'Serialize stable public contract fields' in fetched['changes.patch'], 'actual_source_patch_missing'
            assert hashlib.sha256(fetched['changes.patch']).hexdigest() == job['verification']['patch_sha256'], 'verified_patch_not_returned'
            boundary = json.loads(fetched['home-boundaries.json'])
            assert boundary['observed_from'] == 'haos_app' and all(value is True for value in boundary['checks'].values())
            assert ENTRY_CHECK_NAMES <= set(boundary['checks']), 'worker_entry_checks_missing'
            app_namespaces = {name + '_ns': app_observation['namespaces'][name] for name in ('pid', 'cgroup', 'mnt')}
            assert boundary['app_namespace_ids'] == app_namespaces, 'worker_observation_app_namespace_changed'
            worker_namespaces = boundary['worker_namespace_ids']
            assert set(worker_namespaces) == set(app_namespaces), 'worker_namespace_contract_invalid'
            assert all(type(value) is int and 0 < value < 2**63 and value != app_namespaces[name]
                for name, value in worker_namespaces.items()), 'worker_not_isolated_from_app_namespaces'
            verification = json.loads(fetched['verification.json'])
            assert verification['completion_verified'] and verification['tests_executed'] > 0, 'independent_tests_not_completed'
            transport = app_json('/data/home-worker-fixture/transport.json')['source']
            assert transport['requests'] >= 2 and transport['tool_output_received'] and transport['source_marker'] and transport['pytest_passed']
            evidence['checks'].update(actual_codex_tools=True, actual_source_patch=True, independent_baseline_tests=True,
                same_case_tls_artifacts=True, worker_boundary_checks=True)
            evidence['independent_tests_executed'] = verification['tests_executed']
            evidence['boundary_checks'] = boundary['checks']
            evidence['namespace_comparison'] = {'app': app_namespaces, 'worker': worker_namespaces,
                'haos_host': host_ns, 'worker_pid_cgroup_mount_distinct_from_app': True}
            evidence['model_fixture'] = transport
            phase('stored_same_case_observation')
            deadline = time.monotonic() + 30
            while True:
                with contextlib.closing(sqlite3.connect(f"file:{tmp_path / 'outer-data/controller.sqlite3'}?mode=ro", uri=True)) as database:
                    rows = [json.loads(row[0]) for row in database.execute(
                        "SELECT body FROM records WHERE kind='case_evidence' AND json_extract(body,'$.job_id')=?", (job['id'],))]
                stored = stored_observation_projection(rows, case_id, job['base_commit'])
                if stored['records_present']:
                    break
                assert time.monotonic() < deadline, 'home_observation_not_persisted'
                await asyncio.sleep(.25)
            evidence['stored_observation'] = stored
            assert stored['records_present'] and stored['same_case'], 'home_observation_case_mismatch'
            assert stored['all_from_haos_app'], 'home_observation_origin_mismatch'
            assert stored['single_fixed_probe'], 'home_fixed_observation_missing_or_duplicated'
            assert stored['content_digest_matches'], 'home_observation_digest_changed'
            core_view = stored['core_api_from_app']
            assert core_view['dns_resolved'], 'home_core_dns_unavailable'
            assert core_view['reachable'], 'home_core_api_unreachable'
            assert core_view['http_status'] == 401, 'home_core_api_not_unauthenticated_401'
            assert core_view['http_port'] == 80 and core_view['attempted_ports'] == [80], 'home_core_api_actual_port_mismatch'
            assert core_view['endpoints'] == [{'port': 80, 'reachable': True, 'http_status': 401,
                'error_code': None}], 'home_core_api_endpoint_receipt_mismatch'
            assert stored['app_runtime_from_haos_app'], 'home_runtime_observation_origin_mismatch'
            assert stored['limitations_present'], 'home_observation_limitations_missing'
            assert stored['source_commit_matches'], 'home_observation_source_mismatch'
            evidence['app_core_observation'] = core_view
            evidence['checks']['same_case_observation_content_and_digest'] = True
            source_lock = json.loads(remote('docker exec ' + app_container + ' cat /opt/agentic-worker/source.json', step='source_lock'))
            assert source_lock['commit'] == job['base_commit'] == startup_boundaries['source_commit']
            evidence['pinned_source'] = source_lock
            evidence['outer_comparison'] = outer_scope_observation(outer)
            evidence['checks']['additional_app_namespace_view'] = True
            checkpoint = await api('POST', '/api/worker/jobs/' + job['id'] + '/handoff', {})
            assert checkpoint['stop_confirmed'] and checkpoint['live_session'] is False
            assert any(artifact['id'] in checkpoint['checkpoint']['artifact_ids'] for artifact in job['artifacts'])
            evidence['checks']['fenced_checkpoint_handoff'] = True

            async def submit_held(kind):
                mode(kind)
                reply = await api('POST', '/api/worker/jobs', {'worker_id': 'home-coder', 'mode': 'research',
                    'case_id': case_id, 'goal': 'Hold the explicit disposable descendant probe.'}, expected=202, key='home-' + kind)
                await wait_job(reply['id'], stopped=False, timeout=180)
                deadline = time.monotonic() + JOB_STOP_SECONDS
                while time.monotonic() < deadline:
                    proof = job_proof(reply['id'])
                    if proof.get('pulse_present') and proof.get('cgroup_populated'):
                        return reply, proof
                    await asyncio.sleep(.25)
                raise AssertionError('actual_descendant_not_started')

            async def app_available():
                for _ in range(90):
                    values = await api('GET', '/api/workers')
                    home = next(item for item in values['workers'] if item['worker_id'] == 'home-coder')
                    if home['available']:
                        return values
                    await asyncio.sleep(1)
                raise AssertionError('home_restart_readiness_timeout')

            async def restarted_app(previous, before):
                for _ in range(90):
                    current = app_json('/data/home-worker-fixture/app-observation.json')
                    # Supervisor may remove/recreate the same App container.
                    # Exact image identity inherits the initial verified layer proof.
                    after = verified_app_generation(json.loads(remote('docker inspect ' + app_container, step='app_restart_generation'))[0],
                        slug, installed['Image'])
                    if fresh_app_restart(previous, current, before, after, replacement_verified=True):
                        await app_available()
                        return current, after
                    await asyncio.sleep(1)
                raise AssertionError('fresh_app_process_start_not_observed')

            async def reclaimed(identifier):
                for _ in range(40):
                    proof = job_proof(identifier)
                    if workspace_reclaimed(proof):
                        return proof
                    await asyncio.sleep(.25)
                raise AssertionError('stopped_workspace_not_reclaimed')

            phase('actual_tls_cancel_descendants')
            held, before = await submit_held('cancel')
            cancel_started = time.monotonic()
            await api('POST', '/api/worker/jobs/' + held['id'] + '/cancel', {}, expected=202)
            cancelled = await wait_job(held['id'], timeout=JOB_STOP_SECONDS)
            assert cancelled['state'] == 'cancelled' and cancelled['stop_confirmed']
            proof = await reclaimed(held['id'])
            assert (await api('GET', '/api/workers'))['active_job_id'] is None
            evidence['cancel'] = {'before': before, 'after': proof,
                                  'seconds': round(time.monotonic() - cancel_started, 3)}
            evidence['checks']['actual_tls_cancel_stops_descendants_and_releases_lease'] = True

            phase('graceful_supervisor_stop_and_restart')
            held, before = await submit_held('restart')
            previous = app_json('/data/home-worker-fixture/app-observation.json')
            generation = container_generation(json.loads(remote('docker inspect ' + app_container, step='app_before_graceful_stop'))[0])
            await ha.request('POST', f'/addons/{slug}/stop', supervisor=True, timeout=APP_OPERATION_SECONDS)
            stopped_app = await ha.request('GET', f'/addons/{slug}/info', supervisor=True)
            assert stopped_app['state'] == 'stopped', 'supervisor_stop_not_observed'
            await ha.request('POST', f'/addons/{slug}/start', supervisor=True, timeout=APP_OPERATION_SECONDS)
            current, after = await restarted_app(previous, generation)
            stopped = await wait_job(held['id'], timeout=JOB_STOP_SECONDS)
            assert stopped['state'] in {'cancelled', 'interrupted'} and stopped['stop_confirmed'], 'graceful_worker_stop_unconfirmed'
            assert (await api('GET', '/api/workers'))['active_job_id'] is None, 'graceful_worker_lease_not_released'
            evidence['graceful_restart'] = {'before': before, 'job': job_projection(stopped),
                'generation_before': generation, 'generation_after': after,
                'process_start_ticks_before': previous['process_start_ticks'],
                'process_start_ticks_after': current['process_start_ticks']}
            evidence['checks']['graceful_app_restart_keeps_confirmed_stop_without_replay'] = True

            phase('abrupt_app_restart_keeps_unknown_writer_fenced')
            held, before = await submit_held('crash')
            previous = app_json('/data/home-worker-fixture/app-observation.json')
            generation = container_generation(json.loads(remote('docker inspect ' + app_container, step='app_before_abrupt_stop'))[0])
            evidence['abrupt_restart'] = {'before': before, 'generation_before': generation,
                'process_start_ticks_before': previous['process_start_ticks']}
            # A SIGKILL is intentional fault injection in this disposable App.
            # Product does not receive a trusted Supervisor stop receipt.
            remote('docker kill --signal KILL ' + app_container, step='app_kill')
            killed = container_generation(json.loads(remote('docker inspect ' + app_container, step='app_after_kill'))[0])
            assert killed['running'] is False, 'abrupt_app_stop_not_observed'
            prior_transport = stopped_transport()['crash']['requests']
            await ha.request('POST', f'/addons/{slug}/start', supervisor=True, timeout=APP_OPERATION_SECONDS)
            current, after = await restarted_app(previous, generation)
            evidence['abrupt_restart'].update(generation_after=after,
                process_start_ticks_after=current['process_start_ticks'],
                same_haos_boot=current['boot_id_sha256'] == previous['boot_id_sha256'],
                namespace_ids_changed=current['namespaces'] != previous['namespaces'])
            assert current['boot_id_sha256'] == previous['boot_id_sha256'], 'abrupt_restart_haos_boot_changed'
            deadline = time.monotonic() + JOB_STOP_SECONDS
            while time.monotonic() < deadline:
                uncertain = await api('GET', '/api/worker/jobs/' + held['id'])
                if uncertain['state'] == 'stop_unconfirmed':
                    break
                await asyncio.sleep(.5)
            evidence['abrupt_restart']['job'] = job_projection(uncertain)
            assert uncertain['state'] == 'stop_unconfirmed' and uncertain['stop_confirmed'] is False, 'abrupt_writer_fence_missing'
            lease_retained = (await api('GET', '/api/workers'))['active_job_id'] == held['id']
            evidence['abrupt_restart']['exact_lease_retained'] = lease_retained
            assert lease_retained, 'abrupt_writer_lease_missing'
            queued = await api('POST', '/api/worker/jobs', {'worker_id': 'home-coder', 'mode': 'research',
                'goal': 'This request must remain behind the unknown prior writer.'}, expected=202, key='home-crash-fenced-next')
            await asyncio.sleep(2)
            queued = await api('GET', '/api/worker/jobs/' + queued['id'])
            evidence['abrupt_restart']['queued_job'] = job_projection(queued)
            assert queued['state'] == 'queued' and queued['execution_started'] is False, 'abrupt_restart_new_writer_started'
            not_replayed = app_json('/data/home-worker-fixture/transport.json')['crash']['requests'] == prior_transport
            evidence['abrupt_restart']['model_request_count_unchanged'] = not_replayed
            assert not_replayed, 'abrupt_restart_model_request_replayed'
            await api('POST', '/api/worker/jobs/' + queued['id'] + '/cancel', {}, expected=202)
            evidence['abrupt_restart']['new_writer_started'] = False
            evidence['checks']['abrupt_same_boot_restart_retains_lease_and_prevents_replay'] = True
            phase('completed')
            evidence.update(status='passed',
                            recovery_limit='abrupt same-boot unknown work remains fenced until trusted stop proof; VM removed at teardown')
    except BaseException as error:
        finish_phase(evidence, started, time.monotonic())
        interrupted = isinstance(error, (KeyboardInterrupt, asyncio.CancelledError))
        evidence.update(status='interrupted' if interrupted else 'failed', **failure_receipt(error, source_files=(Path(__file__),)))
        evidence['owned_runtime_observation'] = lifecycle_observation({'vm': vm, 'outer': outer}, wall_started)
        if ha is not None:
            if ha.last_error is not None:
                evidence['upstream_error'] = upstream_flags(ha.last_error)
            try:
                info = await ha.request('GET', f'/addons/{slug}/info', supervisor=True, timeout=5)
                evidence['failure_app_state'] = info.get('state') if info.get('state') in {'started', 'stopped', 'error', 'unknown'} else 'unknown'
            except Exception as observation_error:
                evidence['failure_app_observation_error'] = type(observation_error).__name__
        if ssh is not None:
            if evidence['phase'] == 'disposable_loopback_registry':
                with contextlib.suppress(Exception):
                    evidence['registry_container_state'] = registry_container_state(json.loads(remote(
                        "docker inspect --format '{{json .State}}' " + registry_container, step='registry_state', timeout=10)))
            with contextlib.suppress(Exception):
                evidence['app_container_observation'] = container_projection(json.loads(remote('docker inspect ' + app_container, step='failure_app_inspect', timeout=10))[0], slug)
            for filename in ('startup', 'boundaries'):
                with contextlib.suppress(Exception):
                    evidence['failure_' + filename] = diagnostic_projection(app_json('/data/home-worker-fixture/' + filename + '.json'))
            with contextlib.suppress(Exception):
                raw = remote('docker logs --tail 80 ' + app_container + ' 2>&1 | tail -c 16385', step='failure_app_logs', timeout=10)
                evidence['app_stderr_summary'] = stderr_summary_bytes(raw)
        # Explicit diagnostic window is test-only, bounded and always followed
        # by normal cleanup. It never changes the failure or resubmits effects.
        evidence['failure_hold_seconds'] = hold_seconds if not interrupted else 0
        report.write_text(json.dumps(evidence, indent=2) + '\n')
        if not interrupted and hold_seconds:
            progress.write_text(json.dumps({'phase': 'failure_diagnostic_hold', 'failed_phase': evidence['phase'],
                'error_code': evidence['error_code'], 'seconds': hold_seconds}, indent=2) + '\n')
            for _ in range(hold_seconds):
                await asyncio.sleep(1)
        if interrupted:
            raise
        raise AssertionError('home_worker_haos_failed:' + evidence['phase'] + ':' + type(error).__name__) from None
    finally:
        if ha is not None:
            with contextlib.suppress(Exception):
                await ha.close()
        cleanup = {}
        for role, name in (('outer', outer), ('vm', vm)):
            try:
                subprocess.run(['docker', 'rm', '-fv', name], capture_output=True, timeout=30)
                check = subprocess.run(['docker', 'ps', '--all', '--quiet', '--no-trunc', '--filter', 'name=^/' + name + '$'],
                    capture_output=True, timeout=5)
                cleanup[role + '_removed'] = check.returncode == 0 and not check.stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                cleanup[role + '_removed'] = False
        try:
            owned = subprocess.run(['docker', 'run', '--rm', '-v', f'{tmp_path}:/lab', 'agentic-home-builder:0.2.0',
                'chown', '-R', f'{os.getuid()}:{os.getgid()}', '/lab'], capture_output=True, timeout=60)
            cleanup['disk_fixture_ownership_restored'] = owned.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            cleanup['disk_fixture_ownership_restored'] = False
        try:
            subprocess.run(['docker', 'network', 'rm', network], capture_output=True, timeout=15)
            check = subprocess.run(['docker', 'network', 'ls', '--quiet', '--filter', 'name=^' + network + '$'], capture_output=True, timeout=5)
            cleanup['network_removed'] = check.returncode == 0 and not check.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            cleanup['network_removed'] = False
        if evidence['status'] == 'running':
            evidence.update(status='interrupted', error_type='BaseException')
        evidence['cleanup'] = cleanup
        finish_phase(evidence, started, time.monotonic())
        evidence['duration_seconds'] = round(time.monotonic() - started, 3)
        report.write_text(json.dumps(evidence, indent=2) + '\n')
        progress.unlink(missing_ok=True)
        assert all(cleanup.values()), 'owned_home_worker_resources_not_removed'


@pytest.mark.parametrize('resolved', [False, True])
def test_outer_scope_probe_keeps_real_lookup_and_rejects_positive_dns(monkeypatch, resolved):
    import socket

    def lookup(host, port):
        assert (host, port) == ('homeassistant', 8123)
        if not resolved:
            raise socket.gaierror(socket.EAI_AGAIN, 'fixture DNS unavailable')
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.0.2.1', port))]

    def execute(*args, timeout):
        assert args[:4] == ('exec', 'actual-outer', 'python', '-c')
        assert len(args) == 5 and timeout == 5
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exec(args[4], {})
        return output.getvalue()

    monkeypatch.setattr(socket, 'getaddrinfo', lookup)
    monkeypatch.setitem(globals(), 'docker', execute)
    if resolved:
        with pytest.raises(AssertionError, match='outer_scope_fixture_unexpected'):
            outer_scope_observation('actual-outer')
    else:
        assert outer_scope_observation('actual-outer') == {'app_internal_dns_resolved': False}


@pytest.mark.parametrize('value', [0, None, '', []])
def test_outer_scope_probe_requires_explicit_false(monkeypatch, value):
    monkeypatch.setitem(globals(), 'docker', lambda *args, **kwargs: json.dumps({'app_internal_dns_resolved': value}))
    with pytest.raises(AssertionError, match='outer_scope_fixture_unexpected'):
        outer_scope_observation('actual-outer')


@pytest.mark.parametrize('error', [subprocess.TimeoutExpired('docker', 5),
                                  AssertionError('fixture_command_failed_docker_1')])
def test_outer_scope_probe_never_treats_exec_failure_as_dns_absence(monkeypatch, error):
    def failed(*args, **kwargs):
        raise error

    monkeypatch.setitem(globals(), 'docker', failed)
    with pytest.raises(type(error)) as caught:
        outer_scope_observation('actual-outer')
    assert caught.value is error


def test_safe_job_projection_excludes_prompts_credentials_paths_and_model_claims():
    raw = {'id': 'a' * 32, 'case_id': 'b' * 32, 'state': 'stop_unconfirmed', 'stop_confirmed': False,
           'runtime_location': 'haos_app', 'goal': 'private goal', 'authority': {'token': 'private secret'},
           'verification': {'state': 'failed', 'observed_from': 'private/path', 'exit_code': True,
                            'patch_sha256': 'secret', 'network': 'https://private.invalid'},
           'reason': 'private_secret', 'artifacts': [{'content_base64': 'private'}], 'evidence_ids': ['x']}
    result = job_projection(raw)
    assert result == {'id': 'a' * 32, 'case_id': 'b' * 32, 'state': 'stop_unconfirmed', 'stop_confirmed': False,
                      'runtime_location': 'haos_app', 'artifact_count': 1, 'evidence_count': 1, 'verification': {'state': 'failed'}}
    assert 'private' not in json.dumps(result)


@pytest.mark.parametrize('mutation', ['wrong_digest', 'wrong_identity', 'oversize', 'wrong_size'])
def test_artifact_bytes_rejects_untrusted_receipts(mutation):
    raw = b'actual bounded artifact'
    metadata = {'id': 'a' * 32, 'job_id': 'b' * 32, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    reply = {**metadata, 'content_base64': base64.b64encode(raw).decode()}
    if mutation == 'wrong_digest':
        metadata['sha256'] = '0' * 64
    elif mutation == 'wrong_identity':
        reply['job_id'] = 'c' * 32
    elif mutation == 'oversize':
        metadata['size'] = 200000
    else:
        metadata['size'] += 1
    with pytest.raises(AssertionError):
        artifact_bytes(metadata, reply)


def test_model_fixture_only_replaces_provider_bytes_and_saves_typed_counts(tmp_path):
    (tmp_path / 'mode').write_text('source')
    fixture = ModelFixture(tmp_path)
    headers = {'Authorization': 'Bearer ' + FIXTURE_KEY}
    status, media, stream = fixture('api.openai.com', '/v1/responses', 'POST', b'{"input":[]}', headers)
    assert status == 200 and media == 'text/event-stream' and b'exec_command' in stream
    assert b'Serialize stable public contract fields' in stream
    body = json.dumps({'input': [{'type': 'function_call_output', 'output': 'HOME_PROBE_SOURCE_AND_BOUNDARIES_OK 11 passed'}]}).encode()
    fixture('api.openai.com', '/v1/responses', 'POST', body, headers)
    assert json.loads((tmp_path / 'transport.json').read_text())['source'] == {
        'requests': 2, 'tool_output_received': True, 'source_marker': True, 'pytest_passed': True}
    assert FIXTURE_KEY not in (tmp_path / 'transport.json').read_text()
    with pytest.raises(AssertionError, match='unexpected_provider_route'):
        fixture('private.invalid', '/v1/responses', 'POST', body, headers)


def test_private_stderr_is_reduced_to_fixed_signatures(tmp_path):
    path = tmp_path / 'stderr'
    path.write_text('bwrap: pivot_root: Operation not permitted\nBearer private-secret\n/home/private/secret\n')
    result = stderr_summary(path)
    assert result['bubblewrap'] and result['pivot_root'] and result['operation_not_permitted']
    assert all(value not in json.dumps(result) for value in ('private-secret', '/home/private', 'Bearer'))
    path.write_bytes(b'x' * 20000)
    assert stderr_summary(path)['truncated'] and stderr_summary(path)['scanned_bytes'] == 16384
    path.write_text("bwrap: Can't make / slave: Permission denied\nbwrap: Can't mount proc on /newroot/proc\nprivate-token")
    result = stderr_summary(path)
    assert result['root_mount_propagation'] and result['proc_mount']
    assert 'private-token' not in json.dumps(result)


def test_model_fixture_continues_actual_tool_session_without_inventing_completion(tmp_path):
    (tmp_path / 'mode').write_text('cancel')
    fixture = ModelFixture(tmp_path)
    body = json.dumps({'input': [{'type': 'function_call_output', 'output': 'Process running with session ID 1234'}]}).encode()
    _, _, stream = fixture('api.openai.com', '/v1/responses', 'POST', body, {'Authorization': 'Bearer ' + FIXTURE_KEY})
    assert b'write_stdin' in stream and b'1234' in stream and b'Fixture source change' not in stream
    with pytest.raises(AssertionError, match='credential_boundary_failed'):
        fixture('api.openai.com', '/v1/responses', 'POST', body, {'Authorization': 'Bearer unknown'})


class InstallObserver:
    def __init__(self, *, done=None, missing=False, installed=False, errors=None):
        self.calls, self.done, self.missing, self.installed, self.errors = [], done, missing, installed, errors or []

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path))
        assert method == 'GET', 'observer_must_never_repeat_install_effect'
        if path == '/jobs/job':
            if self.missing:
                raise HAError('job missing')
            return {'uuid': 'job', 'reference': 'local_agentic_runner', 'done': self.done, 'errors': self.errors}
        if path == '/addons':
            return {'addons': [{'slug': 'local_agentic_runner'}] if self.installed else []}
        return {'slug': 'local_agentic_runner', 'version': 'fixture', 'state': 'stopped', 'apparmor': 'profile', 'devices': ['/dev/fuse'],
                'privileged': ['SYS_ADMIN'], 'options': {}, **{key: False for key in ('hassio_api', 'homeassistant_api',
                'docker_api', 'full_access', 'host_network', 'host_pid', 'auto_update', 'watchdog')}}


async def test_install_observer_missing_ack_requires_actual_app_postcondition():
    config = {'version': 'fixture', 'privileged': ['SYS_ADMIN'], 'devices': ['/dev/fuse']}
    ha = InstallObserver(missing=True, installed=True)
    assert await observe_install(ha, 'local_agentic_runner', 'job', config, attempts=1, delay=0) == 'app_postcondition_job_unavailable'
    absent = InstallObserver(missing=True)
    with pytest.raises(AssertionError, match='postcondition_timeout'):
        await observe_install(absent, 'local_agentic_runner', 'job', config, attempts=1, delay=0)
    assert all(method == 'GET' for method, _ in ha.calls + absent.calls)


async def test_install_observer_preserves_failed_job_and_does_not_start_app():
    ha = InstallObserver(done=True, installed=True, errors=[{'message': 'private upstream message'}])
    with pytest.raises(AssertionError, match='supervisor_app_install_failed') as caught:
        await observe_install(ha, 'local_agentic_runner', 'job', {'version': 'fixture'}, attempts=1, delay=0)
    assert 'private' not in str(caught.value) and ha.calls == [('GET', '/jobs/job')]


def test_saved_image_provenance_uses_actual_config_bytes_and_exact_layers(tmp_path):
    path = tmp_path / 'images.tar'
    raw = json.dumps({'rootfs': {'diff_ids': ['sha256:' + 'a' * 64]}, 'config': {'Env': ['PUBLIC=value']}}).encode()
    name = 'blobs/sha256/' + hashlib.sha256(raw).hexdigest()
    manifest = json.dumps([{'RepoTags': ['agentic-home-runner:fixture'], 'Config': name, 'Layers': []}]).encode()
    with tarfile.open(path, 'w') as archive:
        for filename, content in (('manifest.json', manifest), (name, raw)):
            entry = tarfile.TarInfo(filename)
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
    assert saved_image_identity(path, 'agentic-home-runner:fixture') == {
        'config_digest': 'sha256:' + hashlib.sha256(raw).hexdigest(), 'layers': ['sha256:' + 'a' * 64]}
    with pytest.raises(AssertionError, match='identity_ambiguous'):
        saved_image_identity(path, 'agentic-home-runner:other')


async def test_failed_install_job_is_rejected_even_before_done_marker():
    ha = InstallObserver(done=False, errors=[{'message': 'private'}])
    with pytest.raises(AssertionError, match='supervisor_app_install_failed'):
        await observe_install(ha, 'local_agentic_runner', 'job', {'version': 'fixture'}, attempts=1, delay=0)
    assert ha.calls == [('GET', '/jobs/job')]


@pytest.mark.parametrize('profile', [True, False, 'default', 'disable', None])
async def test_custom_apparmor_wire_contract_rejects_default_or_disabled(profile):
    ha = InstallObserver()
    info = await ha.request('GET', '/addons/local_agentic_runner/info')
    info['apparmor'] = profile
    with pytest.raises(AssertionError, match='apparmor_policy_changed'):
        app_contract(info, {'version': 'fixture', 'privileged': ['SYS_ADMIN']}, 'local_agentic_runner')


@pytest.mark.parametrize('capability', ['SYS_ADMIN', 'CAP_SYS_ADMIN'])
def test_exact_supervisor_container_contract_normalizes_kernel_capability_spelling(capability):
    raw = {'AppArmorProfile': 'local_agentic_runner',
        'HostConfig': {'Privileged': False, 'PidMode': '', 'NetworkMode': 'hassio', 'CapAdd': [capability],
                       'DeviceCgroupRules': ['c 10:229 rwm'], 'Devices': []},
        'State': {'Running': False, 'Status': 'exited', 'ExitCode': 1},
        'Config': {'Env': ['SUPERVISOR_TOKEN=private-secret']}}
    result = container_contract(raw, 'local_agentic_runner')
    assert result['cap_add'] == [capability] and result['state']['ExitCode'] == 1
    raw['HostConfig']['CapAdd'].append('CAP_NET_ADMIN')
    with pytest.raises(AssertionError, match='capability_set_changed'):
        container_contract(raw, 'local_agentic_runner')
    raw['AppArmorProfile'] = 'private-secret-profile'
    projected = container_projection(raw, 'local_agentic_runner')
    assert projected['apparmor_profile'] == 'unrecognized' and 'private-secret' not in json.dumps(projected)


def test_restart_requires_fresh_process_and_docker_start_even_when_namespace_inodes_repeat():
    previous = {'process_start_ticks': 100, 'boot_id_sha256': 'a' * 64, 'namespaces': {'pid': 42}}
    before = container_generation({'Id': 'b' * 64, 'State':
        {'StartedAt': '2026-10-13T01:00:00.123456789Z', 'Pid': 200, 'Running': True}})
    after = container_generation({'Id': 'b' * 64, 'State':
        {'StartedAt': '2026-10-13T01:00:05.123456789Z', 'Pid': 300, 'Running': True},
        'Config': {'Env': ['private-secret']}})
    assert not fresh_app_restart(previous, previous, before, after), 'cached_snapshot_must_not_satisfy_restart'
    current = {**previous, 'process_start_ticks': 600}
    assert fresh_app_restart(previous, current, before, after), 'inode_reuse_is_not_restart_failure'
    assert not fresh_app_restart(previous, current, before, before), 'docker_generation_must_also_change'
    assert not fresh_app_restart(previous, {**current, 'boot_id_sha256': 'c' * 64}, before, after)
    assert not fresh_app_restart(previous, current, before, {**after, 'container_id': 'd' * 64})
    assert fresh_app_restart(previous, current, before, {**after, 'container_id': 'd' * 64}, replacement_verified=True)
    assert not fresh_app_restart(previous, current, before, {**after, 'running': False})
    assert 'private' not in json.dumps(after)


def test_recreated_app_requires_exact_owned_name_image_and_unchanged_boundary():
    slug, image = 'local_agentic_runner', 'sha256:' + 'c' * 64
    raw = {'Id': 'b' * 64, 'Name': '/app_' + slug, 'Image': image, 'AppArmorProfile': slug,
        'HostConfig': {'Privileged': False, 'CapAdd': ['SYS_ADMIN'], 'DeviceCgroupRules': ['c 10:229 rwm'], 'Devices': []},
        'State': {'StartedAt': '2026-10-13T01:00:05.123456789Z', 'Pid': 300, 'Running': True}}
    assert verified_app_generation(raw, slug, image)['container_id'] == 'b' * 64
    with pytest.raises(AssertionError, match='restarted_app_image_changed'):
        verified_app_generation({**raw, 'Image': 'sha256:' + 'd' * 64}, slug, image)
    with pytest.raises(AssertionError, match='restarted_app_name_changed'):
        verified_app_generation({**raw, 'Name': '/app_other'}, slug, image)
    with pytest.raises(AssertionError, match='actual_custom_apparmor_profile_required'):
        verified_app_generation({**raw, 'AppArmorProfile': 'unconfined'}, slug, image)


@pytest.mark.parametrize('started', ['private-secret', '2026-99-99T01:00:00Z', None])
def test_restart_generation_rejects_untrusted_or_malformed_start_fields(started):
    with pytest.raises(AssertionError, match='app_start_generation_invalid'):
        container_generation({'Id': 'b' * 64, 'State': {'StartedAt': started, 'Pid': 200, 'Running': True}})


@pytest.mark.parametrize('value', ['-1', '181', '9999', '1.0', '', ' 1', '01', 'private-secret'])
def test_failure_hold_is_explicit_bounded_and_rejects_invalid_values(value):
    with pytest.raises(AssertionError, match='failure_hold_must_be_0_to_180_seconds'):
        failure_hold_seconds(value)
    assert failure_hold_seconds('0') == 0 and failure_hold_seconds('180') == 180


def test_failure_receipts_keep_fixed_codes_and_drop_private_nested_values():
    secret = 'private-secret'
    raw = {'phase': 'quota', 'status': 'failed', 'error_code': secret, 'error_type': 'AssertionError', 'token': secret,
        'checks': {'actual_ext4_enospc': False, secret: True},
        'stderr_summary': {'missing_module': True, 'scanned_bytes': 20, 'message': secret},
        'launcher_diagnostic': {'mode': 'fuse', 'error_code': 'home_launcher_failure', 'errno': 13, 'message': secret},
        'quota_counters': {'cpu.stat': {'usage_usec': 42, 'path': secret}, 'private': {secret: 99}}}
    result = diagnostic_projection(raw)
    assert result['error_code'] == 'unclassified_fixture_error'
    assert result['launcher_diagnostic']['error_code'] == 'home_launcher_failure'
    assert result['quota_counters'] == {'cpu.stat': {'usage_usec': 42}}
    assert secret not in json.dumps(result)
    fixed = 'actual_capability_set_changed'
    assert failure_receipt(AssertionError(fixed), source_files=(Path(__file__),))['error_code'] == fixed
    assert failure_receipt(AssertionError(secret), source_files=(Path(__file__),))['error_code'] == 'unclassified_fixture_error'


@pytest.mark.parametrize('output,code', [
    (b'dial tcp 127.0.0.1:5000: connect: connection refused', 'connection_refused'),
    ('connection refused', 'connection_refused'),
    (b'read: connection reset by peer', 'connection_reset_or_eof'),
    (b'Get "http://127.0.0.1:5000/v2/": unexpected EOF', 'connection_reset_or_eof'),
    (b'Get "http://127.0.0.1:5000/v2/": EOF', 'connection_reset_or_eof'),
    (b'use of closed network connection', 'connection_reset_or_eof'),
    (b'write: broken pipe', 'connection_reset_or_eof'),
    (b'received unexpected HTTP status: 500 Internal Server Error', 'http_5xx'),
    (b'HTTP/1.1 502 Bad Gateway', 'http_5xx'),
    (b'503 Service Unavailable', 'http_5xx'),
    (b'504 Gateway Timeout', 'http_5xx'),
    (b'error from registry: blob unknown to registry', 'blob_unknown'),
    (b'BLOB_UNKNOWN', 'blob_unknown'),
    (b'blob unknown to registry: 500 Internal Server Error', 'blob_unknown'),
    (b'Client.Timeout exceeded while awaiting headers', 'timeout'),
    (b'context deadline exceeded', 'timeout'),
    (b'i/o timeout', 'timeout'),
    (b'private-secret unexplained', 'other'),
    (b'Get "http://127.0.0.1:5000/v2/": retrying in 500 ms', 'other'),
    (None, 'other'),
])
def test_registry_push_failure_is_one_fixed_code_without_the_output(output, code):
    result = classify_registry_push_failure(output)
    assert result == code
    assert result in {'connection_refused', 'connection_reset_or_eof', 'http_5xx', 'blob_unknown', 'timeout', 'other'}
    assert 'private' not in result


def test_registry_push_timeout_and_cap_do_not_publish_partial_output():
    assert classify_registry_push_failure(b'connection refused private-secret', timed_out=True) == 'timeout'
    assert classify_registry_push_failure(b'a' * 65536 + b'connection refused') == 'other'
    assert classify_registry_push_failure(b'private-secret ' * 20 + b'connection refused') == 'connection_refused'


def test_registry_container_state_keeps_only_typed_docker_fields():
    raw = {'Running': True, 'Restarting': False, 'OOMKilled': False, 'Dead': False, 'Paused': True,
           'ExitCode': 137, 'Status': 'exited', 'Error': 'private-secret', 'Pid': 9, 'FinishedAt': 'private'}
    result = registry_container_state(raw)
    assert result == {'Running': True, 'Restarting': False, 'OOMKilled': False, 'Dead': False,
                      'ExitCode': 137, 'Status': 'exited'}
    assert 'private' not in json.dumps(result)
    assert registry_container_state({'Status': 'paused', 'Paused': True}) == {'Status': 'paused'}
    assert registry_container_state({'Running': 'true', 'ExitCode': '1', 'Status': 'weird', 'Error': 'x'}) == {}
    assert registry_container_state(None) == {}
    assert registry_container_state({'ExitCode': 999, 'Status': 'dead', 'OOMKilled': False}) == {
        'Status': 'dead', 'OOMKilled': False}


@pytest.mark.parametrize('code', ['local_registry_push_failed', 'runner_store_catalog_unavailable'])
def test_registry_fixture_failure_codes_are_literal_receipts(code):
    assert failure_receipt(AssertionError(code), source_files=(Path(__file__),))['error_code'] == code


def test_stopped_container_receipt_archive_rejects_symlink_and_oversize():
    for kind, size in ((tarfile.SYMTYPE, 0), (tarfile.REGTYPE, 129 * 1024)):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as archive:
            entry = tarfile.TarInfo('startup.json')
            entry.type, entry.size = kind, size
            archive.addfile(entry, io.BytesIO(b'x' * size) if size else None)
        with pytest.raises(AssertionError, match='fixture_archive_(?:invalid|limit)'):
            copied_json(stream.getvalue())


def test_source_probe_never_writes_to_potential_tmpfs_and_checks_both_token_aliases():
    from home_worker_haos_probe import SOURCE_COMMAND
    assert 'os.statvfs(target).f_flag & os.ST_RDONLY' in SOURCE_COMMAND
    assert "os.environ.get('HASSIO_TOKEN')" in SOURCE_COMMAND
    assert 'fixture-write' not in SOURCE_COMMAND
    assert "Path(path).write_text" not in SOURCE_COMMAND


def test_rewritten_pytest_failure_keeps_only_exact_fixed_first_line():
    code = 'actual_boundary_limits_failed'
    rewritten = code + '\nassert private-account-token == private-path'
    result = failure_receipt(AssertionError(rewritten), source_files=(Path(__file__),))
    assert result['error_code'] == code and 'private' not in json.dumps(result)


def test_stored_observation_projection_distinguishes_connection_failure_without_private_content():
    content = {'app_runtime': {'observed_from': 'haos_app'}, 'core_api_from_app':
        {'dns_resolved': True, 'reachable': False, 'http_status': None, 'timed_out': False,
         'http_port': None, 'attempted_ports': [80, 8123], 'endpoints': [
             {'port': 80, 'reachable': False, 'http_status': None, 'error_code': 'connection_failed'},
             {'port': 8123, 'reachable': False, 'http_status': None, 'error_code': 'timeout'}]},
        'private': 'private-secret'}
    row = {'case_id': 'private-case', 'observed_from': 'haos_app',
        'collection_method': 'fixed_read_only_app_probe', 'content': content,
        'content_digest': hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True,
            separators=(',', ':')).encode()).hexdigest(),
        'limitations': ['private-note'], 'version_refs': {'source': 'a' * 40}, 'credential': 'private-secret'}
    result = stored_observation_projection([row], 'private-case', 'a' * 40)
    assert result['same_case'] and result['content_digest_matches'] and result['source_commit_matches']
    assert result['core_api_from_app'] == content['core_api_from_app']
    assert 'private' not in json.dumps(result)
    row['content_digest'] = 'wrong'
    row['content']['core_api_from_app']['http_status'] = 'private-secret'
    row['content']['core_api_from_app']['endpoints'][0].update(error_code='private-secret', body='private-secret')
    row['content']['app_runtime']['observed_from'] = 'private-origin'
    result = stored_observation_projection([row], 'different-case', 'b' * 40)
    assert not result['same_case'] and not result['content_digest_matches'] and not result['source_commit_matches']
    assert not result['app_runtime_from_haos_app'] and result['core_api_from_app']['http_status'] is None
    assert result['core_api_from_app']['endpoints'][0]['error_code'] is None
    assert 'private' not in json.dumps(result)
    assert not stored_observation_projection([row, row], 'private-case', 'a' * 40)['single_fixed_probe']
    assert not stored_observation_projection([], 'private-case', 'a' * 40)['records_present']
    fixed = failure_receipt(AssertionError('home_core_api_unreachable'), source_files=(Path(__file__),))
    assert fixed['error_code'] == 'home_core_api_unreachable'


def test_typed_sandbox_and_prepare_diagnostics_keep_codes_without_message(tmp_path):
    from agentic_host.coding_sandbox import SandboxError
    from home_worker_haos_probe import launcher_diagnostic
    error = SandboxError('private-key /private/path', code='command_failed', operation='findmnt', exit_code=1)
    result = failure_receipt(error)
    assert result['error_code'] == 'command_failed'
    assert result['operation'] == 'findmnt' and result['command_exit_code'] == 1
    result['mode'] = 'prepare'
    result['private_token'] = 'private-key'
    path = tmp_path / 'launch-error.json'
    path.write_text(json.dumps(result))
    safe = launcher_diagnostic(path)
    assert safe['mode'] == 'prepare' and safe['error_type'] == 'SandboxError'
    assert safe['error_code'] == 'command_failed' and safe['operation'] == 'findmnt'
    projected = diagnostic_projection(safe)
    assert projected['command_exit_code'] == 1 and 'private' not in json.dumps(projected)


@pytest.mark.parametrize('uid,request_data,accepted', [
    (0, b'job-proof ' + b'a' * 32 + b'\n', True),
    (1000, b'job-proof ' + b'a' * 32 + b'\n', False),
    (0, b'job-proof ../../private\n', False),
    (0, b'exec private-command\n', False),
    (0, b'job-proof ' + b'a' * 200 + b'\n', False),
])
async def test_fixture_diagnostic_rpc_only_root_fixed_job_id(monkeypatch, uid, request_data, accepted):
    import struct

    import home_worker_haos_probe as probe

    calls = []
    def inspect(identifier):
        calls.append(identifier)
        return {'record_present': False}
    monkeypatch.setattr(probe, 'job_proof', inspect)
    class Peer:
        def getsockopt(self, *args):
            return struct.pack('3i', 1, uid, 0)
    class Writer:
        def __init__(self):
            self.output, self.closed = b'', False
        def get_extra_info(self, name):
            assert name == 'socket'
            return Peer()
        def write(self, value):
            self.output += value
        async def drain(self):
            pass
        def close(self):
            self.closed = True
        async def wait_closed(self):
            pass
    reader, writer = asyncio.StreamReader(limit=128), Writer()
    reader.feed_data(request_data)
    reader.feed_eof()
    await probe.diagnostic_request(reader, writer)
    assert writer.closed and bool(calls) is accepted
    if accepted:
        result = json.loads(writer.output)
        assert result['record_present'] is False
        assert set(result['diagnostic_namespace_ids']) == {'pid_ns', 'cgroup_ns', 'mnt_ns'}
    else:
        assert writer.output == b''


@pytest.mark.parametrize('devices', [[], ['/dev/fuse', '/dev/private-fixture'], ['/dev/private-fixture']])
async def test_app_contract_permits_only_declared_fixed_fuse_device(devices):
    info = await InstallObserver().request('GET', '/addons/local_agentic_runner/info')
    info['devices'] = devices
    config = {'version': 'fixture', 'privileged': ['SYS_ADMIN'], 'devices': ['/dev/fuse']}
    with pytest.raises(AssertionError, match='supervisor_fuse_device_contract_changed'):
        app_contract(info, config, 'local_agentic_runner')


@pytest.mark.parametrize('rules,mappings', [([], []), (['a *:* rwm'], []),
    (['c 10:229 rwm', 'c 1:3 rwm'], []), (['c 10:229 rwm'], [{'PathOnHost': '/dev/private-fixture'}])])
def test_actual_device_contract_rejects_missing_broad_or_additional_access(rules, mappings):
    raw = {'AppArmorProfile': 'local_agentic_runner', 'HostConfig': {'Privileged': False, 'CapAdd': ['SYS_ADMIN'],
        'DeviceCgroupRules': rules, 'Devices': mappings}}
    assert 'private-fixture' not in json.dumps(container_projection(raw, 'local_agentic_runner'))
    with pytest.raises(AssertionError, match='actual_fuse_device_cgroup_contract_changed'):
        container_contract(raw, 'local_agentic_runner')


@pytest.mark.parametrize('missing', [True, False])
def test_candidate_fuse_probe_rejects_absent_or_device_cgroup_denied_without_raw_error(monkeypatch, missing):
    import errno
    import stat
    from types import SimpleNamespace

    from agentic_runner import home_launcher as launcher

    calls = []
    def metadata(path):
        assert str(path) == '/dev/fuse'
        if missing:
            raise FileNotFoundError(errno.ENOENT, 'private-fixture-error')
        return SimpleNamespace(st_mode=stat.S_IFCHR | 0o666)
    def denied(path, flags):
        calls.append(path)
        raise PermissionError(errno.EPERM, 'private-fixture-error')
    monkeypatch.setattr(Path, 'stat', metadata)
    monkeypatch.setattr(os, 'open', denied)
    with pytest.raises(launcher.BoundaryError) as caught:
        launcher.validate_fuse_device()
    assert str(caught.value) == 'home_fuse_device_unavailable'
    assert 'private' not in str(caught.value)
    assert calls == ([] if missing else ['/dev/fuse'])


def test_entry_probe_detects_inherited_private_directory_fd_without_exporting_targets(tmp_path, monkeypatch):
    import ctypes
    import errno

    from home_worker_haos_probe import ENTRY_BOUNDARY_SOURCE

    namespace = {}
    exec(compile(ENTRY_BOUNDARY_SOURCE, '<fixed-entry-probe>', 'exec'), namespace)
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    def missing(*args):
        raise FileNotFoundError(errno.ENOENT, 'fixture')
    class Library:
        def unshare(self, flags):
            assert flags.value == 0x10000000
            ctypes.set_errno(errno.EPERM)
            return -1
    try:
        with monkeypatch.context() as local:
            local.setattr(os, 'listdir', lambda path: [str(descriptor), '999999'])
            local.setattr(os, 'readlink', lambda path: '/data/coding/work/private-marker')
            local.setattr(os, 'lstat', missing)
            local.setattr(os, 'open', missing)
            local.setattr(Path, 'read_text', lambda path: 'CapEff:\t0000000000000000\nNoNewPrivs:\t1\nSeccomp:\t2\n')
            local.setattr(ctypes, 'CDLL', lambda *args, **kwargs: Library())
            checks = namespace['entry_boundary_checks']()
    finally:
        os.close(descriptor)
    assert set(checks) == ENTRY_CHECK_NAMES and all(type(value) is bool for value in checks.values())
    assert not checks['private_workspace_fds_absent'] and not checks['inherited_directory_fds_absent']
    assert checks['effective_capabilities_zero'] and checks['no_new_privileges'] and checks['seccomp_filter_active']
    assert checks['worker_fuse_device_unavailable'] and checks['nested_user_namespace_denied']
    assert 'private-marker' not in json.dumps(checks)


@pytest.mark.parametrize('code', ['sandbox_rejected', 'command_timeout', 'command_failed',
    'coding_setup_failed', 'home_fuse_mount_unavailable', 'home_launcher_stop_unconfirmed'])
def test_job_projection_keeps_allowlisted_launch_failure_and_phase(code):
    result = job_projection({'state': 'stop_unconfirmed', 'phase': 'launching', 'error_code': code,
        'stop_confirmed': False, 'execution_started': False,
        'launch_failure': {'mode': 'prepare', 'error_type': 'SandboxError', 'error_code': code,
            'operation': 'git.clone', 'timeout_seconds': 90, 'message': 'private /home/owner/token'},
        'prompt': 'private /home/owner/token'})
    assert result['error_code'] == code and result['phase'] == 'launching'
    assert result['launch_failure']['error_code'] == code
    assert result['launch_failure']['operation'] == 'git.clone'
    assert result['stop_confirmed'] is False and result['execution_started'] is False
    assert 'private' not in json.dumps(result)


def test_partial_launch_proof_keeps_failure_before_cgroup_creation_without_inferring_stop(tmp_path):
    from home_worker_haos_probe import job_proof
    root, identifier = tmp_path / 'work', 'a' * 32
    directory = root / identifier
    directory.mkdir(parents=True)
    failure = {'mode': 'prepare', 'error_code': 'command_timeout', 'error_type': 'SandboxError',
        'operation': 'git.checkout', 'timeout_seconds': 90, 'message': 'private-secret /private/path'}
    (directory / 'launch-error.json').write_text(json.dumps(failure))
    (root / (identifier + '.json')).write_text(json.dumps({'state': 'stop_unconfirmed',
        'stop_confirmed': False, 'launch_failure': failure, 'request': {'token': 'private-secret'}}))
    before = {path: path.read_bytes() for path in root.rglob('*.json')}
    result = job_proof(identifier, root=root)
    assert result['launcher_diagnostic']['operation'] == 'git.checkout'
    assert result['launch_failure']['error_code'] == 'command_timeout'
    assert result['stop_confirmed'] is False
    assert result['cgroup_recorded'] is False and result['fuse_cgroup_recorded'] is False
    assert 'cgroup_present' not in result and 'cgroup_populated' not in result
    assert not workspace_reclaimed(result)
    assert 'private' not in json.dumps(result)
    assert {path: path.read_bytes() for path in root.rglob('*.json')} == before


def test_partial_launch_missing_worker_group_preserves_fuse_and_unreadable_event_evidence(tmp_path):
    from home_worker_haos_probe import job_proof
    root, identifier, name = tmp_path / 'work', 'b' * 32, 'f-' + 'c' * 32
    root.mkdir()
    group = tmp_path / 'cgroups' / name
    group.mkdir(parents=True)
    path = root / (identifier + '.json')
    path.write_text(json.dumps({'state': 'stop_unconfirmed', 'stop_confirmed': False, 'fuse_cgroup': {'name': name}}))
    failed_read = job_proof(identifier, root=root)
    assert failed_read['fuse_cgroup_present'] is True
    assert failed_read['fuse_cgroup_observation_error']['error_type'] == 'FileNotFoundError'
    assert 'fuse_cgroup_populated' not in failed_read
    (group / 'cgroup.events').write_text('populated 1\nfrozen 0\n')
    result = job_proof(identifier, root=root)
    assert result['fuse_cgroup_populated'] is True and result['cgroup_recorded'] is False
    assert result['launcher_diagnostic'] == {'available': False, 'read_status': 'missing'}
    assert result['stop_confirmed'] is False


@pytest.mark.parametrize('raw', ['null', '[]', '{broken', '"private-secret"', 'x' * 4097,
    '{"mode": [], "error_type": {}, "error_code": [], "operation": ["private-secret"]}'])
def test_launch_receipt_bad_shape_cannot_hide_other_failure_evidence(tmp_path, raw):
    from home_worker_haos_probe import launcher_diagnostic
    path = tmp_path / 'launch-error.json'
    path.write_text(raw)
    result = launcher_diagnostic(path)
    assert 'private' not in json.dumps(result)
    if raw.startswith('{"mode"'):
        assert result['error_code'] == 'home_launcher_failure'
    else:
        assert result['available'] is False
        assert result['read_status'] in {'invalid', 'oversize'}


def test_job_proof_keeps_launch_receipt_when_job_ledger_is_missing_or_invalid(tmp_path):
    from home_worker_haos_probe import job_proof
    root, identifier = tmp_path / 'work', 'd' * 32
    directory = root / identifier
    directory.mkdir(parents=True)
    (directory / 'launch-error.json').write_text(json.dumps({'mode': 'fuse',
        'error_type': 'BoundaryError', 'error_code': 'home_fuse_mount_unavailable'}))
    result = job_proof(identifier, root=root)
    assert result['record_present'] is False
    assert result['launcher_diagnostic']['error_code'] == 'home_fuse_mount_unavailable'
    (root / (identifier + '.json')).write_text('[]')
    result = job_proof(identifier, root=root)
    assert result['record_observation_error']['error_code'] == 'job_record_invalid'
    assert result['launcher_diagnostic']['mode'] == 'fuse'
    assert not workspace_reclaimed(result)


def test_cleanup_proof_requires_confirmed_disposal_and_all_resource_observations():
    proof = {'record_present': True, 'stop_confirmed': True, 'state': 'disposed',
        'workspace_mounted': False, 'workspace_image_present': False,
        'cgroup_present': False, 'fuse_cgroup_present': False}
    assert workspace_reclaimed(proof)
    for missing in proof:
        assert not workspace_reclaimed({key: value for key, value in proof.items() if key != missing})
    assert not workspace_reclaimed({**proof, 'state': 'stop_unconfirmed'})
    assert not workspace_reclaimed({**proof, 'fuse_cgroup_present': True})


async def test_wait_timeout_captures_exact_job_fence_and_phase_timing_before_cleanup():
    identifier, now = 'a' * 32, [100.0]
    evidence, calls, checkpoints = {}, [], []
    async def api(method, path):
        calls.append((method, path))
        if path == '/api/workers':
            return {'active_job_id': identifier, 'workers': [{'worker_id': 'home-coder', 'available': True,
                'private_token': 'secret'}]}
        return {'id': identifier, 'state': 'stop_unconfirmed', 'phase': 'stop_unconfirmed',
            'stop_confirmed': False, 'execution_started': False, 'error_code': 'sandbox_rejected'}
    async def sleep(seconds):
        now[0] += seconds
    def proof(value):
        assert value == identifier
        assert checkpoints[-1]['job_waits'][0]['outcome'] == 'failed'
        return {'stop_confirmed': False, 'launch_failure': {'error_code': 'command_timeout'}}
    with pytest.raises(AssertionError, match='home_worker_state_timeout'):
        await observe_job(api, identifier, evidence, proof, timeout=1, clock=lambda: now[0], sleep=sleep,
            checkpoint=lambda: checkpoints.append(json.loads(json.dumps(evidence))))
    result = evidence['job_waits'][0]
    assert result['elapsed_seconds'] == 1 and len(result['state_changes']) == 1
    assert result['boundary']['launch_failure']['error_code'] == 'command_timeout'
    assert result['lease'] == {'exact_job_retained': True, 'no_active_job': False, 'worker_available': True}
    assert result['error']['error_code'] == 'home_worker_state_timeout'
    assert all(method == 'GET' for method, _ in calls)
    assert 'secret' not in json.dumps(evidence)


async def test_wait_diagnostic_failure_does_not_replace_original_timeout_or_claim_stop():
    now, evidence = [0.0], {}
    async def api(method, path):
        if path == '/api/workers':
            raise TimeoutError('private response')
        return {'state': 'stop_unconfirmed', 'stop_confirmed': False}
    async def sleep(seconds):
        now[0] += seconds
    def proof(value):
        raise subprocess.TimeoutExpired('private ssh command', 10, output=b'private-token')
    with pytest.raises(AssertionError, match='home_worker_state_timeout'):
        await observe_job(api, 'a' * 32, evidence, proof, timeout=1, clock=lambda: now[0], sleep=sleep)
    result = evidence['job_waits'][0]
    assert result['boundary_observation_error']['error_type'] == 'TimeoutExpired'
    assert result['lease_observation_error']['error_type'] == 'TimeoutError'
    assert result['error']['error_code'] == 'home_worker_state_timeout'
    assert 'lease' not in result and 'boundary' not in result
    assert evidence['last_job']['stop_confirmed'] is False
    assert 'private' not in json.dumps(evidence)


async def test_wait_records_successful_state_changes_without_failure_diagnostics():
    now, evidence, states = [0.0], {}, iter(['launching', 'running', 'succeeded'])
    async def api(method, path):
        state = next(states)
        return {'state': state, 'phase': state, 'stop_confirmed': state == 'succeeded'}
    async def sleep(seconds):
        now[0] += seconds
    def proof(value):
        pytest.fail('Successful observation does not need failure diagnostics')
    result = await observe_job(api, 'a' * 32, evidence, proof, timeout=2, clock=lambda: now[0], sleep=sleep)
    assert result['state'] == 'succeeded'
    wait = evidence['job_waits'][0]
    assert wait['outcome'] == 'observed' and wait['elapsed_seconds'] == 1
    assert [(item['elapsed_seconds'], item['job']['phase']) for item in wait['state_changes']] == [
        (0, 'launching'), (.5, 'running'), (1, 'succeeded')]


def test_phase_times_preserve_failed_stage_duration_without_including_diagnostics_or_cleanup():
    evidence = {}
    begin_phase(evidence, 'boot_official_haos', 100, 100)
    begin_phase(evidence, 'actual_case_and_home_source_task', 100, 120)
    finish_phase(evidence, 100, 400)
    finish_phase(evidence, 100, 500)
    assert evidence['phase_timings'] == [
        {'phase': 'boot_official_haos', 'started_seconds': 0, 'duration_seconds': 20},
        {'phase': 'actual_case_and_home_source_task', 'started_seconds': 20, 'duration_seconds': 280}]


async def test_failed_stopped_job_captures_receipt_and_survives_checkpoint_failure():
    evidence, reads = {}, []
    async def api(method, path):
        reads.append((method, path))
        if path == '/api/workers':
            return {'active_job_id': None, 'workers': []}
        return {'state': 'failed', 'stop_confirmed': True, 'execution_started': False,
            'error_code': 'command_timeout'}
    def checkpoint():
        raise OSError(28, 'private evidence path')
    result = await observe_job(api, 'a' * 32, evidence, lambda _: {
        'launcher_diagnostic': {'error_code': 'command_timeout'}}, checkpoint=checkpoint)
    wait = evidence['job_waits'][0]
    assert result['state'] == 'failed' and wait['job_failed'] is True
    assert wait['boundary']['launcher_diagnostic']['error_code'] == 'command_timeout'
    assert wait['checkpoint_error']['errno'] == 28 and wait['lease']['no_active_job'] is True
    assert 'private' not in json.dumps(evidence)
    assert all(method == 'GET' for method, _ in reads)


async def test_timeout_with_failed_checkpoint_still_collects_receipts_and_keeps_original_error():
    evidence = {}
    async def api(method, path):
        return {'active_job_id': 'a' * 32}
    def checkpoint():
        raise OSError(28, 'private evidence path')
    with pytest.raises(AssertionError, match='home_worker_state_timeout'):
        await observe_job(api, 'a' * 32, evidence, lambda _: {'record_present': False},
            timeout=0, checkpoint=checkpoint)
    wait = evidence['job_waits'][0]
    assert wait['boundary'] == {'record_present': False}
    assert wait['checkpoint_error']['errno'] == 28 and wait['lease']['exact_job_retained'] is True
    assert wait['error']['error_code'] == 'home_worker_state_timeout'


async def test_job_timeline_stays_bounded_and_retains_the_last_observation():
    now, evidence = [0.0], {}
    async def api(method, path):
        if path == '/api/workers':
            return {'active_job_id': 'a' * 32}
        return {'state': 'launching' if now[0] % 1 == 0 else 'running', 'stop_confirmed': False}
    async def sleep(seconds):
        now[0] += seconds
    with pytest.raises(AssertionError, match='home_worker_state_timeout'):
        await observe_job(api, 'a' * 32, evidence, lambda _: {}, timeout=100,
            clock=lambda: now[0], sleep=sleep)
    timeline = evidence['job_waits'][0]
    assert len(timeline['state_changes']) == 128 and timeline['state_changes_omitted'] == 72
    assert timeline['state_changes'][0]['elapsed_seconds'] == 0
    assert timeline['state_changes'][-1]['elapsed_seconds'] == 99.5


def test_partial_runtime_launch_recovery_is_observed_without_clearing_its_fence(tmp_path, monkeypatch):
    from agentic_host.coding_sandbox import SandboxError
    from agentic_runner import home_launcher as launcher
    from agentic_runner.home_sandbox import HomeSandbox
    from home_worker_haos_probe import job_proof

    root, identifier = tmp_path / 'work', 'a' * 32
    root.mkdir()
    identity = {'boot_id': 'fixture-boot', 'pid_ns': 101, 'cgroup_ns': 102, 'mnt_ns': 103}
    current = [dict(identity)]
    monkeypatch.setattr(launcher, 'ROOT', tmp_path)
    monkeypatch.setattr(launcher, 'identity', lambda: dict(current[0]))
    sandbox = HomeSandbox(root, None, {'agentic-home': tmp_path})
    monkeypatch.setattr(sandbox, 'preflight', lambda: True)
    monkeypatch.setattr(sandbox, '_require_work_root', lambda: None)
    attempts = []
    def failed_prepare(job, program):
        attempts.append(job['job_id'])
        (root / job['job_id']).mkdir()
        raise SandboxError('private command and arguments', code='command_timeout',
            operation='git.checkout', timeout_seconds=90)
    monkeypatch.setattr(sandbox, 'prepare', failed_prepare)
    with pytest.raises(SandboxError):
        sandbox.launch(identifier, 1, 'agentic-home', 'b' * 40, 'pass')
    failure = job_proof(identifier, root=root)
    assert failure['launch_failure']['error_code'] == 'command_timeout'
    assert failure['stop_confirmed'] is False and failure['state'] == 'stop_unconfirmed'
    current[0]['mnt_ns'] += 1
    assert sandbox.cancel(identifier)['stop_confirmed'] is False
    assert job_proof(identifier, root=root)['stop_confirmed'] is False
    with pytest.raises(SandboxError, match='Previous writer has not been fenced'):
        sandbox.launch('c' * 32, 2, 'agentic-home', 'b' * 40, 'pass')
    assert attempts == [identifier]
    current[0] = identity
    assert sandbox.cancel(identifier)['stop_confirmed'] is True
    recovered = job_proof(identifier, root=root)
    assert recovered['state'] == 'cancelled' and recovered['stop_confirmed'] is True
    assert recovered['launch_failure'] == failure['launch_failure']
    assert (root / identifier / 'cancelled').is_file()
    assert not workspace_reclaimed(recovered)


@pytest.mark.parametrize('diagnostic', [
    {'boundary': {'launcher_diagnostic': {'error_code': 'command_timeout'}}},
    {'boundary_observation_error': {'error_type': 'TimeoutExpired', 'error_code': 'unclassified_fixture_error'}},
])
async def test_failed_source_job_keeps_primary_verdict_when_artifact_read_fails(diagnostic):
    identifier, case_id = 'a' * 32, 'b' * 32
    evidence = {'job_waits': [{'job_id': identifier, **diagnostic}]}
    job = {'id': identifier, 'case_id': case_id, 'state': 'failed', 'stop_confirmed': True,
        'artifacts': [{'name': 'home-boundaries.json', 'id': 'c' * 32}]}
    async def api(method, path):
        raise TimeoutError('private response')
    def proof(value):
        pytest.fail('The failure observation was already captured')
    with pytest.raises(AssertionError, match='^home_source_job_failed$'):
        await require_source_job_success(job, case_id, evidence, api, proof)
    assert evidence['failed_job_boundary_observation_error']['error_type'] == 'TimeoutError'
    key = 'boundary' if 'boundary' in diagnostic else 'boundary_observation_error'
    assert evidence['source_job_' + key] == diagnostic[key]
    assert 'private' not in json.dumps(evidence)


async def test_failed_source_job_keeps_verdict_when_uncaptured_proof_and_artifacts_are_invalid():
    job = {'id': 'a' * 32, 'state': 'failed', 'stop_confirmed': True, 'artifacts': [None]}
    evidence = {}
    async def api(method, path):
        pytest.fail('Invalid metadata must not trigger a request')
    def proof(value):
        raise subprocess.TimeoutExpired('private command', 10)
    with pytest.raises(AssertionError, match='^home_source_job_failed$'):
        await require_source_job_success(job, 'b' * 32, evidence, api, proof)
    assert evidence['source_job_boundary_observation_error']['error_type'] == 'TimeoutExpired'
    assert 'failed_job_boundary_observation_error' in evidence
    assert 'private' not in json.dumps(evidence)


async def test_missing_active_job_field_remains_unknown_in_failure_receipt():
    evidence = {}
    async def api(method, path):
        return {'workers': [{'worker_id': 'home-coder', 'available': False}]}
    with pytest.raises(AssertionError, match='home_worker_state_timeout'):
        await observe_job(api, 'a' * 32, evidence, lambda _: {}, timeout=0)
    assert evidence['job_waits'][0]['lease'] == {'worker_available': False}


@pytest.mark.parametrize('extra', [
    {'record_observation_error': {}}, {'cgroup_observation_error': {}}, {'fuse_cgroup_observation_error': {}},
    {'cgroup_populated': True}, {'fuse_cgroup_populated': True},
    {'cgroup_populated': False}, {'fuse_cgroup_populated': False},
])
def test_cleanup_rejects_failed_or_contradictory_resource_observations(extra):
    proof = {'record_present': True, 'stop_confirmed': True, 'state': 'disposed',
        'workspace_mounted': False, 'workspace_image_present': False,
        'cgroup_present': False, 'fuse_cgroup_present': False}
    assert not workspace_reclaimed({**proof, **extra})


def test_job_proof_observes_cgroup_presence_once_and_cannot_publish_absent_but_populated(tmp_path, monkeypatch):
    from home_worker_haos_probe import job_proof
    identifier, name = 'a' * 32, 'w-' + 'b' * 32
    root = tmp_path / 'work'
    root.mkdir()
    group = tmp_path / 'cgroups' / name
    group.mkdir(parents=True)
    (group / 'cgroup.events').write_text('populated 1\nfrozen 0\n')
    (root / (identifier + '.json')).write_text(json.dumps({'state': 'disposed', 'stop_confirmed': True,
        'cgroup': {'name': name}}))
    original, observed = Path.exists, []
    def exists(path):
        if path == group:
            observed.append(True)
            return len(observed) > 1
        return original(path)
    monkeypatch.setattr(Path, 'exists', exists)
    proof = job_proof(identifier, root=root)
    assert len(observed) == 1
    assert proof['cgroup_present'] is False and 'cgroup_populated' not in proof
