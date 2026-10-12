"""Compiled UI with protocol fixtures; never claims real cloud/HAOS acceptance."""
import base64
import hashlib
import json
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).parents[1]


@pytest.fixture
def ui_page():
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(SimpleHTTPRequestHandler, directory=str(ROOT / 'controller/static')))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 900})
            yield page, f'http://127.0.0.1:{server.server_port}'
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def common(path):
    return {'deployment': {'mode': 'host', 'authenticated': True},
            'session': {'principal': 'host-owner', 'csrf': 'fixture-csrf'},
            'status': {'ha': {'status': 'connected', 'location_name': '家庭測試環境'}, 'runner': {'protocol': 1, 'providers': []}, 'version': 'fixture', 'paused': False},
            'tasks': {'tasks': []},
            'host/status': {'vm': 'running', 'profile': {'label': '家庭筆電', 'memory_mib': 4096, 'vcpus': 2}, 'storage_free_bytes': 80 * 1024**3, 'jobs': [], 'snapshots': [], 'provisioned': True}}.get(path)


def screenshot(page, name):
    for width in (1440, 768, 390):
        page.set_viewport_size({'width': width, 'height': 900 if width == 1440 else 844})
        page.evaluate('window.scrollTo(0,0)')
        page.wait_for_timeout(100)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(ROOT / f'evidence/ui/{name}-{width}.png'), full_page=True)


