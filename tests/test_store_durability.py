"""Disk durability and responsiveness; local transport/HA replies are fixtures."""
import ast
import asyncio
import gc
import os
import selectors
import signal
import sqlite3
import subprocess
import sys
import threading
import weakref
from pathlib import Path

import httpx
import pytest
from agentic_controller import store_journal
from agentic_controller.app import create_app
from agentic_controller.durability import client_session
from agentic_controller.ha import HomeAssistant
from agentic_controller.store import Store
from agentic_controller.workers import Workers
from aiohttp import web


@pytest.fixture
def live(tmp_path):
    store = Store(tmp_path / 'controller.sqlite3')
    try:
        yield store
    finally:
        if store.failed:
            with pytest.raises(RuntimeError, match='持久化失敗'):
                store.close()
        else:
            store.close()


@pytest.fixture
def held_commit(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    batches = []

    def hold():
        batches.append(threading.get_native_id())
        entered.set()
        assert release.wait(10), 'test did not release disk commit'

    monkeypatch.setattr(store_journal, 'before_disk_commit', hold)
    try:
        yield entered, release, batches
    finally:
        release.set()


async def wait_entered(entered):
    assert await asyncio.to_thread(entered.wait, 3), 'writer did not reach commit'


def disk_connect(store):
    return sqlite3.connect(f'file:{store.path}?mode=ro', uri=True)


async def test_memory_live_disk_full_and_installed_update_probe(live):
    assert live.db.execute('PRAGMA database_list').fetchone()[2] == ''
    assert live.db.execute('PRAGMA temp_store').fetchone()[0] == 2
    with disk_connect(live) as disk:
        assert disk.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    # FULL is configured on the owned writer connection, rather than a fresh reader.
    assert live._journal.disk.execute('PRAGMA synchronous').fetchone()[0] == 2
    assert live.db.execute('PRAGMA user_version').fetchone()[0] == 3
    live.setting('update-probe')
    assert live.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    await live.durable()
    immediate = live.durable()
    with pytest.raises(StopIteration):
        immediate.send(None)


async def test_held_commit_keeps_reads_and_writes_immediate(live, held_commit):
    entered, release, batches = held_commit
    live.set_setting('first', {'value': 1})
    await wait_entered(entered)
    waiter = asyncio.create_task(live.durable())

    async def write(index):
        live.set_setting(f'write-{index}', index)
        assert live.setting(f'write-{index}') == index

    await asyncio.wait_for(asyncio.gather(*(write(i) for i in range(30))), 1)
    await asyncio.sleep(0)
    assert not waiter.done()
    with disk_connect(live) as disk:
        assert disk.execute("SELECT value FROM settings WHERE key='first'").fetchone() is None
    release.set()
    await asyncio.wait_for(waiter, 3)
    await live.durable()
    with disk_connect(live) as disk:
        assert disk.execute("SELECT count(*) FROM settings WHERE key LIKE 'write-%'").fetchone()[0] == 30
    assert 1 <= len(batches) <= 2


async def test_cancelled_durability_waiter_does_not_drop_later_writes(live, held_commit):
    entered, release, _ = held_commit
    live.set_setting('cancelled-waiter', True)
    await wait_entered(entered)
    waiter = asyncio.create_task(live.durable())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    live.set_setting('after-cancellation', True)
    release.set()
    await asyncio.wait_for(live.durable(), 3)
    assert live._journal.thread.is_alive()
    with disk_connect(live) as disk:
        assert disk.execute("SELECT value FROM settings WHERE key='after-cancellation'").fetchone()[0] == 'true'


@pytest.mark.parametrize('sql', [
    "INSERT INTO settings VALUES ('bypass','true')",
    "UPDATE settings SET value='false'",
    'DELETE FROM settings',
    'CREATE TABLE bypass(value TEXT)',
    'PRAGMA user_version=9',
    'PRAGMA synchronous=OFF',
])
def test_memory_only_writes_are_rejected(live, sql):
    with pytest.raises(sqlite3.DatabaseError):
        live.db.execute(sql)
    assert live.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_authorizer_checks_previously_allowed_statements(live):
    sql = 'INSERT OR REPLACE INTO settings VALUES (?,?)'
    with live.transaction() as tx:
        tx.execute(sql, ('allowed', 'true'))
    with pytest.raises(sqlite3.DatabaseError):
        live.db.execute(sql, ('bypass-cache', 'true'))
    assert live.setting('bypass-cache') is None


async def test_transaction_rollback_and_parameter_copy(live, held_commit):
    entered, release, _ = held_commit
    with pytest.raises(ValueError):
        with live.transaction() as tx:
            tx.execute('INSERT INTO settings VALUES (?,?)', ('rolled-back', 'true'))
            raise ValueError('abort')
    assert live.setting('rolled-back') is None
    live.set_setting('block', True)
    await wait_entered(entered)
    value = bytearray(b'original')
    class Adapted:
        def __init__(self):
            self.value = 'adapted-original'

        def __conform__(self, protocol):
            return self.value
    class AdaptedString(str):
        value = 'subclass-original'

        def __conform__(self, protocol):
            return self.value
    adapted = Adapted()
    subclass = AdaptedString('raw')
    with live.transaction() as tx:
        tx.execute('INSERT INTO settings VALUES (?,?)', ('immutable', value))
        tx.execute('INSERT INTO settings VALUES (?,?)', ('adapted', adapted))
        tx.execute('INSERT INTO settings VALUES (?,?)', ('subclass', subclass))
    value[:] = b'modified'
    adapted.value = 'adapted-modified'
    subclass.value = 'subclass-modified'
    tx.statements.append(("UPDATE settings SET value=? WHERE key='immutable'", ('bypass',)))
    release.set()
    await live.durable()
    with disk_connect(live) as disk:
        assert disk.execute("SELECT value FROM settings WHERE key='immutable'").fetchone()[0] == b'original'
        assert disk.execute("SELECT value FROM settings WHERE key='adapted'").fetchone()[0] == 'adapted-original'
        assert disk.execute("SELECT value FROM settings WHERE key='subclass'").fetchone()[0] == 'subclass-original'


@pytest.fixture
async def real_app(live, ha, tmp_path, monkeypatch):
    monkeypatch.setenv('AGENTIC_DATA', str(tmp_path))
    monkeypatch.setenv('AGENTIC_LINE_CONFIG', str(tmp_path / 'line-config/gateway.json'))
    (tmp_path / 'access-key').write_text('durability-fixture-key')
    app = create_app(store=live, ha=ha, host_mode=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1', 1)),
                                 base_url='http://localhost:8099',
                                 headers={'Origin': 'http://localhost:8099'}) as client:
        assert (await client.post('/api/host/login', json={'key': 'durability-fixture-key'})).status_code == 200
        client.headers['X-Agentic-CSRF'] = (await client.get('/api/session')).json()['csrf']
        await live.durable()
        yield app, client
    await app.state.gateway.close()
    tasks = list(app.state.running.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_issue_68_health_status_and_post(real_app, live, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def hold():
        entered.set()
        assert release.wait(10)

    monkeypatch.setattr(store_journal, 'before_disk_commit', hold)
    _, client = real_app
    try:
        live.set_setting('held', True)
        await wait_entered(entered)
        assert (await asyncio.wait_for(client.get('/health'), 1)).status_code == 200
        assert (await asyncio.wait_for(client.get('/api/status'), 1)).status_code == 200
        post = asyncio.create_task(client.post('/api/pause', json={'paused': True}))
        await asyncio.sleep(.05)
        assert not post.done()
        release.set()
        assert (await asyncio.wait_for(post, 3)).status_code == 200
        assert live.setting('paused') is True
    finally:
        release.set()


@pytest.fixture
async def local_ha(live):
    """Exercise the real HA transport against a loopback protocol fixture."""
    received = []

    async def config(request):
        received.append(('http', request.method, request.path))
        return web.json_response({'version': 'local-fixture', 'location_name': '測試'})

    async def websocket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({'type': 'auth_required'})
        auth = await ws.receive_json()
        assert auth['type'] == 'auth'
        received.append(('ws', 'auth'))
        await ws.send_json({'type': 'auth_ok'})
        async for message in ws:
            if message.type == web.WSMsgType.TEXT:
                command = message.json()
                received.append(('ws', command['type']))
                await ws.send_json({'id': command['id'], 'type': 'result',
                                    'success': True, 'result': {'fixture': True}})
        return ws

    app = web.Application()
    async def host_status(request):
        received.append(('http', request.method, request.path))
        return web.json_response({'controller_update': {'ingress_bridge_contract': 1}})

    app.router.add_get('/api/config', config)
    app.router.add_get('/api/host-status', host_status)
    app.router.add_get('/api/websocket', websocket)
    server = web.AppRunner(app)
    await server.setup()
    site = web.TCPSite(server, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    ha = HomeAssistant('local-fixture-token', base=f'http://127.0.0.1:{port}/api',
                       websocket=f'ws://127.0.0.1:{port}/api/websocket')
    ha.store = live
    await ha.start()
    try:
        yield ha, received
    finally:
        await ha.close()
        await server.cleanup()


async def test_real_ha_status_get_bypasses_held_commit(live, local_ha, tmp_path, monkeypatch):
    ha, received = local_ha
    monkeypatch.setenv('AGENTIC_DATA', str(tmp_path))
    monkeypatch.setenv('AGENTIC_LINE_CONFIG', str(tmp_path / 'line-config/gateway.json'))
    (tmp_path / 'access-key').write_text('local-status-fixture-key')
    app = create_app(store=live, ha=ha, host_mode=True)
    entered, release = threading.Event(), threading.Event()

    def hold():
        entered.set()
        assert release.wait(10)

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1', 1)),
                                     base_url='http://localhost:8099',
                                     headers={'Origin': 'http://localhost:8099'}) as client:
            assert (await client.post('/api/host/login', json={'key': 'local-status-fixture-key'})).status_code == 200
            client.headers['X-Agentic-CSRF'] = (await client.get('/api/session')).json()['csrf']
            await live.durable()
            monkeypatch.setattr(store_journal, 'before_disk_commit', hold)
            live.set_setting('status-pending', True)
            await wait_entered(entered)
            assert (await asyncio.wait_for(client.get('/health'), 1)).status_code == 200
            response = await asyncio.wait_for(client.get('/api/status'), 1)
            assert response.status_code == 200
            assert response.json()['ha']['version'] == 'local-fixture'
            assert ('http', 'GET', '/api/config') in received
            waiter = asyncio.create_task(live.durable())
            await asyncio.sleep(0)
            assert not waiter.done()
            release.set()
            await asyncio.wait_for(waiter, 3)
    finally:
        release.set()
        await app.state.gateway.close()
        tasks = list(app.state.running.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize('command,kwargs,waits', [
    ('get_states', {}, False),
    ('config_entries/flow/progress', {}, False),
    ('supervisor/api', {'endpoint': '/addons', 'method': 'get'}, False),
    ('supervisor/api', {'endpoint': '/addons', 'method': 'HEAD'}, False),
    ('supervisor/api', {'endpoint': '/addons', 'method': 'post'}, True),
    ('supervisor/api', {'endpoint': '/addons'}, True),
    ('call_service', {'domain': 'light', 'service': 'turn_on'}, True),
    ('future_unknown_fixture_command', {}, True),
])
async def test_real_websocket_read_allowlist_and_mutation_barriers(live, local_ha, held_commit,
                                                                 command, kwargs, waits):
    ha, received = local_ha
    entered, release, _ = held_commit
    live.set_setting('ws-before-command', True)
    await wait_entered(entered)
    task = asyncio.create_task(ha.ws(command, **kwargs))
    try:
        if waits:
            await asyncio.sleep(.05)
            assert not task.done()
            assert ('ws', command) not in received
            release.set()
        result = await asyncio.wait_for(task, 3 if waits else 1)
        assert result == {'fixture': True}
        assert ('ws', 'auth') in received
        assert ('ws', command) in received
        if not waits:
            waiter = asyncio.create_task(live.durable())
            await asyncio.sleep(0)
            assert not waiter.done()
            release.set()
            await asyncio.wait_for(waiter, 3)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('pending', [False, True])
async def test_worker_auth_read_only_or_persisted_receipt(live, held_commit, pending):
    class Runner:
        async def call(self, method, path, body=None):
            assert method == 'GET'
            return {'id': 'a' * 32, 'status': 'configured'}

    workers = Workers(live, {'host': Runner()}, None)
    entered, release, _ = held_commit
    if pending:
        live.set_setting('worker_auth_pending', {'worker_id': 'host-coder', 'id': 'a' * 32})
    live.set_setting('unrelated-background-write', True)
    await wait_entered(entered)
    task = asyncio.create_task(workers.role_auth('host-coder', 'GET', '/coding/auth/' + 'a' * 32))
    try:
        if pending:
            await asyncio.sleep(.05)
            assert live.setting('worker_auth_pending') is None
            assert not task.done()
            release.set()
        result = await asyncio.wait_for(task, 3 if pending else 1)
        assert result['status'] == 'configured'
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_outgoing_request_does_not_reach_server_before_durable(live, held_commit):
    entered, release, _ = held_commit
    received = asyncio.Event()

    async def handle(request):
        received.set()
        return web.Response(text='fixture')

    app = web.Application()
    app.router.add_post('/', handle)
    server = web.AppRunner(app)
    await server.setup()
    site = web.TCPSite(server, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        live.set_setting('before-request', True)
        await wait_entered(entered)
        async with client_session(live) as session:
            request = asyncio.create_task(session.post(f'http://127.0.0.1:{port}/'))
            await asyncio.sleep(.05)
            assert not received.is_set() and not request.done()
            release.set()
            response = await asyncio.wait_for(request, 3)
            assert await response.text() == 'fixture'
            assert received.is_set()
            response.release()
    finally:
        release.set()
        await server.cleanup()


@pytest.mark.parametrize('exit_mode', ['normal', 'durable-hard-exit'])
def test_process_exit_persists(tmp_path, exit_mode):
    path = tmp_path / 'child.sqlite3'
    script = """
import asyncio, os, sys
from pathlib import Path
from agentic_controller.store import Store
from agentic_controller.workers import Workers
from aiohttp import web
s = Store(Path(sys.argv[1]))
s.set_setting('child', {'persisted': True})
if sys.argv[2] == 'durable-hard-exit':
    asyncio.run(s.durable())
    os._exit(0)
"""
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join(sys.path)
    result = subprocess.run([sys.executable, '-c', script, str(path), exit_mode],
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(path) as disk:
        assert disk.execute("SELECT value FROM settings WHERE key='child'").fetchone()[0] == '{"persisted":true}'
        assert disk.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


async def test_fail_stop_preserves_memory_and_health_reports_503(real_app, live, monkeypatch):
    def fail():
        raise OSError('fixture disk failure')

    monkeypatch.setattr(store_journal, 'before_disk_commit', fail)
    live.set_setting('last-memory-write', True)
    with pytest.raises(RuntimeError, match='持久化失敗'):
        await live.durable()
    before = list(live.db.iterdump())
    with pytest.raises(RuntimeError, match='持久化失敗'):
        live.set_setting('must-not-write', True)
    assert list(live.db.iterdump()) == before
    with pytest.raises(RuntimeError, match='持久化失敗'):
        await live.durable()
    _, client = real_app
    assert (await client.get('/health')).status_code == 503
    before_pause = live.setting('paused', False)
    response = await client.post('/api/pause', json={'paused': not before_pause})
    assert response.status_code == 503
    assert '資料' in response.json()['detail']
    assert live.setting('paused', False) == before_pause
    assert (await client.get('/api/status')).status_code == 200


def test_outgoing_session_construction_is_centralized():
    root = Path(__file__).resolve().parents[1] / 'controller/agentic_controller'
    sites = []
    for path in root.glob('*.py'):
        tree = ast.parse(path.read_text())
        aliases = {'aiohttp'}
        direct = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                aliases.update(alias.asname or alias.name for alias in node.names if alias.name == 'aiohttp')
            elif isinstance(node, ast.ImportFrom) and node.module == 'aiohttp':
                direct.update(alias.asname or alias.name for alias in node.names if alias.name == 'ClientSession')
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if (isinstance(function, ast.Attribute) and function.attr == 'ClientSession'
                    and isinstance(function.value, ast.Name) and function.value.id in aliases
                    or isinstance(function, ast.Name) and function.id in direct):
                sites.append((path.name, node.lineno))
    assert len(sites) == 1 and sites[0][0] == 'durability.py', sites


def test_dropped_stores_drain_and_leave_no_threads(tmp_path):
    gc.collect()
    baseline = {thread.ident for thread in threading.enumerate() if thread.name == 'controller-store-writer'}
    refs = []
    for index in range(20):
        store = Store(tmp_path / f'gc-{index}.sqlite3')
        store.set_setting('index', index)
        refs.append(weakref.ref(store))
        del store
    gc.collect()
    assert all(ref() is None for ref in refs)
    assert {thread.ident for thread in threading.enumerate() if thread.name == 'controller-store-writer'} == baseline
    for index in range(20):
        with sqlite3.connect(tmp_path / f'gc-{index}.sqlite3') as disk:
            assert disk.execute("SELECT value FROM settings WHERE key='index'").fetchone()[0] == str(index)


async def test_mixed_workload_and_snapshot_match_disk(live, tmp_path):
    for index in range(20):
        task = {'id': f'task-{index}', 'state': 'planning'}
        live.put('task', task)
        live.transition(task, 'ready', fixture=True)
        live.event(task['id'], 'fixture', {'index': index})
        live.request('fixture', str(index), f'digest-{index}', task['id'])
    # The worker batch shape includes atomic idempotency insertion and a lease update.
    with live.transaction() as tx:
        tx.execute('INSERT INTO records VALUES (?,?,?,?,?)',
                   ('worker_job', 'job', '{"id":"job"}', '2026', '2026'))
        tx.execute('INSERT INTO requests VALUES (?,?,?,?)', ('worker', 'job', 'digest', 'job'))
        tx.executemany('INSERT OR REPLACE INTO settings VALUES (?,?)',
                       [('worker_lease', '{"id":"job"}'), ('worker_busy', 'true')])
    workers = Workers(live, {}, None)
    live.put('work_job', {'id': 'accepted', 'job_id': 'accepted', 'case_id': 'fixture',
                         'worker_id': 'host-coder', 'lease_epoch': 1, 'state': 'running',
                         'content_expired': True, 'created_epoch': 0})
    live.set_setting('worker_lease', {'job_id': 'accepted', 'epoch': 1})
    workers.accept('accepted', {'id': 'accepted', 'lease_epoch': 1, 'state': 'succeeded',
                                'stop_confirmed': True})
    assert live.setting('worker_lease') is None
    live.prune()
    snapshot = tmp_path / 'snapshot.sqlite3'
    await live.snapshot(snapshot)
    await live.durable()
    memory = list(live.db.iterdump())
    with disk_connect(live) as disk:
        assert list(disk.iterdump()) == memory
    with sqlite3.connect(snapshot) as disk:
        assert list(disk.iterdump()) == memory


async def test_host_proof_refresh_keeps_running_during_long_commit(real_app, live, local_ha, monkeypatch):
    app, _ = real_app
    ha, received = local_ha
    entered, release = threading.Event(), threading.Event()
    ticks = []

    def hold():
        entered.set()
        assert release.wait(20)

    async def host_status():
        async with client_session(live) as session:
            async with session.get(ha.base + '/host-status') as response:
                return await response.json()

    async def ticker():
        while True:
            ticks.append(asyncio.get_running_loop().time())
            await asyncio.sleep(.025)

    async def concurrent_writes():
        for index in range(100):
            live.set_setting('host-proof-write', index)
            assert live.setting('host-proof-write') == index
            await asyncio.sleep(.05)

    bridge = app.state.ingress_bridge
    bridge.host_status = host_status
    monkeypatch.setattr(store_journal, 'before_disk_commit', hold)
    live.set_setting('host-proof-held', True)
    await wait_entered(entered)
    ticking = asyncio.create_task(ticker())
    writing = asyncio.create_task(concurrent_writes())
    pending = asyncio.create_task(live.durable())
    try:
        await asyncio.wait_for(bridge.require_host(force=True), 1)
        assert bridge.host_current()
        # Exceed the fixed five-second Host proof timeout while the disk stays held.
        await asyncio.sleep(2.6)
        assert not pending.done()
        await asyncio.wait_for(bridge.require_host(force=True), 1)
        assert bridge.host_current()
        await asyncio.sleep(2.6)
        assert not pending.done()
        await asyncio.wait_for(writing, 1)
        await asyncio.wait_for(bridge.require_host(force=True), 1)
        assert bridge.host_current()
        assert received.count(('http', 'GET', '/api/host-status')) == 3
        assert len(ticks) >= 100
        assert max(right - left for left, right in zip(ticks[:-1], ticks[1:], strict=True)) < .5
        release.set()
        await asyncio.wait_for(pending, 3)
        await live.durable()
    finally:
        release.set()
        for task in (ticking, writing, pending):
            if not task.done():
                task.cancel()
        await asyncio.gather(ticking, writing, pending, return_exceptions=True)


@pytest.mark.parametrize('method', ['execute', 'executemany', 'executescript', 'commit', 'rollback',
                                     'tx-commit', 'tx-rollback'])
async def test_direct_memory_write_is_denied_even_inside_transaction(live, method):
    before = live._journal.seq
    with pytest.raises(sqlite3.DatabaseError):
        with live.transaction() as tx:
            tx.execute('INSERT INTO settings VALUES (?,?)', ('must-rollback', 'true'))
            if method == 'execute':
                live.db.execute("INSERT INTO settings VALUES ('bypass','true')")
            elif method == 'executemany':
                live.db.executemany('INSERT INTO settings VALUES (?,?)', [('bypass', 'true')])
            elif method == 'executescript':
                live.db.executescript("INSERT INTO settings VALUES ('bypass','true');")
            elif method in {'commit', 'rollback'}:
                getattr(live.db, method)()
            else:
                tx.execute('COMMIT' if method == 'tx-commit' else 'ROLLBACK')
    assert live.setting('must-rollback') is None
    assert live.setting('bypass') is None
    assert live._journal.seq == before
    await live.durable()


@pytest.mark.parametrize('container', ['mapping', 'string', 'generator'])
def test_transaction_rejects_non_sequence_parameter_containers(live, container):
    params = {'key': 'unexpected', 'value': 'true'} if container == 'mapping' else (
        'xy' if container == 'string' else (value for value in ['unexpected', 'true']))
    before = list(live.db.iterdump())
    with pytest.raises(TypeError):
        with live.transaction() as tx:
            tx.execute('INSERT INTO settings VALUES (?,?)', params)
    assert list(live.db.iterdump()) == before


def test_executemany_rolls_back_when_a_parameter_container_is_invalid(live):
    with pytest.raises(TypeError):
        with live.transaction() as tx:
            tx.executemany('INSERT INTO settings VALUES (?,?)',
                           [('must-rollback', 'true'), {'key': 'unexpected', 'value': 'true'}])
    assert live.setting('must-rollback') is None


def test_store_transactions_have_no_coroutine_suspension():
    root = Path(__file__).resolve().parents[1] / 'controller/agentic_controller'
    violations = []
    for path in root.rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, (ast.With, ast.AsyncWith)):
                continue
            transactions = [item for item in node.items if isinstance(item.context_expr, ast.Call)
                            and isinstance(item.context_expr.func, ast.Attribute)
                            and item.context_expr.func.attr == 'transaction']
            if transactions:
                if isinstance(node, ast.AsyncWith):
                    violations.append((str(path.relative_to(root)), node.lineno))
                for statement in node.body:
                    for nested in ast.walk(statement):
                        if isinstance(nested, (ast.Await, ast.AsyncFor, ast.AsyncWith, ast.Yield, ast.YieldFrom)):
                            violations.append((str(path.relative_to(root)), nested.lineno))
    assert not violations, violations


async def test_unexpected_writer_publish_failure_fails_waiters_and_future_writes(live, held_commit,
                                                                              tmp_path, monkeypatch):
    entered, release, _ = held_commit

    def fail_publish(seq):
        raise RuntimeError('fixture unexpected writer death')

    monkeypatch.setattr(live._journal, 'publish', fail_publish)
    live.set_setting('before-publish-failure', True)
    await wait_entered(entered)
    pending = asyncio.create_task(live.durable())
    snapshot = asyncio.create_task(live.snapshot(tmp_path / 'must-not-hang.sqlite3'))
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(pending, 3)
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(snapshot, 3)
    assert live.failed
    before = list(live.db.iterdump())
    with pytest.raises(RuntimeError):
        live.set_setting('after-writer-death', True)
    assert list(live.db.iterdump()) == before
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(live.durable(), 1)
    await asyncio.to_thread(live._journal.thread.join, 3)
    assert not live._journal.thread.is_alive()


def test_outgoing_session_helpers_require_an_explicit_store():
    root = Path(__file__).resolve().parents[1] / 'controller/agentic_controller'
    violations = []
    sites = []
    for path in root.rglob('*.py'):
        tree = ast.parse(path.read_text())
        names = {name: name for name in ('client_session', 'host_call', 'broker_call')}
        partials = {'partial'}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in names:
                        names[alias.asname or alias.name] = alias.name
                    if node.module == 'functools' and alias.name == 'partial':
                        partials.add(alias.asname or alias.name)
        checked = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function, args = node.func, node.args
            if ((isinstance(function, ast.Name) and function.id in partials
                 or isinstance(function, ast.Attribute) and function.attr == 'partial') and args):
                function, args = args[0], args[1:]
            helper = (names.get(function.id) if isinstance(function, ast.Name) else
                      function.attr if isinstance(function, ast.Attribute) else None)
            if helper not in {'client_session', 'host_call', 'broker_call'}:
                continue
            checked.add(id(function))
            sites.append((path.name, node.lineno, helper))
            store = (args[0] if helper == 'client_session' and args else next(
                (keyword.value for keyword in node.keywords if keyword.arg == 'store'), None))
            if store is None or isinstance(store, ast.Constant) and store.value is None:
                violations.append((path.name, node.lineno, helper))
        # A bare reference becomes a call elsewhere (`self.host_request = ... or host.host_call`)
        # that no call-site check sees; bind the store with partial(..., store=...) instead.
        for node in ast.walk(tree):
            if id(node) in checked:
                continue
            if (isinstance(node, ast.Name) and node.id in names or
                    isinstance(node, ast.Attribute) and node.attr in {'client_session', 'host_call', 'broker_call'}):
                violations.append((path.name, node.lineno, 'reference'))
    assert {site[2] for site in sites} == {'client_session', 'host_call', 'broker_call'}
    assert not violations, violations


def test_session_helper_refuses_missing_or_none_store():
    with pytest.raises(TypeError):
        client_session()
    with pytest.raises(ValueError):
        client_session(None)


def test_uvicorn_sigterm_drains_unawaited_store_write(tmp_path):
    path = tmp_path / 'sigterm.sqlite3'
    script = """
import asyncio, os, socket, sys, threading, time
from pathlib import Path
import uvicorn
from agentic_controller import store_journal
from agentic_controller.app import create_app
from agentic_controller.store import Store

class FixtureHA:
    connected = False
    last_observed = None
    async def start(self):
        pass
    async def close(self):
        pass
    async def watch(self):
        while True:
            await asyncio.sleep(600)

async def main():
    path = Path(sys.argv[1])
    os.environ['AGENTIC_DATA'] = str(path.parent)
    store = Store(path)
    app = create_app(store=store, ha=FixtureHA(), host_mode=False)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=0,
        access_log=False, log_level='warning', timeout_graceful_shutdown=45))
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    entered = threading.Event()
    def hold():
        entered.set()
        time.sleep(2)
    async def write_when_started():
        while not server.started:
            await asyncio.sleep(.01)
        await store.durable()
        store_journal.before_disk_commit = hold
        store.set_setting('sigterm-unawaited', {'persisted': True})
        assert await asyncio.to_thread(entered.wait, 10)
        print('READY', flush=True)
    writing = asyncio.create_task(write_when_started())
    try:
        await server.serve(sockets=[sock])
        await writing
    finally:
        sock.close()

asyncio.run(main())
"""
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join(sys.path)
    process = subprocess.Popen([sys.executable, '-c', script, str(path)], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            assert ready.select(45), 'uvicorn child did not start'
            assert process.stdout.readline().strip() == 'READY'
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=45)
        assert process.returncode in {0, -signal.SIGTERM}, stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
    with sqlite3.connect(path) as disk:
        assert disk.execute("SELECT value FROM settings WHERE key='sigterm-unawaited'").fetchone()[0] == '{"persisted":true}'
        assert disk.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
