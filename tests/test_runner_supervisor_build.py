"""Actual HAOS Supervisor build of the Home coding App from exact HEAD source.

Opt-in (--haos-vm). Every file of runner/ at HEAD is delivered as a local App and
the real Supervisor builds runner/Dockerfile in a VM sized like the shipped laptop
profile. The Supervisor builder and the two digest-pinned base images are
preloaded, but the registry is not blocked; apt, npm, PyPI, agy and the coding
source fetch stay live. This is not the public #stable channel and no pairing,
login, model or coding job runs. No HA/Supervisor/OS update is performed.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import lzma
import math
import os
import re
import secrets
import shlex
import shutil
import subprocess
import tarfile
import time
import uuid
from pathlib import Path

import aiohttp
import pytest
import yaml
from agentic_controller import VERSION
from agentic_controller.ha import HAError
from agentic_controller.home_worker_maintenance import JOB_TIMEOUT
from home_worker_haos_probe import failure_receipt
from test_bridge_haos import BUILDER, PYTHON, ProbeHA, docker, lifecycle_observation, process
from test_home_worker_haos import (
    app_contract,
    container_contract,
    container_projection,
    failure_hold_seconds,
    observe_install,
)

ROOT = Path(__file__).parents[1]
# Literal assertion codes in these trusted files (including the reused observers
# and contracts) are the only error codes a failure receipt may publish.
CODE_SOURCES = (Path(__file__), ROOT / 'tests/test_home_worker_haos.py', ROOT / 'tests/test_bridge_haos.py')
KIND = 'real_haos_supervisor_app_build'
DELIVERY = 'local_source_exact_head_files'
OBSERVED_FROM = 'haos_supervisor_build_and_app_container'
SLUG, APP_DIR, APP_CONTAINER = 'local_agentic_runner', 'agentic_runner', 'app_local_agentic_runner'
INSTALL_POLL_SECONDS = 5
LIVE_BUILD_DOWNLOADS = ('apt', 'npm', 'pypi', 'agy', 'coding_source')
LIMITATIONS = ('local_source_delivery_not_public_stable_channel',
               'no_pairing_login_model_or_coding_executed',
               'builder_and_pinned_bases_preloaded_registry_not_blocked')
VM_PROFILE = 'host/profiles/laptop-amd64.json'
CHECK_NAMES = frozenset({'supervisor_healthy', 'exact_head_source_delivered', 'pinned_bases_preloaded',
    'store_catalog_build_version', 'supervisor_build_install_completed', 'real_app_contract_installed',
    'app_started', 'pairing_published_to_own_options', 'real_app_contract_configured', 'container_contract',
    'docker_socket_not_mounted', 'image_version_label', 'app_health_200', 'pairing_fingerprint_matches_tls',
    'codex_version_pinned', 'worker_source_matches_lock', 'custom_apparmor_enforcing', 'docker_socket_absent'})
PAIRING_KEYS = frozenset({'pairing_fingerprint', 'pairing_code'})
PAIRING_CODE = re.compile(r'[A-Za-z0-9_-]{16,128}')
CONTRACT_KEYS = ('slug', 'version', 'hassio_api', 'homeassistant_api', 'docker_api', 'full_access', 'host_network',
                 'host_pid', 'apparmor', 'privileged', 'devices', 'auto_update', 'watchdog', 'network', 'auth_api', 'ingress')
CLEANUP_NAMES = frozenset({'vm_removed', 'disk_fixture_ownership_restored', 'network_removed'})
HEX40 = re.compile(r'[a-f0-9]{40}')
HEX64 = re.compile(r'[a-f0-9]{64}')
PINNED_REF = re.compile(r'[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9._-]+@sha256:[a-f0-9]{64}')
PINNED_FROM = re.compile(r'(?i:FROM)\s+(' + PINNED_REF.pattern + r')(?:\s+(?i:AS)\s+[a-z][a-z0-9_-]*)?')
SAFE_TEXT = re.compile(r'[A-Za-z0-9_.:@+-]{0,100}')
SAFE_KEY = re.compile(r'[A-Za-z0-9_.]{1,64}(?:/tcp)?')
FIXED_TEXT = frozenset({'/dev/fuse', 'c 10:229 rwm', 'a *:* rwm'})
HEALTH_PROBE = ("import json,urllib.request,urllib.error\n"
                "try: status=urllib.request.urlopen('http://127.0.0.1:8099/health',timeout=5).status\n"
                "except urllib.error.HTTPError as error: status=error.code\n"
                "except OSError: status=None\n"
                "print(json.dumps({'status':status}))")
# Digest of the certificate the App actually serves to the Controller on 8443.
TLS_PROBE = ("import hashlib,json,ssl\n"
             "try: value={'tls_sha256':hashlib.sha256(ssl.PEM_cert_to_DER_cert("
             "ssl.get_server_certificate(('127.0.0.1',8443),timeout=5))).hexdigest()}\n"
             "except (OSError,ValueError) as error: value={'tls_error_type':type(error).__name__}\n"
             "print(json.dumps(value))")
# Runs inside the built App. Reads only the image's packaged source and process
# attributes; the product validator performs no network access.
APP_PROBE = r'''
import json
from pathlib import Path
value = {}
try:
    value['apparmor_profile'] = Path('/proc/self/attr/current').read_text().strip()[:128]
except OSError:
    value['apparmor_profile'] = None
value['docker_socket_present'] = any(Path(name).exists() for name in ('/var/run/docker.sock', '/run/docker.sock'))
try:
    value['source_lock'] = json.loads(Path('/opt/agentic-worker/source.json').read_text())
except (OSError, ValueError):
    value['source_lock'] = None
try:
    from agentic_runner.home_service import validate_source
    value['validated_commit'] = validate_source()['commit']
except Exception as error:
    value['validated_commit'] = None
    value['validation_error_type'] = type(error).__name__
print(json.dumps(value))
'''


def dockerfile_bases(text):
    """Every FROM must name an exact registry digest; return unique references in order."""
    bases = []
    for line in text.splitlines():
        stripped = line.strip()
        if not re.match(r'(?i:FROM)\b', stripped):
            continue
        match = PINNED_FROM.fullmatch(stripped)
        assert match, 'runner_dockerfile_base_not_digest_pinned'
        if match[1] not in bases:
            bases.append(match[1])
    assert bases, 'runner_dockerfile_base_missing'
    return bases


def head_runner_entries(raw):
    """Parse `git ls-tree -r -z HEAD -- runner`: only regular blobs strictly below runner/."""
    entries = []
    for record in raw.split(b'\0'):
        if not record:
            continue
        try:
            meta, separator, path = record.partition(b'\t')
            mode, kind, _ = meta.decode().split(' ')
            name = path.decode()
        except (UnicodeDecodeError, ValueError):
            raise AssertionError('runner_source_listing_invalid') from None
        assert separator and kind == 'blob' and mode in ('100644', '100755'), 'runner_source_entry_not_regular_file'
        parts = name.split('/')
        assert (parts[0] == 'runner' and len(parts) > 1
                and all(part not in ('', '.', '..') for part in parts)), 'runner_source_path_invalid'
        entries.append((name, int(mode[-3:], 8)))
    assert entries, 'runner_source_empty'
    return entries


def source_archive(entries, read, destination, *, mtime):
    """Write exact bytes as agentic_runner/; return a tree digest over mode, sha256 and path."""
    tree, directories = hashlib.sha256(), set()

    def member(name, **fields):
        info = tarfile.TarInfo(APP_DIR + ('/' + name if name else ''))
        info.uid = info.gid = 0
        info.uname = info.gname = 'root'
        info.mtime = mtime
        for key, value in fields.items():
            setattr(info, key, value)
        return info

    with tarfile.open(destination, 'w:gz') as archive:
        archive.addfile(member('', type=tarfile.DIRTYPE, mode=0o755))
        for name, mode in sorted(entries):
            relative = name.split('/', 1)[1]
            parents = relative.split('/')[:-1]
            for index in range(1, len(parents) + 1):
                directory = '/'.join(parents[:index])
                if directory not in directories:
                    directories.add(directory)
                    archive.addfile(member(directory, type=tarfile.DIRTYPE, mode=0o755))
            data = read(name)
            archive.addfile(member(relative, size=len(data), mode=mode), io.BytesIO(data))
            tree.update(f'{mode:o} {hashlib.sha256(data).hexdigest()} {relative}\n'.encode())
    return tree.hexdigest()


def cli_version(raw):
    """Exactly one version token in bounded CLI output, otherwise unknown."""
    if not isinstance(raw, bytes) or len(raw) > 512:
        return None
    found = set(re.findall(rb'(?<![0-9.])([0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6})(?![0-9.])', raw))
    return found.pop().decode() if len(found) == 1 else None


def upstream_flags(last):
    """Sanitized ProbeHA.last_error: fixed boolean flags only."""
    if not isinstance(last, dict):
        return {}
    return {key: last[key] for key in ('job_missing', 'update_required', 'upstream_unknown_error')
            if type(last.get(key)) is bool}


def pairing_options(options):
    """State of the one-time pairing the real App publishes into its own options."""
    if options == {}:
        return 'absent'
    published = (isinstance(options, dict) and set(options) == PAIRING_KEYS
        and isinstance(options['pairing_fingerprint'], str) and HEX64.fullmatch(options['pairing_fingerprint']) is not None
        and isinstance(options['pairing_code'], str) and PAIRING_CODE.fullmatch(options['pairing_code']) is not None)
    return 'published' if published else 'invalid'


def real_app_contract(info, config, slug, *, configured=True):
    # An unpaired real App publishes its one-time pairing into its own options;
    # the shared fixture contract expects empty options. Every assertion below
    # sees only public contract fields, so a pytest failure diff cannot print
    # the pairing code or the ingress entry.
    state = pairing_options(info.get('options'))
    assert state != 'invalid', 'runner_app_options_not_pairing_shape'
    assert configured or state == 'absent', 'runner_app_options_before_start_not_empty'
    public = {key: info[key] for key in CONTRACT_KEYS if key in info} | {'options': {}}
    value = app_contract(public, config, slug, configured=configured)
    assert public.get('privileged') == ['SYS_ADMIN'], 'runner_app_capabilities_changed'
    assert public.get('network') == {'8443/tcp': 8443}, 'runner_app_network_changed'
    assert public.get('auth_api') is False, 'runner_app_auth_api_enabled'
    assert public.get('ingress') is True, 'runner_app_ingress_missing'
    return {**value, 'network': {'8443/tcp': 8443}, 'ingress': True, 'auth_api': False, 'pairing_options': state}


class PollingReads:
    """Install observation only reads: transport loss is retried within a bound, never an effect.

    HAError stays semantic (a cleaned-up job is not found) and is never retried here.
    App info reaches the shared observer as public contract fields only, so its
    failure diff cannot print fields such as the ingress entry.
    """

    def __init__(self, ha, *, attempts=3, delay=5):
        self.ha, self.attempts, self.delay = ha, attempts, delay

    async def request(self, method, path, data=None, **kwargs):
        assert method == 'GET', 'install_observer_effect_forbidden'
        for attempt in range(1, self.attempts + 1):
            try:
                value = await self.ha.request(method, path, data, **kwargs)
                break
            except (aiohttp.ClientError, TimeoutError):
                if attempt == self.attempts:
                    raise
                await asyncio.sleep(self.delay)
        if re.fullmatch(r'/addons/[a-z0-9_]+/info', path) and isinstance(value, dict):
            return {key: value[key] for key in (*CONTRACT_KEYS, 'state', 'options') if key in value}
        return value


def app_probe_checks(raw, slug, lock):
    """Fixed comparison results only; never the raw profile, lock body or error text."""
    result = {'custom_apparmor_enforcing': raw.get('apparmor_profile') == slug + ' (enforce)',
              'docker_socket_absent': raw.get('docker_socket_present') is False,
              'source_lock_matches': isinstance(lock, dict) and raw.get('source_lock') == lock,
              'packaged_source_validated': isinstance(lock, dict) and isinstance(lock.get('commit'), str)
                  and raw.get('validated_commit') == lock['commit']}
    if raw.get('validation_error_type') in {'SandboxError', 'ImportError', 'ModuleNotFoundError', 'OSError', 'Exception'}:
        result['validation_error_type'] = raw['validation_error_type']
    return result


def docker_socket_mounted(raw):
    mounts = raw.get('Mounts') or []
    binds = (raw.get('HostConfig') or {}).get('Binds') or []
    names = [mount.get(key) for mount in mounts if isinstance(mount, dict) for key in ('Source', 'Destination')]
    names += [part for bind in binds if isinstance(bind, str) for part in bind.split(':')[:2]]
    return any(isinstance(name, str) and name.rstrip('/').endswith('docker.sock') for name in names)


_DROP = object()


def _plain(value, depth=0):
    """Nested trusted projections: bounded scalars and fixed-shape keys, no paths or free text."""
    if value is None or type(value) is bool or type(value) is int:
        return value
    if type(value) is float:
        return value if math.isfinite(value) else _DROP
    if isinstance(value, str):
        return value if value in FIXED_TEXT or SAFE_TEXT.fullmatch(value) else _DROP
    if depth < 4 and isinstance(value, list):
        return [item for item in (_plain(item, depth + 1) for item in value[:64]) if item is not _DROP]
    if depth < 4 and isinstance(value, dict):
        kept = {key: _plain(item, depth + 1) for key, item in value.items() if isinstance(key, str) and SAFE_KEY.fullmatch(key)}
        return {key: item for key, item in kept.items() if item is not _DROP}
    return _DROP


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value < 10**7


def _version(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9]{4}\.[0-9]{1,2}\.[0-9][0-9a-z.]{0,15}', value) is not None


def _bool_map(names):
    return lambda value: (isinstance(value, dict) and
        all(key in names and type(item) is bool for key, item in value.items()))


FIELDS = {
    'kind': lambda value: value == KIND, 'delivery': lambda value: value == DELIVERY,
    'haos_image': lambda value: value == '18.3', 'observed_from': lambda value: value == OBSERVED_FROM,
    'status': lambda value: value in {'running', 'passed', 'failed', 'interrupted'},
    'phase': lambda value: isinstance(value, str) and re.fullmatch(r'[a-z_]{3,64}', value) is not None,
    'core': _version, 'supervisor': _version,
    'source_commit': lambda value: isinstance(value, str) and HEX40.fullmatch(value) is not None,
    'worker_source_commit': lambda value: isinstance(value, str) and HEX40.fullmatch(value) is not None,
    'runner_git_tree': lambda value: isinstance(value, str) and HEX40.fullmatch(value) is not None,
    'runtime_source_uncommitted': lambda value: type(value) is bool,
    'delivered_file_count': lambda value: type(value) is int and 0 < value <= 10000,
    'delivered_tree_sha256': lambda value: isinstance(value, str) and HEX64.fullmatch(value) is not None,
    'app_config_sha256': lambda value: isinstance(value, str) and HEX64.fullmatch(value) is not None,
    'app_apparmor_sha256': lambda value: isinstance(value, str) and HEX64.fullmatch(value) is not None,
    'preloaded_images': lambda value: (isinstance(value, list) and len(value) == 3 and
        all(isinstance(item, str) and PINNED_REF.fullmatch(item) for item in value)),
    'preload_digest_resolution': _bool_map({'builder_tag', 'node_digest', 'python_digest'}),
    'live_build_downloads': lambda value: value == list(LIVE_BUILD_DOWNLOADS),
    'upstream_error': _bool_map({'job_missing', 'update_required', 'upstream_unknown_error'}),
    'install_confirmation': lambda value: value in {'supervisor_job_and_app', 'app_postcondition_job_unavailable'},
    'install_job_reported_errors': lambda value: type(value) is bool,
    'build_seconds': _number, 'duration_seconds': _number, 'command_timeout_seconds': _number,
    'installed_image_id': lambda value: isinstance(value, str) and re.fullmatch(r'sha256:[a-f0-9]{64}', value) is not None,
    'health_status': lambda value: type(value) is int and 100 <= value <= 599,
    'codex_version': lambda value: isinstance(value, str) and re.fullmatch(r'[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6}', value) is not None,
    'checks': _bool_map(CHECK_NAMES), 'cleanup': _bool_map(CLEANUP_NAMES),
    'limitations': lambda value: value == list(LIMITATIONS),
    'error_type': lambda value: isinstance(value, str) and re.fullmatch(r'[A-Za-z]{1,40}', value) is not None,
    'error_code': lambda value: isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9_.-]{2,120}', value) is not None,
    'errno': lambda value: value is None or (type(value) is int and 0 < value < 4096),
    'failure_app_state': lambda value: value in {'started', 'stopped', 'error', 'unknown'},
    'failure_app_observation_error': lambda value: isinstance(value, str) and re.fullmatch(r'[A-Za-z]{1,40}', value) is not None,
    'failure_hold_seconds': lambda value: type(value) is int and 0 <= value <= 180,
}
NESTED = frozenset({'app_contract', 'app_container_observation', 'app_probe', 'haos_vm_profile',
                    'owned_runtime_observation', 'store_catalog', 'supervisor_log_signatures'})


def evidence_projection(raw):
    """Only allow-listed, validated public fields reach the evidence file."""
    result = {}
    for key, value in raw.items():
        if key in FIELDS:
            if FIELDS[key](value):
                result[key] = value
        elif key in NESTED:
            projected = _plain(value)
            if isinstance(projected, dict):
                result[key] = projected
    return result


async def test_real_supervisor_builds_home_coding_app(tmp_path, request):
    if not request.config.getoption('--haos-vm'):
        pytest.skip('Actual Supervisor build of the Home coding App needs --haos-vm; run heavy suites serially')
    assert str(tmp_path).startswith(str(Path.home() / 'agent-work/tmp/pytest'))
    hold_seconds = failure_hold_seconds(os.environ.get('AGENTIC_RUNNER_BUILD_FAILURE_HOLD_SECONDS', '0'))
    label = uuid.uuid4().hex[:10]
    vm, network = ('agentic-runner-build-' + kind + '-' + label for kind in ('vm', 'net'))
    started, wall_started = time.monotonic(), time.time()
    report = ROOT / 'evidence/runner-supervisor-build.json'
    progress = ROOT / 'evidence/runner-supervisor-build-progress.json'
    evidence = {'kind': KIND, 'delivery': DELIVERY, 'haos_image': '18.3', 'observed_from': OBSERVED_FROM,
                'status': 'running', 'live_build_downloads': list(LIVE_BUILD_DOWNLOADS),
                'limitations': list(LIMITATIONS), 'checks': {}}
    ha, ssh, build_started = None, None, None

    def phase(value):
        evidence['phase'] = value
        progress.write_text(json.dumps({'phase': value, 'elapsed_seconds': round(time.monotonic() - started)}, indent=2) + '\n')

    def git(*args):
        return process(['git', '-C', str(ROOT), *args])

    def remote(command, *, timeout=30, input=None):
        return process([*ssh, command], timeout=timeout, input=input)

    def write_report():
        report.write_text(json.dumps(evidence_projection(evidence), indent=2) + '\n')

    try:
        phase('prepare_exact_head_source')
        evidence['source_commit'] = git('rev-parse', 'HEAD').decode().strip()
        evidence['runner_git_tree'] = git('rev-parse', 'HEAD:runner').decode().strip()
        evidence['runtime_source_uncommitted'] = bool(git('status', '--porcelain', '--', 'runner').strip())
        profile = json.loads(git('show', 'HEAD:' + VM_PROFILE))
        size = {key: profile.get(key) for key in ('memory_mib', 'vcpus', 'disk_gib')}
        assert all(type(value) is int and value > 0 for value in size.values()), 'laptop_vm_profile_invalid'
        evidence['haos_vm_profile'] = {'profile': profile.get('id'), **size}
        # Exact committed bytes only: never the working tree or the index.
        entries = head_runner_entries(git('ls-tree', '-r', '-z', 'HEAD', '--', 'runner'))
        blobs = {name: git('show', 'HEAD:' + name) for name, _ in entries}
        required = {'runner/' + name for name in ('Dockerfile', 'config.yaml', 'apparmor.txt',
                                                  'coding-source.lock.json', 'build_worker_source.py')}
        assert required <= set(blobs), 'runner_source_required_file_missing'
        config = yaml.safe_load(blobs['runner/config.yaml'])
        assert config.get('slug') == APP_DIR and config.get('version') == VERSION, 'runner_config_identity_changed'
        source_lock = json.loads(blobs['runner/coding-source.lock.json'])
        assert (isinstance(source_lock, dict) and isinstance(source_lock.get('commit'), str)
                and HEX40.fullmatch(source_lock['commit'])), 'runner_coding_source_lock_invalid'
        providers = json.loads(git('show', 'HEAD:providers.lock.json'))
        codex_pin = providers['providers']['codex']['version']
        bases = dockerfile_bases(blobs['runner/Dockerfile'].decode())
        assert (sorted(bases) == sorted([providers['bases']['node'], providers['bases']['python']])
                and PYTHON in bases), 'runner_dockerfile_bases_differ_from_lock'
        node = next(base for base in bases if base != PYTHON)
        commit_time = int(git('show', '-s', '--format=%ct', 'HEAD').decode().strip())
        evidence.update(delivered_file_count=len(entries),
            delivered_tree_sha256=source_archive(entries, blobs.__getitem__, tmp_path / 'runner.tar.gz', mtime=commit_time),
            app_config_sha256=hashlib.sha256(blobs['runner/config.yaml']).hexdigest(),
            app_apparmor_sha256=hashlib.sha256(blobs['runner/apparmor.txt']).hexdigest())
        phase('local_pinned_images_present')
        builder_tag = BUILDER.split('@', 1)[0]
        for reference in (BUILDER, node, PYTHON):
            try:
                docker('image', 'inspect', '--format', '{{.Id}}', reference)
            except AssertionError:
                # Present locally or fail: this harness never pulls.
                raise AssertionError('pinned_build_image_not_present_locally') from None
        docker('save', '-o', str(tmp_path / 'bases.tar'), PYTHON, node, builder_tag, timeout=180)
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
            '-netdev user,id=net,hostfwd=tcp:0.0.0.0:8123-:80,hostfwd=tcp:0.0.0.0:2222-:22222 '
            '-device virtio-net-pci,netdev=net -display none -serial file:/lab/serial.log')
        ports = json.loads(docker('inspect', vm))[0]['NetworkSettings']['Ports']
        base = 'http://127.0.0.1:' + ports['8123/tcp'][0]['HostPort']
        ssh = ['ssh', '-i', str(tmp_path / 'key'), '-p', ports['2222/tcp'][0]['HostPort'], '-o', 'BatchMode=yes',
            '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=no', '-o', f'UserKnownHostsFile={tmp_path / "known_hosts"}',
            '-o', 'ConnectTimeout=5', 'root@127.0.0.1']
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8),
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
        evidence['checks']['supervisor_healthy'] = True
        phase('preload_builder_and_pinned_bases')
        for _ in range(5):
            if subprocess.run([*ssh, 'true'], capture_output=True, timeout=15).returncode == 0:
                break
            await asyncio.sleep(3)
        else:
            raise AssertionError('haos_ssh_readiness_timeout')
        with (tmp_path / 'bases.tar').open('rb') as stream:
            loaded = subprocess.run([*ssh, 'docker load'], stdin=stream, capture_output=True, timeout=240)
        assert loaded.returncode == 0, 'haos_base_preload_failed'
        evidence['preloaded_images'] = [BUILDER, node, PYTHON]
        evidence['checks']['pinned_bases_preloaded'] = True
        # Observation only: whether the loaded digests resolve locally on HAOS.
        # An unresolved digest would make the builder fetch that base live.
        evidence['preload_digest_resolution'] = {role: subprocess.run(
            [*ssh, 'docker image inspect --format ' + shlex.quote('{{.Id}}') + ' ' + shlex.quote(reference)],
            capture_output=True, timeout=30).returncode == 0
            for role, reference in (('builder_tag', builder_tag), ('node_digest', node), ('python_digest', PYTHON))}
        phase('deliver_exact_head_runner_source')
        with (tmp_path / 'runner.tar.gz').open('rb') as stream:
            delivered = subprocess.run([*ssh, 'mkdir -p /mnt/data/supervisor/apps/local && tar xzf - -C /mnt/data/supervisor/apps/local'],
                                       stdin=stream, capture_output=True, timeout=60)
        assert delivered.returncode == 0, 'runner_source_delivery_failed'
        evidence['checks']['exact_head_source_delivered'] = True
        phase('supervisor_store_catalog')
        # A single store call failed transiently once in the 0.9.0 run. Reload and
        # read are bounded; each failed attempt keeps only its step, type and fixed flags.
        reloaded, catalog, failures = False, None, []
        for attempt in range(1, 11):
            ha.last_error = None
            try:
                if not reloaded:
                    await ha.request('POST', '/store/reload', supervisor=True, timeout=60)
                    reloaded = True
                catalog = await ha.request('GET', f'/store/addons/{SLUG}/info', supervisor=True)
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
        assert catalog.get('version_latest') == VERSION and catalog.get('build') is True, 'runner_catalog_changed'
        evidence['checks']['store_catalog_build_version'] = True
        phase('supervisor_build_and_install')
        build_started = time.monotonic()
        # Exactly one install effect. A lost reply is observed, never retried.
        receipt = await ha.request('POST', f'/store/addons/{SLUG}/install', {'background': True}, supervisor=True)
        assert isinstance(receipt, dict) and isinstance(receipt.get('job_id'), str), 'supervisor_install_job_missing'
        try:
            evidence['install_confirmation'] = await observe_install(PollingReads(ha), SLUG, receipt['job_id'], config,
                attempts=JOB_TIMEOUT // INSTALL_POLL_SECONDS, delay=INSTALL_POLL_SECONDS)
            evidence['install_job_reported_errors'] = False
        except AssertionError as error:
            if str(error) == 'supervisor_app_install_failed':
                evidence['install_job_reported_errors'] = True
            raise
        finally:
            evidence['build_seconds'] = round(time.monotonic() - build_started, 1)
        evidence['checks']['supervisor_build_install_completed'] = True
        phase('real_app_contract')
        info = await ha.request('GET', f'/addons/{SLUG}/info', supervisor=True)
        evidence['app_contract'] = real_app_contract(info, config, SLUG, configured=False)
        evidence['checks']['real_app_contract_installed'] = True
        phase('configure_and_start_app')
        await ha.request('POST', f'/addons/{SLUG}/options', {'options': {}, 'boot': 'auto', 'watchdog': False,
            'auto_update': False}, supervisor=True)
        await ha.request('POST', f'/addons/{SLUG}/start', supervisor=True, timeout=120)
        # From here the App info holds the pairing code and the inspect output holds
        # SUPERVISOR_TOKEN: assertions only see fixed values extracted from them.
        deadline = time.monotonic() + 180
        while True:
            state = (await ha.request('GET', f'/addons/{SLUG}/info', supervisor=True)).get('state')
            if state == 'started':
                break
            assert state != 'error', 'runner_app_start_failed'
            assert time.monotonic() < deadline, 'runner_app_start_timeout'
            await asyncio.sleep(3)
        evidence['checks']['app_started'] = True
        phase('app_self_published_pairing')
        # An unpaired App publishes its one-time pairing into its own options while
        # hassio_api stays false. The values are compared, never exported.
        for _ in range(20):
            info = await ha.request('GET', f'/addons/{SLUG}/info', supervisor=True)
            published = pairing_options(info.get('options')) == 'published'
            if published:
                break
            await asyncio.sleep(3)
        assert published, 'runner_app_pairing_not_published'
        fingerprint = info['options']['pairing_fingerprint']
        evidence['checks']['pairing_published_to_own_options'] = True
        evidence['app_contract'] = real_app_contract(info, config, SLUG)
        del info
        evidence['checks']['real_app_contract_configured'] = True
        phase('app_container_contract')
        installed = json.loads(remote('docker inspect ' + APP_CONTAINER))[0]
        evidence['app_container_observation'] = container_projection(installed, SLUG)
        container_contract(installed, SLUG)
        evidence['checks']['container_contract'] = True
        mounted = docker_socket_mounted(installed)
        assert not mounted, 'runner_app_docker_socket_mounted'
        evidence['checks']['docker_socket_not_mounted'] = True
        image = installed.get('Image')
        assert isinstance(image, str) and re.fullmatch(r'sha256:[a-f0-9]{64}', image), 'runner_installed_image_identity_invalid'
        evidence['installed_image_id'] = image
        labels = json.loads(remote('docker image inspect ' + shlex.quote(image)))[0].get('Config', {}).get('Labels') or {}
        assert labels.get('io.hass.version') == VERSION, 'runner_image_version_label_changed'
        evidence['checks']['image_version_label'] = True
        phase('app_health')
        health = {}
        for _ in range(30):
            try:
                health = json.loads(remote('docker exec ' + APP_CONTAINER + ' python -c ' + shlex.quote(HEALTH_PROBE), timeout=20))
            except (AssertionError, ValueError, subprocess.TimeoutExpired):
                health = {}
            if isinstance(health, dict) and health.get('status') == 200:
                break
            await asyncio.sleep(2)
        status = health.get('status') if isinstance(health, dict) else None
        if type(status) is int:
            evidence['health_status'] = status
        assert status == 200, 'runner_app_health_not_200'
        evidence['checks']['app_health_200'] = True
        served = json.loads(remote('docker exec ' + APP_CONTAINER + ' python -c ' + shlex.quote(TLS_PROBE), timeout=30))
        matches = isinstance(served, dict) and served.get('tls_sha256') == fingerprint
        assert matches, 'runner_pairing_fingerprint_differs_from_served_tls'
        evidence['checks']['pairing_fingerprint_matches_tls'] = True
        phase('app_pinned_cli_and_worker_source')
        evidence['codex_version'] = cli_version(remote('docker exec ' + APP_CONTAINER + ' codex --version', timeout=60))
        assert evidence['codex_version'] == codex_pin, 'runner_codex_version_differs_from_lock'
        evidence['checks']['codex_version_pinned'] = True
        probe = app_probe_checks(json.loads(remote('docker exec ' + APP_CONTAINER + ' python -c ' + shlex.quote(APP_PROBE),
                                                   timeout=90)), SLUG, source_lock)
        evidence['app_probe'] = probe
        assert probe['source_lock_matches'] and probe['packaged_source_validated'], 'runner_worker_source_differs_from_lock'
        evidence['worker_source_commit'] = source_lock['commit']
        evidence['checks']['worker_source_matches_lock'] = True
        assert probe['custom_apparmor_enforcing'], 'runner_app_apparmor_not_custom_enforcing'
        evidence['checks']['custom_apparmor_enforcing'] = True
        assert probe['docker_socket_absent'], 'runner_app_docker_socket_present'
        evidence['checks']['docker_socket_absent'] = True
        assert set(evidence['checks']) == CHECK_NAMES and all(evidence['checks'].values()), 'runner_build_checks_incomplete'
        evidence.update(status='passed', phase='completed')
    except BaseException as error:
        interrupted = isinstance(error, (KeyboardInterrupt, asyncio.CancelledError))
        evidence.update(status='interrupted' if interrupted else 'failed', **failure_receipt(error, source_files=CODE_SOURCES))
        evidence['owned_runtime_observation'] = lifecycle_observation({'vm': vm}, wall_started)
        if isinstance(error, subprocess.TimeoutExpired):
            evidence['command_timeout_seconds'] = error.timeout
        if ha is not None:
            if ha.last_error is not None:
                evidence['upstream_error'] = upstream_flags(ha.last_error)
            try:
                async with asyncio.timeout(10):
                    observed = await ha.request('GET', f'/addons/{SLUG}/info', supervisor=True, timeout=5)
                state = observed.get('state') if isinstance(observed, dict) else None
                evidence['failure_app_state'] = state if state in {'started', 'stopped', 'error'} else 'unknown'
            except Exception as observation_error:
                evidence['failure_app_observation_error'] = type(observation_error).__name__
        if ssh is not None:
            with contextlib.suppress(Exception):
                evidence['app_container_observation'] = container_projection(
                    json.loads(remote('docker inspect ' + APP_CONTAINER, timeout=10))[0], SLUG)
            try:
                logs = subprocess.run([*ssh, 'ha supervisor logs | tail -80'], capture_output=True, timeout=15)
                text = logs.stdout[-65536:].decode(errors='replace').lower()
                evidence['supervisor_log_signatures'] = {name: pattern in text for name, pattern in
                    {'update_required': 'needs to be updated', 'job_missing': 'job does not exist',
                     'build_failure': 'build failed', 'registry_failure': 'failed to resolve',
                     'no_space': 'no space left', 'permission': 'permission denied',
                     'network_failure': 'temporary failure', 'not_installed': 'not installed'}.items()}
                evidence['supervisor_log_signatures']['bytes_observed'] = len(text.encode())
            except Exception:
                evidence['supervisor_log_signatures'] = {'unavailable': True}
        # Explicit diagnostic window is test-only, bounded and always followed
        # by normal cleanup. It never changes the failure or resubmits effects.
        evidence['failure_hold_seconds'] = hold_seconds if not interrupted else 0
        write_report()
        if not interrupted and hold_seconds:
            progress.write_text(json.dumps({'phase': 'failure_diagnostic_hold', 'failed_phase': evidence['phase'],
                'error_code': evidence['error_code'], 'seconds': hold_seconds}, indent=2) + '\n')
            for _ in range(hold_seconds):
                await asyncio.sleep(1)
        raise
    finally:
        if ha is not None:
            with contextlib.suppress(Exception):
                await ha.close()
        cleanup = {}
        try:
            subprocess.run(['docker', 'rm', '-fv', vm], capture_output=True, timeout=30)
            check = subprocess.run(['docker', 'ps', '--all', '--quiet', '--no-trunc', '--filter', 'name=^/' + vm + '$'],
                capture_output=True, timeout=5)
            cleanup['vm_removed'] = check.returncode == 0 and not check.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            cleanup['vm_removed'] = False
        try:
            owned = subprocess.run(['docker', 'run', '--rm', '--name', 'agentic-runner-build-clean-' + label,
                '-v', f'{tmp_path}:/lab', 'agentic-home-builder:0.2.0',
                'chown', '-R', f'{os.getuid()}:{os.getgid()}', '/lab'], capture_output=True, timeout=60)
            cleanup['disk_fixture_ownership_restored'] = owned.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            cleanup['disk_fixture_ownership_restored'] = False
        try:
            subprocess.run(['docker', 'network', 'rm', network], capture_output=True, timeout=15)
            check = subprocess.run(['docker', 'network', 'ls', '--quiet', '--filter', 'name=^' + network + '$'],
                capture_output=True, timeout=5)
            cleanup['network_removed'] = check.returncode == 0 and not check.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            cleanup['network_removed'] = False
        if evidence['status'] == 'running':
            evidence.update(status='interrupted', error_type='BaseException')
        # Keep the original failure; a leaked owned resource fails an otherwise passing run.
        leaked = evidence['status'] == 'passed' and not all(cleanup.values())
        if leaked:
            evidence.update(status='failed', error_type='AssertionError', error_code='owned_runner_build_resources_not_removed')
        evidence['cleanup'] = cleanup
        evidence['duration_seconds'] = round(time.monotonic() - started, 3)
        write_report()
        progress.unlink(missing_ok=True)
        assert not leaked, 'owned_runner_build_resources_not_removed'


NODE = 'node:24-bookworm-slim@sha256:' + 'd' * 64
PY = 'python:3.13-slim-bookworm@sha256:' + 'a' * 64


def test_shipped_dockerfile_bases_are_the_locked_digests():
    text = process(['git', '-C', str(ROOT), 'show', 'HEAD:runner/Dockerfile']).decode()
    lock = json.loads(process(['git', '-C', str(ROOT), 'show', 'HEAD:providers.lock.json']))
    bases = dockerfile_bases(text)
    assert bases == [lock['bases']['node'], lock['bases']['python']] and PYTHON in bases


@pytest.mark.parametrize('text', [
    'FROM python:3.13-slim-bookworm\nRUN true\n',
    'RUN true\n',
    '',
    f'FROM {NODE} AS agents\nFROM agents\n',
    f'FROM --platform=linux/amd64 {PY}\n',
    'FROM python@sha256:' + 'a' * 64 + '\n',
    'FROM python:3.13@sha256:' + 'a' * 63 + '\n',
    f'from {PY} as x extra\n',
    'FROM python:3.13-slim-bookworm \\\n  @sha256:' + 'a' * 64 + '\n',
])
def test_dockerfile_bases_reject_unpinned_or_missing_from(text):
    with pytest.raises(AssertionError):
        dockerfile_bases(text)


def test_dockerfile_bases_deduplicate_pinned_stages():
    text = f'FROM {NODE} AS agents\nRUN x\nfrom {NODE} as web\nFROM {PY}\nCOPY --from=agents /a /a\n'
    assert dockerfile_bases(text) == [NODE, PY]


def test_head_runner_listing_is_exact_regular_files():
    entries = head_runner_entries(process(['git', '-C', str(ROOT), 'ls-tree', '-r', '-z', 'HEAD', '--', 'runner']))
    names = {name for name, _ in entries}
    assert {'runner/Dockerfile', 'runner/config.yaml', 'runner/apparmor.txt', 'runner/coding-source.lock.json'} <= names
    assert all(name.startswith('runner/') and mode in (0o644, 0o755) for name, mode in entries)


@pytest.mark.parametrize('record', [
    b'120000 blob ' + b'a' * 40 + b'\trunner/link',
    b'160000 commit ' + b'a' * 40 + b'\trunner/sub',
    b'100644 blob ' + b'a' * 40 + b'\thost/install.py',
    b'100644 blob ' + b'a' * 40 + b'\trunner/../secret',
    b'100644 blob ' + b'a' * 40 + b'\trunner',
    b'100644 blob ' + b'a' * 40 + b'runner/no-tab',
    b'100644 blob\trunner/short',
    b'',
])
def test_head_runner_listing_rejects_non_regular_or_escaping_entries(record):
    good = b'100644 blob ' + b'b' * 40 + b'\trunner/config.yaml\0'
    with pytest.raises(AssertionError):
        head_runner_entries((good + record + b'\0') if record else b'')


def test_source_archive_delivers_exact_bytes_modes_and_tree_digest(tmp_path):
    blobs = {'runner/Dockerfile': b'FROM x\n', 'runner/agentic_runner/app.py': b'print(1)\n',
             'runner/worker_runtime/agentic_host/cli.py': b'#!/usr/bin/env python\n'}
    entries = [('runner/worker_runtime/agentic_host/cli.py', 0o755), ('runner/Dockerfile', 0o644),
               ('runner/agentic_runner/app.py', 0o644)]
    first = source_archive(entries, blobs.__getitem__, tmp_path / 'a.tar.gz', mtime=1700000000)
    second = source_archive(list(reversed(entries)), blobs.__getitem__, tmp_path / 'b.tar.gz', mtime=1700000000)
    assert first == second and HEX64.fullmatch(first)
    with tarfile.open(tmp_path / 'a.tar.gz') as archive:
        members = {item.name: item for item in archive.getmembers()}
        assert set(members) == {'agentic_runner', 'agentic_runner/Dockerfile', 'agentic_runner/agentic_runner',
            'agentic_runner/agentic_runner/app.py', 'agentic_runner/worker_runtime',
            'agentic_runner/worker_runtime/agentic_host', 'agentic_runner/worker_runtime/agentic_host/cli.py'}
        for name, mode in entries:
            item = members['agentic_runner/' + name.split('/', 1)[1]]
            assert item.isfile() and item.mode == mode and item.uid == 0 and item.mtime == 1700000000
            assert archive.extractfile(item).read() == blobs[name]
    changed = source_archive(entries, {**blobs, 'runner/Dockerfile': b'FROM y\n'}.__getitem__, tmp_path / 'c.tar.gz',
                             mtime=1700000000)
    assert changed != first


@pytest.mark.parametrize('raw,expected', [
    (b'codex-cli 0.157.1\n', '0.157.1'), (b'0.157.1', '0.157.1'),
    (b'codex-cli 0.157.1 (node 24.1.0)\n', None), (b'codex-cli\n', None), (b'x' * 513, None), ('0.1.2', None),
])
def test_cli_version_requires_one_bounded_token(raw, expected):
    assert cli_version(raw) == expected


def test_upstream_flags_keep_only_fixed_booleans():
    raw = {'job_missing': False, 'update_required': True, 'message_bytes': 99, 'upstream_unknown_error': 'secret',
           'message': 'private upstream body'}
    assert upstream_flags(raw) == {'job_missing': False, 'update_required': True}
    assert upstream_flags(None) == {}


def real_info():
    return {'slug': SLUG, 'version': VERSION, 'hassio_api': False, 'homeassistant_api': False, 'auth_api': False,
            'docker_api': False, 'full_access': False, 'host_network': False, 'host_pid': False, 'apparmor': 'profile',
            'privileged': ['SYS_ADMIN'], 'devices': ['/dev/fuse'], 'options': {}, 'auto_update': False, 'watchdog': False,
            'network': {'8443/tcp': 8443}, 'ingress': True, 'state': 'started'}


def test_real_app_contract_accepts_shipped_config():
    config = yaml.safe_load(process(['git', '-C', str(ROOT), 'show', 'HEAD:runner/config.yaml']))
    result = real_app_contract(real_info(), config, SLUG)
    assert result['network'] == {'8443/tcp': 8443} and result['ingress'] is True and result['devices'] == ['/dev/fuse']


@pytest.mark.parametrize('change', [('privileged', ['SYS_ADMIN', 'NET_ADMIN']), ('network', {'8443/tcp': 9443}),
    ('network', None), ('auth_api', True), ('ingress', False), ('hassio_api', True), ('homeassistant_api', True),
    ('docker_api', True), ('full_access', True), ('host_network', True), ('host_pid', True), ('apparmor', 'disable'),
    ('devices', []), ('version', '0.0.1'), ('auto_update', True)])
def test_real_app_contract_rejects_widened_or_changed_app(change):
    config = yaml.safe_load(process(['git', '-C', str(ROOT), 'show', 'HEAD:runner/config.yaml']))
    info = {**real_info(), change[0]: change[1]}
    with pytest.raises(AssertionError):
        real_app_contract(info, config, SLUG)


def test_app_probe_checks_compare_without_exporting_raw_values():
    lock = {'repo_id': 'agentic-home', 'repository': 'https://github.com/teddashh/agentic-smart-home.git', 'commit': 'c' * 40}
    raw = {'apparmor_profile': SLUG + ' (enforce)', 'docker_socket_present': False, 'source_lock': dict(lock),
           'validated_commit': 'c' * 40}
    assert app_probe_checks(raw, SLUG, lock) == {'custom_apparmor_enforcing': True, 'docker_socket_absent': True,
        'source_lock_matches': True, 'packaged_source_validated': True}
    for mutation in ({'apparmor_profile': 'unconfined'}, {'apparmor_profile': SLUG + ' (complain)'},
                     {'apparmor_profile': 'docker-default (enforce)'}, {'docker_socket_present': True},
                     {'docker_socket_present': None}, {'source_lock': {**lock, 'commit': 'd' * 40}},
                     {'validated_commit': None, 'validation_error_type': '/private/path'}):
        result = app_probe_checks({**raw, **mutation}, SLUG, lock)
        assert not all(result[key] for key in ('custom_apparmor_enforcing', 'docker_socket_absent',
                                               'source_lock_matches', 'packaged_source_validated'))
        assert 'private' not in json.dumps(result) and 'unconfined' not in json.dumps(result)


@pytest.mark.parametrize('raw,expected', [
    ({'Mounts': [{'Source': '/mnt/data/supervisor/apps/data/x', 'Destination': '/data'}]}, False),
    ({'Mounts': [{'Source': '/run/docker.sock', 'Destination': '/var/run/docker.sock'}]}, True),
    ({'Mounts': [{'Source': '/x', 'Destination': '/run/docker.sock/'}]}, True),
    ({'HostConfig': {'Binds': ['/var/run/docker.sock:/sock:ro']}}, True),
    ({'Mounts': None, 'HostConfig': {'Binds': None}}, False),
])
def test_docker_socket_mount_detection(raw, expected):
    assert docker_socket_mounted(raw) is expected


def test_evidence_projection_drops_unknown_keys_and_private_values():
    home = str(Path.home())
    raw = {'kind': KIND, 'delivery': DELIVERY, 'status': 'failed', 'phase': 'supervisor_build_and_install',
           'source_commit': 'a' * 40, 'core': '2026.10.1', 'supervisor': '2026.10.0', 'build_seconds': 1234.5,
           'preloaded_images': [BUILDER, NODE, PYTHON], 'live_build_downloads': list(LIVE_BUILD_DOWNLOADS),
           'limitations': list(LIMITATIONS), 'checks': {'supervisor_healthy': True},
           'cleanup': {'vm_removed': True, 'network_removed': True, 'disk_fixture_ownership_restored': True},
           'error_code': 'supervisor_app_install_failed', 'installed_image_id': 'sha256:' + 'b' * 64,
           'upstream_error': {'job_missing': True}, 'runner_git_tree': 'e' * 40,
           'haos_vm_profile': {'profile': 'laptop-amd64', 'memory_mib': 4096, 'vcpus': 2, 'disk_gib': 64},
           'store_catalog': {'attempts': 2, 'failures': [{'attempt': 1, 'step': 'read', 'error_type': 'HAError',
               'job_missing': False, 'message': 'store body at ' + home}]},
           'app_container_observation': {'apparmor_profile': SLUG, 'device_cgroup_rules': ['c 10:229 rwm'],
               'cap_add': ['SYS_ADMIN'], 'env': 'SUPERVISOR_TOKEN=secret value', 'path': home + '/agent-work'},
           'app_contract': {'devices': ['/dev/fuse'], 'network': {'8443/tcp': 8443}, 'note': 'free text body'},
           # Everything below is private, unknown or malformed and must be dropped.
           'token': 'secret', 'upstream_body': {'message': 'raw'}, 'logs': 'private log',
           'source_tree': home + '/agent-work', 'haos_image': '18.4',
           'supervisor_log_signatures': {'build_failure': True, 'raw': 'build failed: secret at ' + home}}
    raw['checks']['unknown_check'] = True
    result = evidence_projection(raw)
    text = json.dumps(result)
    assert home not in text and 'secret' not in text and 'private' not in text and 'free text' not in text
    assert {'token', 'upstream_body', 'logs', 'source_tree', 'haos_image', 'checks'}.isdisjoint(result)
    assert result['app_container_observation'] == {'apparmor_profile': SLUG, 'device_cgroup_rules': ['c 10:229 rwm'],
                                                   'cap_add': ['SYS_ADMIN']}
    assert result['app_contract'] == {'devices': ['/dev/fuse'], 'network': {'8443/tcp': 8443}}
    assert result['supervisor_log_signatures'] == {'build_failure': True}
    assert result['store_catalog'] == {'attempts': 2, 'failures': [{'attempt': 1, 'step': 'read',
                                                                    'error_type': 'HAError', 'job_missing': False}]}
    assert result['cleanup'] == raw['cleanup'] and result['preloaded_images'] == [BUILDER, NODE, PYTHON]
    for key in ('kind', 'delivery', 'status', 'phase', 'source_commit', 'core', 'supervisor', 'build_seconds',
                'error_code', 'installed_image_id', 'upstream_error', 'live_build_downloads', 'limitations',
                'runner_git_tree', 'haos_vm_profile'):
        assert result[key] == raw[key]
    assert evidence_projection({'build_seconds': float('nan'), 'preloaded_images': ['python:latest'] * 3,
                                'runner_git_tree': 'HEAD', 'codex_version': 'codex-cli 1.2.3'}) == {}


class FlakyReads:
    def __init__(self, *errors, value=None):
        self.errors, self.calls, self.value = list(errors), [], value

    async def request(self, method, path, data=None, **kwargs):
        self.calls.append((method, path))
        if self.errors:
            raise self.errors.pop(0)
        return {'path': path} if self.value is None else self.value


async def test_install_polling_retries_only_bounded_transport_loss_on_reads():
    ha = FlakyReads(TimeoutError(), aiohttp.ClientConnectionError())
    assert await PollingReads(ha, delay=0).request('GET', '/jobs/x', supervisor=True) == {'path': '/jobs/x'}
    assert len(ha.calls) == 3
    with pytest.raises(TimeoutError):
        await PollingReads(FlakyReads(*[TimeoutError()] * 3), delay=0).request('GET', '/addons', supervisor=True)
    missing = FlakyReads(HAError('supervisor_job_missing'))
    with pytest.raises(HAError):
        await PollingReads(missing, delay=0).request('GET', '/jobs/x', supervisor=True)
    assert len(missing.calls) == 1
    effect = FlakyReads()
    with pytest.raises(AssertionError, match='install_observer_effect_forbidden'):
        await PollingReads(effect, delay=0).request('POST', '/store/addons/x/install', {'background': True})
    assert effect.calls == []


async def test_install_polling_passes_only_public_app_info_to_shared_observer():
    raw = {**real_info(), 'state': 'stopped', 'options': {}, 'ingress_entry': '/api/hassio_ingress/' + 'q' * 43,
           'ip_address': '172.30.33.2', 'long_description': 'free text'}
    value = await PollingReads(FlakyReads(value=raw), delay=0).request('GET', f'/addons/{SLUG}/info', supervisor=True)
    assert set(value) == {*CONTRACT_KEYS, 'state', 'options'} and value['state'] == 'stopped'
    listed = {'addons': [{'slug': SLUG}]}
    assert await PollingReads(FlakyReads(value=listed), delay=0).request('GET', '/addons', supervisor=True) == listed


CODE = 'fixturePairingCode_' + 'Z' * 24


def pairing_info(**changes):
    return {**real_info(), 'options': {'pairing_fingerprint': 'f' * 64, 'pairing_code': CODE},
            'ingress_entry': '/api/hassio_ingress/' + 'q' * 43, **changes}


@pytest.mark.parametrize('options,state', [
    ({}, 'absent'), ({'pairing_fingerprint': 'f' * 64, 'pairing_code': CODE}, 'published'),
    ({'pairing_fingerprint': 'F' * 64, 'pairing_code': CODE}, 'invalid'),
    ({'pairing_fingerprint': 'f' * 64, 'pairing_code': 'short'}, 'invalid'),
    ({'pairing_fingerprint': 'f' * 64, 'pairing_code': CODE, 'extra': 1}, 'invalid'),
    ({'pairing_code': CODE}, 'invalid'), (None, 'invalid'), ([], 'invalid'),
])
def test_pairing_options_states(options, state):
    assert pairing_options(options) == state


def test_real_app_contract_accepts_self_published_pairing_without_exporting_it():
    config = yaml.safe_load(process(['git', '-C', str(ROOT), 'show', 'HEAD:runner/config.yaml']))
    result = real_app_contract(pairing_info(), config, SLUG)
    assert result['pairing_options'] == 'published' and CODE not in json.dumps(result) and 'f' * 64 not in json.dumps(result)


@pytest.mark.parametrize('changes,configured', [
    ({'options': {'pairing_fingerprint': 'f' * 64, 'pairing_code': CODE, 'extra': CODE}}, True),
    ({}, False), ({'privileged': ['SYS_ADMIN', 'NET_ADMIN']}, True), ({'auth_api': True}, True),
    ({'hassio_api': True}, True), ({'apparmor': 'disable'}, True), ({'watchdog': True}, True),
    ({'ingress': None}, True), ({'slug': 'other'}, True),
])
def test_real_app_contract_failures_never_print_pairing_code_or_ingress_entry(changes, configured):
    config = yaml.safe_load(process(['git', '-C', str(ROOT), 'show', 'HEAD:runner/config.yaml']))
    with pytest.raises(AssertionError) as caught:
        real_app_contract(pairing_info(**changes), config, SLUG, configured=configured)
    message = str(caught.value)
    assert CODE not in message and 'hassio_ingress' not in message and 'f' * 64 not in message