def test_coding_role_login_lost_reply_stop_fence_and_artifact_preview(ui_page):
    page, origin = ui_page
    provider = {'id': 'codex', 'supported': True, 'models': ['gpt-6-astra'], 'auth_methods': ['device_code', 'api_key'], 'credential_state': 'needs_auth'}
    workers = [{'worker_id': 'host-coder', 'available': True, 'state': 'ready', 'reason': '', 'runtime_location': 'host_worker_container', 'modes': ['research', 'development'], 'providers': [provider]},
               {'worker_id': 'home-coder', 'available': False, 'state': 'blocked', 'reason': 'haos_coding_boundary_unavailable', 'runtime_location': 'haos_app', 'modes': [], 'providers': []}]
    auth = {'id': 'a' * 32, 'status': 'pending', 'url': 'https://auth.openai.com/codex/device', 'code': 'SYNTHETIC-CODE'}
    result = {'jobs': [], 'active_job_id': None}
    calls, errors, urls = [], [], []
    contents = {'b' * 32: '--- a/button.css\n+++ b/button.css\n@@ -1 +1 @@\n-height: 32px\n+min-height: 44px\n',
                'c' * 32: '<html><body><script>window.artifactExecuted=true</script><h1>家庭設定</h1></body></html>'}
    artifacts = [{'id': id_, 'name': 'candidate.patch' if id_.startswith('b') else 'index.html', 'kind': 'diff' if id_.startswith('b') else 'preview', 'media_type': 'text/plain', 'sha256': hashlib.sha256(body.encode()).hexdigest(), 'size': len(body.encode())} for id_, body in contents.items()]

    def route(route):
        req = route.request
        path = req.url.split('/api/')[1]
        urls.append(req.url)
        body = req.post_data_json if req.method == 'POST' else None
        if body is not None:
            assert req.headers['x-agentic-csrf'] == 'fixture-csrf'
            assert req.headers['idempotency-key']
            calls.append((path, body, req.headers['idempotency-key']))
        value = common(path)
        if value is not None:
            pass
        elif path == 'workers':
            value = {'workers': workers, 'active_job_id': result['active_job_id'], 'concurrency_limit': 1, 'work_contract_major': 1}
        elif path == 'worker/jobs' and body is None:
            value = result
        elif path == 'workers/host-coder/providers/codex/auth':
            if body['method'] == 'api_key':
                assert body['secret'] == 'synthetic-private-coding-key'
                provider['credential_state'] = 'configured'
                auth.update(status='connected', url=None, code=None)
            else:
                assert body == {'method': 'device_code', 'secret': ''}
            value = auth
        elif path == f"workers/host-coder/auth/{auth['id']}":
            value = auth
        elif path.endswith('/auth/' + auth['id'] + '/cancel'):
            auth['status'] = 'cancelled'
            value = auth
        elif path == 'worker/jobs':
            assert body['repo_id'] == 'agentic-home' and body['worker_id'] == 'host-coder'
            identifier = str(len(result['jobs']) + 1) * 32
            job = {'id': identifier, 'case_id': 'e' * 32, 'worker_id': 'host-coder', 'mode': body['mode'], 'provider': 'codex', 'goal': body['goal'], 'state': 'running', 'execution_started': True, 'stop_confirmed': False, 'created_at': '2026-10-12T09:00:00Z', 'runtime_location': 'host_worker_container', 'base_commit': 'f' * 40, 'facts': [], 'artifacts': []}
            result['jobs'].insert(0, job)
            result['active_job_id'] = identifier
            if len(result['jobs']) == 1:
                route.abort('failed')  # Accepted remotely; UI must recover with GET.
                return
            job.update(state='succeeded', stop_confirmed=True, facts=['已重現手機按鈕過小，修正後觸控區域為 44px。'], experiments=['獨立測試程序：12 項通過。'], artifacts=artifacts)
            result['active_job_id'] = None
            value = job
        elif path.endswith('/cancel'):
            result['jobs'][0].update(state='stop_unconfirmed', reason='等待確認原工作已停止。')
            value = result['jobs'][0]
        elif path.endswith('/reconcile'):
            result['jobs'][0].update(state='cancelled', stop_confirmed=True, reason='')
            result['active_job_id'] = None
            value = result['jobs'][0]
        elif '/artifacts/' in path:
            identifier = path.split('/')[-1]
            value = {**next(item for item in artifacts if item['id'] == identifier), 'content_base64': base64.b64encode(contents[identifier].encode()).decode()}
        elif path.endswith('/previews'):
            assert body == {'artifact_ids': ['c' * 32]}
            value = {'id': 'd' * 32, 'state': 'ready', 'preview_urls': {str(width): f"http://127.0.0.2:8100/p/{'x' * 43}/{width}.png" for width in (1440, 768, 390)}}
        else:
            raise AssertionError(path)
        route.fulfill(status=200, content_type='application/json', body=json.dumps(value))

    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/api/**', route)
    page.goto(origin + '/#workers')
    page.get_by_role('heading', name='要調查或改善什麼？').wait_for()
    page.get_by_label('開發需求', exact=True).fill('調查手機上的按鈕')
    expect(page.get_by_role('button', name='開始調查', exact=True)).to_be_disabled()
    page.get_by_role('button', name='登入 Codex 開發助手').click()
    page.get_by_text('SYNTHETIC-CODE', exact=True).wait_for()
    page.reload()
    page.get_by_text('SYNTHETIC-CODE', exact=True).wait_for()
    assert 'SYNTHETIC-CODE' not in page.evaluate('JSON.stringify({...sessionStorage})')
    auth['url'] = 'https://unexpected.example/collect'
    expect(page.get_by_role('link', name='開啟開發助手授權頁')).to_have_count(0)
    page.get_by_role('button', name='取消開發助手登入').click()
    page.get_by_label('開發助手登入方式').select_option('api_key')
    page.get_by_label('開發助手 API key').fill('synthetic-private-coding-key')
    page.get_by_role('button', name='登入 Codex 開發助手').click()
    page.get_by_role('heading', name='開發助手已登入').wait_for()
    assert 'synthetic-private-coding-key' not in page.evaluate('JSON.stringify({s:{...sessionStorage},l:{...localStorage}})')
    assert all('synthetic-private-coding-key' not in url for url in urls)
    page.get_by_label('開發需求', exact=True).fill('調查手機上的按鈕')
    page.get_by_role('button', name='開始調查', exact=True).click()
    page.get_by_role('button', name='停止這份工作', exact=True).wait_for()
    page.reload()
    page.get_by_role('button', name='停止這份工作', exact=True).click()
    page.get_by_role('button', name='核對停止與工作狀態').wait_for()
    page.get_by_label('開發需求', exact=True).fill('修正按鈕大小並測試')
    expect(page.get_by_role('button', name='開始調查', exact=True)).to_be_disabled()
    assert len([item for item in calls if item[0] == 'worker/jobs']) == 1
    page.get_by_role('button', name='核對停止與工作狀態').click()
    page.get_by_label('工作方式').select_option('development')
    page.get_by_role('button', name='開始修正與測試', exact=True).click()
    page.get_by_text('獨立測試程序：12 項通過。', exact=True).wait_for()
    page.locator('.worker-artifact').filter(has_text='candidate.patch').get_by_role('button', name='查看內容').click()
    expect(page.locator('.worker-artifact pre').first).to_contain_text('+min-height: 44px')
    html = page.locator('.worker-artifact').filter(has_text='index.html')
    html.get_by_role('button', name='查看內容').click()
    expect(html.locator('pre')).to_contain_text('window.artifactExecuted')
    assert page.evaluate('window.artifactExecuted === undefined')
    html.get_by_role('button', name='收起內容').click()
    page.get_by_role('button', name='產生畫面預覽', exact=True).click()
    expect(page.get_by_role('link', name='手機預覽')).to_have_attribute('href', f"http://127.0.0.2:8100/p/{'x' * 43}/390.png")
    screenshot(page, 'coding-results')
    page.get_by_role('button', name='新工作', exact=True).click()
    page.get_by_label('執行位置').select_option('home-coder')
    page.get_by_label('開發需求', exact=True).fill('調查家庭現況')
    expect(page.get_by_role('button', name='開始修正與測試', exact=True)).to_be_disabled()
    expect(page.get_by_text('執行環境尚未就緒', exact=False)).to_be_visible()
    assert not errors


def test_network_and_digest_bound_update_review(ui_page):
    page, origin = ui_page
    network = {'lan': 'dhcp', 'remote': 'disabled', 'managed': False, 'tailscale': {'state': 'NeedsLogin'}}
    releases = {'trusted_publisher_configured': False, 'records': []}
    calls, errors = [], []
    manifest = {'version': '0.6.1', 'source_commit': 'b' * 40, 'tests': [{'name': 'browser-and-regression', 'result': 'passed'}]}
    release = {'candidate_digest': 'a' * 64, 'manifest': manifest, 'state': 'staged', 'approval_id': 'apply-fixture', 'approval_expires_at': time.time() + 600}

    def route(route):
        request = route.request
        path = request.url.split('/api/')[1]
        body = request.post_data_json if request.method == 'POST' else None
        if body is not None:
            assert request.headers['x-agentic-csrf'] == 'fixture-csrf'
            calls.append((path, body))
        value = common(path)
        if value is not None:
            pass
        elif path == 'network':
            value = network
        elif path == 'network/preferences':
            network.update(body)
            value = network
        elif path == 'network/login':
            network.update(managed=True, tailscale={'state': 'NeedsLogin', 'login_url': 'https://login.tailscale.com/a/synthetic-login'})
            value = network
        elif path == 'network/revoke':
            network.update(remote='disabled', managed=False, tailscale={'state': 'NeedsLogin'})
            value = network
        elif path == 'updates':
            value = releases
        elif path == 'updates/stage':
            assert body == {'manifest': manifest, 'signature': 'synthetic-signature'}
            releases['records'] = [release]
            value = release
        elif path == 'updates/apply':
            assert body == {'candidate_digest': 'a' * 64, 'approval_id': 'apply-fixture'}
            release['state'] = 'deployed'
            value = release
        elif path == 'updates/rollback-offer':
            assert body == {'candidate_digest': 'a' * 64}
            release.update(rollback_approval_id='rollback-fixture', rollback_expires_at=time.time() + 600)
            value = release
        elif path == 'updates/rollback':
            assert body == {'candidate_digest': 'a' * 64, 'approval_id': 'rollback-fixture'}
            release['state'] = 'rolled_back'
            value = release
        else:
            raise AssertionError(path)
        route.fulfill(status=200, content_type='application/json', body=json.dumps(value))

    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/api/**', route)
    page.goto(origin + '/#host')
    page.get_by_text('網路與遠端連線', exact=True).click()
    page.get_by_label('家中網路').select_option('reservation')
    page.get_by_label('外出連線').select_option('tailscale')
    page.get_by_role('button', name='保存連線偏好').click()
    page.get_by_role('button', name='開始 Tailscale 登入').click()
    expect(page.get_by_role('link', name='開啟 Tailscale 官方登入')).to_have_attribute('href', 'https://login.tailscale.com/a/synthetic-login')
    network['tailscale']['login_url'] = 'https://unexpected.example/steal'
    expect(page.get_by_role('link', name='開啟 Tailscale 官方登入')).to_have_count(0)
    network.update(tailscale={'state': 'Running', 'identity': 'synthetic-user@example.invalid'}, tunnel_command='ssh -N -L 127.0.0.1:8099:127.0.0.1:8099 owner@100.64.0.9')
    page.get_by_role('button', name='複製遠端連線指令').wait_for()
    page.context.grant_permissions(['clipboard-read', 'clipboard-write'])
    page.get_by_role('button', name='複製遠端連線指令').click()
    assert page.evaluate('navigator.clipboard.readText()') == network['tunnel_command']
    page.get_by_text('版本更新與退版', exact=True).click()
    page.get_by_text('尚未設定可信的更新發行者。', exact=False).wait_for()
    expect(page.get_by_role('button', name='檢查候選版本', exact=True)).to_have_count(0)
    releases['trusted_publisher_configured'] = True
    page.get_by_text('或貼上更新說明', exact=True).click()
    page.get_by_label('簽署的更新說明檔', exact=True).fill(json.dumps({'manifest': manifest, 'signature': 'synthetic-signature'}))
    page.get_by_role('button', name='檢查候選版本', exact=True).click()
    page.get_by_role('button', name='檢查並確認更新', exact=True).click()
    assert not [item for item in calls if item[0] == 'updates/apply']
    screenshot(page, 'host-maintenance')
    page.get_by_role('button', name='確認更新這個版本', exact=True).click()
    page.get_by_role('button', name='選擇退回前一版本').click()
    assert not [item for item in calls if item[0] == 'updates/rollback']
    page.get_by_role('button', name='確認退版', exact=True).click()
    expect(page.get_by_role('region', name='更新候選版本').get_by_text('已退版', exact=True)).to_be_visible()
    page.get_by_role('button', name='登出此主機的 Tailscale').click()
    expect(page.get_by_label('外出連線')).to_have_value('disabled')
    assert not errors


def test_same_case_results_open_inline_without_dispatch(ui_page):
    page, origin = ui_page
    case_id, job_id = 'e' * 32, 'd' * 32
    posts = []
    job = {'id': job_id, 'case_id': case_id, 'worker_id': 'host-coder', 'mode': 'development', 'provider': 'codex', 'goal': '改善手機設定流程', 'state': 'succeeded', 'execution_started': True, 'stop_confirmed': True, 'facts': ['按鈕已調整，差異與測試證據已保存。'], 'artifacts': []}
    message = {'id': 'a' * 64, 'source': 'web', 'runtime': 'host', 'provider': 'codex', 'coordinator': 'hermes', 'thread_id': 'b' * 32, 'case_id': case_id, 'state': 'succeeded', 'message': '幫我檢查手機的設定流程。', 'answer': '手機設定流程已檢查完成。成果整理在下方，可以直接展開查看。', 'created_at': '2026-10-12T09:00:00Z'}
    def route(route):
        req = route.request
        path = req.url.split('/api/')[1]
        if req.method == 'POST':
            posts.append(path)
        value = common(path)
        if value is not None:
            pass
        elif path == 'bootstrap':
            value = {'state': 'completed', 'id': 'c' * 32, 'steps': [], 'provider': 'codex', 'channel': 'web'}
        elif path == 'gateway':
            value = {'preferences': {'runtime': 'host', 'provider': 'codex', 'coordinator': 'hermes'}, 'channels': []}
        elif path == 'gateway/messages':
            value = {'messages': [message]}
        elif path == 'hermes/cases/' + case_id:
            value = {'id': case_id, 'jobs': [{'id': job_id, 'state': 'succeeded', 'worker_id': 'host-coder', 'artifact_count': 3, 'evidence_count': 2, 'content_expired': False}]}
        elif path == 'workers':
            value = {'workers': [], 'active_job_id': None}
        elif path == 'worker/jobs/' + job_id:
            value = job
        else:
            raise AssertionError(path)
        route.fulfill(status=200, content_type='application/json', body=json.dumps(value))
    page.route('**/api/**', route)
    page.goto(origin + '/')
    page.get_by_role('heading', name='今天想處理什麼？').wait_for()
    assert page.url.endswith('#gateway')
    screenshot(page, 'assistant-welcome')
    page.get_by_role('button', name='看看家裡目前的狀態').click()
    expect(page.get_by_label('對話訊息')).to_have_value('看看家裡目前的狀態')
    assert not posts
    page.get_by_label('對話訊息').fill('')
    page.get_by_role('button', name='對話紀錄', exact=False).click()
    page.get_by_role('region', name='對話紀錄').get_by_role('button').filter(has_text=message['message']).click()
    page.get_by_text('3 份成果 · 2 份證據', exact=True).wait_for()
    screenshot(page, 'conversation-work')
    page.get_by_role('button', name='查看成果', exact=True).click()
    page.get_by_role('heading', name=job['goal'], exact=True).wait_for()
    expect(page.get_by_text(job['facts'][0], exact=True)).to_be_visible()
    assert page.url.endswith('#gateway')
    screenshot(page, 'assistant-results')
    job['case_id'] = 'f' * 32
    page.get_by_role('button', name='收起成果', exact=True).click()
    page.get_by_role('button', name='查看成果', exact=True).click()
    expect(page.get_by_role('alert')).to_contain_text('成果不屬於這段對話')
    expect(page.get_by_text(job['facts'][0], exact=True)).to_have_count(0)
    assert not posts


def test_chat_history_is_readonly_without_thread_and_locked_during_send(ui_page):
    page, origin = ui_page
    web = {'id': 'a' * 64, 'source': 'web', 'runtime': 'host', 'provider': 'codex', 'coordinator': 'hermes', 'thread_id': 'b' * 32, 'state': 'succeeded', 'message': '原本的家庭問題', 'answer': '請繼續補充。', 'created_at': '2026-10-12T09:00:00Z'}
    legacy = {**web, 'id': 'c' * 64, 'thread_id': None, 'message': '舊版沒有對話識別的訊息'}
    external = {**web, 'id': 'd' * 64, 'source': 'line', 'message': 'LINE 的對話', 'thread_id': 'e' * 32}
    messages, pending, posts = [external, legacy, web], [], []

    def route(route):
        req = route.request
        path = req.url.split('/api/')[1]
        value = common(path)
        if req.method == 'POST':
            assert path == 'gateway/messages'
            posts.append(req.post_data_json)
            pending.append(route)
            return
        if value is not None:
            pass
        elif path == 'gateway':
            value = {'preferences': {'runtime': 'host', 'provider': 'codex', 'coordinator': 'hermes'}, 'channels': []}
        elif path == 'gateway/messages':
            value = {'messages': messages}
        else:
            raise AssertionError(path)
        route.fulfill(status=200, content_type='application/json', body=json.dumps(value))

    page.add_init_script("sessionStorage.setItem('agentic-auth-conversations', JSON.stringify({'hermes-host-codex': '" + web['id'] + "'}))")
    page.route('**/api/**', route)
    page.goto(origin + '/#gateway')
    page.get_by_role('heading', name=web['message'], exact=True).wait_for()
    for message in (legacy, external):
        page.get_by_role('button', name='對話紀錄', exact=False).click()
        page.get_by_role('region', name='對話紀錄').get_by_role('button').filter(has_text=message['message']).click()
        expect(page.get_by_label('對話訊息')).to_have_count(0)
        assert json.loads(page.evaluate("sessionStorage.getItem('agentic-auth-conversations')")) == {'hermes-host-codex': web['id']}
        assert not posts
    page.get_by_role('button', name='對話紀錄', exact=False).click()
    page.get_by_role('region', name='對話紀錄').get_by_role('button').filter(has_text=web['message']).click()
    page.get_by_label('對話訊息').fill('補充現在的狀況')
    page.get_by_role('button', name='對話紀錄', exact=False).click()
    page.get_by_role('button', name='送出訊息', exact=True).click()
    expect(page.get_by_role('button', name='對話紀錄', exact=False)).to_be_disabled()
    for item in page.get_by_role('region', name='對話紀錄').get_by_role('button').all():
        expect(item).to_be_disabled()
    assert len(posts) == 1 and posts[0]['reply_to'] == web['id']
    result = {**web, **posts[0], 'id': 'f' * 64, 'answer': '收到補充。', 'created_at': '2026-10-12T09:01:00Z'}
    messages.insert(0, result)
    pending[0].fulfill(status=200, content_type='application/json', body=json.dumps(result))
    expect(page.get_by_role('heading', name=result['message'], exact=True)).to_be_visible()
    expect(page.get_by_label('對話訊息')).to_have_value('')
    assert len(posts) == 1
