"""Compiled UI with protocol fixtures; never claims real cloud/HAOS acceptance."""
import base64
import hashlib
import json
import re
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


def mount_home_verifier_fixture(page, origin):
    """Compiled browser, synthetic API receipt; no Home execution claim."""
    job = {'id': '9' * 32, 'case_id': '8' * 32, 'worker_id': 'home-coder', 'mode': 'development', 'provider': 'codex',
        'goal': '調查家庭連線', 'state': 'succeeded', 'execution_started': True, 'stop_confirmed': True,
        'created_at': '2026-10-12T19:00:00Z', 'runtime_location': 'haos_app', 'facts': ['調查結果與測試紀錄已保存。'],
        'artifacts': [], 'verification': {'observed_from': 'independent_home_verifier', 'state': 'passed',
            'tests_executed': 12, 'patch_sha256': 'a' * 64}}
    errors, posts = [], []
    def route(route):
        req = route.request
        path = req.url.split('/api/')[1]
        if req.method != 'GET':
            posts.append(path)
        value = common(path)
        if value is not None:
            pass
        elif path == 'workers':
            value = {'workers': [], 'active_job_id': None}
        elif path == 'worker/jobs':
            value = {'jobs': [job], 'active_job_id': None}
        else:
            raise AssertionError(path)
        route.fulfill(status=200, content_type='application/json', body=json.dumps(value))
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/api/**', route)
    page.goto(origin + '/#workers')
    page.get_by_role('heading', name=job['goal'], exact=True).wait_for()
    return job, errors, posts


def verifier_screenshot(page, stage, widths=(1440, 768, 390)):
    output = ROOT / 'evidence/ui/home-verifier'
    output.mkdir(parents=True, exist_ok=True)
    for width in widths:
        page.set_viewport_size({'width': width, 'height': 900 if width == 1440 else 844})
        page.evaluate('window.scrollTo(0,0)')
        page.wait_for_timeout(400)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(output / f'{stage}-{width}.png'), full_page=True)


def test_home_and_host_verification_show_exact_source_unknown_is_hidden(ui_page):
    page, origin = ui_page
    job, errors, posts = mount_home_verifier_fixture(page, origin)
    section = page.locator('.worker-findings').filter(has=page.get_by_role('heading', name='測試', exact=True))
    expect(section).to_be_visible()
    expect(section).to_contain_text('家庭現場基準測試通過（12 項）。')
    expect(section).to_contain_text('基準測試使用固定的模擬家庭，不等於你家的實際驗收。')
    verifier_screenshot(page, 'after')
    tech = page.locator('.worker-results details').filter(has=page.locator('summary', has_text='技術資料'))
    tech.locator('summary').click()
    expect(tech.locator('pre')).to_contain_text('independent_home_verifier')
    expect(tech.locator('pre')).to_contain_text('passed')
    expect(tech.locator('pre')).to_contain_text('a' * 64)
    job['worker_id'] = 'host-coder'
    job['verification']['observed_from'] = 'independent_host_verifier'
    page.reload()
    expect(section).to_contain_text('主機基準測試通過（12 項）。')
    expect(section).to_contain_text('基準測試使用固定的模擬家庭，不等於你家的實際驗收。')
    expect(section).not_to_contain_text('獨立測試')
    expect(section).not_to_contain_text('家庭現場')
    job['verification']['observed_from'] = 'unknown_model_claim'
    page.reload()
    page.get_by_role('heading', name=job['goal'], exact=True).wait_for()
    expect(page.get_by_role('heading', name='測試', exact=True)).to_have_count(0)
    expect(page.get_by_text('unknown_model_claim')).to_have_count(0)
    expect(page.get_by_text('查看驗證證據', exact=True)).to_have_count(0)
    assert not errors and not posts


def screenshot(page, name):
    for width in (1440, 768, 390):
        page.set_viewport_size({'width': width, 'height': 900 if width == 1440 else 844})
        page.evaluate('window.scrollTo(0,0)')
        page.wait_for_timeout(100)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(ROOT / f'evidence/ui/native-home/{name}-{width}.png'), full_page=True)


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
        elif path == 'host/home-worker/maintenance':
            value = {'contract': 1, 'state': 'not_authorized', 'stage': None, 'code': None, 'enabled': False,
                     'managed': False, 'release': {'repository': 'local', 'app': 'agentic_coder', 'version': '1.0.0'},
                     'credential_state': 'needs_auth', 'worker_available': False, 'runtime_location': 'haos_app',
                     'login': {'worker_id': 'home-coder', 'provider': 'codex', 'show': False,
                               'auth_path': '/api/workers/home-coder/providers/codex/auth',
                               'auth_poll_path': '/api/workers/home-coder/auth/{id}'},
                     'restart_allowed': False, 'limitations': ['workspace_has_no_network']}
        else:
            raise AssertionError(path)
        route.fulfill(status=200, content_type='application/json', body=json.dumps(value))

    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/api/**', route)
    page.goto(origin + '/#workers')
    page.get_by_label('要調查或改善什麼？', exact=True).wait_for()
    for width, height in ((1440, 900), (768, 844), (390, 844)):
        page.set_viewport_size({'width': width, 'height': height})
        page.evaluate('window.scrollTo(0,0)')
        box = page.get_by_role('button', name='登入 Codex 開發助手', exact=True).bounding_box()
        assert box and box['y'] >= 0 and box['y'] + box['height'] <= height, (width, box)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.set_viewport_size({'width': 1440, 'height': 900})
    page.get_by_label('要調查或改善什麼？', exact=True).fill('調查手機上的按鈕')
    expect(page.get_by_role('button', name='開始調查', exact=True)).to_be_disabled()
    page.get_by_role('button', name='登入 Codex 開發助手').click()
    page.get_by_text('SYNTHETIC-CODE', exact=True).wait_for()
    page.reload()
    page.get_by_text('SYNTHETIC-CODE', exact=True).wait_for()
    assert 'SYNTHETIC-CODE' not in page.evaluate('JSON.stringify({...sessionStorage})')
    auth['url'] = 'https://unexpected.example/collect'
    expect(page.get_by_role('link', name='開啟登入頁')).to_have_count(0)
    page.get_by_role('button', name='取消登入').click()
    page.get_by_role('button', name='重新登入', exact=True).click()
    page.get_by_label('開發助手登入方式').select_option('api_key')
    page.get_by_label('開發助手 API key').fill('synthetic-private-coding-key')
    page.get_by_role('button', name='登入 Codex 開發助手').click()
    page.get_by_text('已登入 Codex', exact=True).wait_for()
    assert 'synthetic-private-coding-key' not in page.evaluate('JSON.stringify({s:{...sessionStorage},l:{...localStorage}})')
    assert all('synthetic-private-coding-key' not in url for url in urls)
    page.get_by_label('要調查或改善什麼？', exact=True).fill('調查手機上的按鈕')
    page.get_by_role('button', name='開始調查', exact=True).click()
    page.get_by_role('button', name='停止這份工作', exact=True).wait_for()
    page.reload()
    page.get_by_role('button', name='停止這份工作', exact=True).click()
    page.get_by_role('button', name='核對停止與工作狀態').wait_for()
    page.get_by_label('要調查或改善什麼？', exact=True).fill('修正按鈕大小並測試')
    expect(page.get_by_role('button', name='開始調查', exact=True)).to_be_disabled()
    assert len([item for item in calls if item[0] == 'worker/jobs']) == 1
    page.get_by_role('button', name='核對停止與工作狀態').click()
    page.get_by_role('radio', name='修改並測試', exact=True).check()
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
    page.get_by_role('radio', name='主機', exact=True).focus()
    page.keyboard.press('ArrowRight')
    expect(page.get_by_role('radio', name='家庭現場', exact=True)).to_be_checked()
    page.keyboard.press('ArrowLeft')
    expect(page.get_by_role('radio', name='主機', exact=True)).to_be_checked()
    page.keyboard.press('ArrowRight')
    expect(page.get_by_role('radio', name='家庭現場', exact=True)).to_be_checked()
    page.get_by_label('要調查或改善什麼？', exact=True).fill('調查家庭現況')
    expect(page.get_by_role('button', name='開始修正與測試', exact=True)).to_be_disabled()
    expect(page.locator('.worker-readiness')).to_contain_text('尚未啟用')
    assert not errors


def test_network_and_digest_bound_update_review(ui_page):
    page, origin = ui_page
    network = {'lan': 'dhcp', 'remote': 'disabled', 'managed': False, 'tailscale': {'state': 'NeedsLogin'}}
    releases = {'trusted_publisher_configured': False, 'records': []}
    calls, errors = [], []
    manifest = {'version': '0.7.0', 'source_commit': 'b' * 40, 'tests': [{'name': 'browser-and-regression', 'result': 'passed'}]}
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
    page.locator('summary').filter(has_text='網路與遠端').click()
    expect(page.get_by_role('radio', name='自動取得 IP（建議）')).to_be_enabled()
    page.get_by_role('radio', name='自動取得 IP（建議）').focus()
    page.keyboard.press('ArrowRight')
    expect(page.get_by_role('radio', name='路由器保留固定 IP')).to_be_checked()
    page.get_by_role('radio', name='暫不啟用').focus()
    page.keyboard.press('ArrowRight')
    expect(page.get_by_role('radio', name='Tailscale 私人網路')).to_be_checked()
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
    page.locator('summary').filter(has_text='版本').click()
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
    page.get_by_role('button', name='退回前一版本', exact=True).click()
    assert not [item for item in calls if item[0] == 'updates/rollback']
    page.get_by_role('button', name='確認退版', exact=True).click()
    expect(page.get_by_role('region', name='更新候選版本').get_by_text('已退版', exact=True)).to_be_visible()
    page.get_by_role('button', name='登出此主機的 Tailscale').click()
    expect(page.get_by_role('radio', name='暫不啟用')).to_be_checked()
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
    page.goto(origin + '/#gateway')
    page.get_by_text('需要協助時，直接告訴管家。', exact=True).wait_for()
    assert page.url.endswith('#gateway')
    screenshot(page, 'assistant-welcome')
    assert page.get_by_role('heading', name='今天想處理什麼？').count() == 0
    page.get_by_label('對話訊息').fill('看看家裡目前的狀態')
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
        row = page.get_by_role('region', name='對話紀錄').get_by_role('button').filter(has_text=message['message'])
        if message['source'] == 'line':
            expect(row).to_contain_text('LINE')
        else:
            assert 'Web' not in (row.inner_text() or '')
        row.click()
        expect(page.get_by_label('對話訊息')).to_have_count(0)
        if message['source'] != 'web':
            expect(page.locator('.assistant-conversation .chat-meta')).to_contain_text('LINE')
            assert 'WEB ·' not in page.locator('.assistant-conversation').inner_text()
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


def coding_frames(page, name, check=None):
    output = ROOT / 'evidence/ui/coding-status'
    output.mkdir(parents=True, exist_ok=True)
    for width in (1440, 390):
        page.set_viewport_size({'width': width, 'height': 900 if width == 1440 else 844})
        page.evaluate('window.scrollTo(0,0)')
        page.wait_for_timeout(200)
        if check:
            check()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
        page.screenshot(path=str(output / f'{name}-{width}.png'), full_page=True)
    page.set_viewport_size({'width': 1440, 'height': 900})


def test_coding_job_phase_reason_stop_line_and_source_commit(ui_page):
    """Fixture-backed coding status copy. Not a real worker or cloud run."""
    page, origin = ui_page
    job_sha, source, install = 'abc1234' + 'e' * 33, 'f00db0b' + 'a' * 33, 'c0ffee1' + 'b' * 33
    sentences = {
        'setup_timeout': '準備環境逾時，已停止。',
        'execution_timeout': '執行逾時，已停止。已保留目前的結果。',
        'verification_timeout': '測試逾時，已停止。已保留修改內容。',
        'authorization_expired': '這次授權已到期，工作已停止。已保留目前的結果。',
        'authority_revoked': '授權已變更，工作已停止。',
        'owner_cancelled': '你已停止這份工作。',
        'broker_restarted': '主機重新啟動，工作已中斷。已保留目前的結果。',
        'coding_needs_auth': '開發助手需要重新登入。',
        'coding_needs_reauth': '開發助手需要重新登入。',
        'unsupported_model': '開發助手目前無法執行這個設定。',
        'unsupported_provider': '開發助手目前無法執行這個設定。',
        'worker_unavailable': '開發助手目前無法執行這個設定。',
        'runner_identity_changed': '這份工作沒有完成。',
    }
    retryable = {
        'setup_timeout', 'execution_timeout', 'verification_timeout', 'authorization_expired',
        'authority_revoked', 'owner_cancelled', 'broker_restarted',
    }
    host = {'id': 'codex', 'supported': True, 'credential_state': 'configured'}
    workers = [
        {'worker_id': 'host-coder', 'available': True, 'state': 'ready', 'reason': '', 'modes': ['research', 'development'],
         'runtime_location': 'host_worker_container', 'providers': [host], 'source_commit': source, 'install_commit': install},
        {'worker_id': 'home-coder', 'available': False, 'state': 'blocked', 'reason': '', 'modes': [], 'providers': []},
    ]
    job = {'id': '1' * 32, 'case_id': '2' * 32, 'worker_id': 'host-coder', 'mode': 'development', 'provider': 'codex',
           'model': 'gpt-6-astra', 'goal': '修正按鈕觸控範圍', 'state': 'launching', 'phase': 'launching', 'reason': None,
           'execution_started': False, 'stop_confirmed': False, 'created_at': '2026-10-12T09:00:00Z',
           'runtime_location': 'host_worker_container', 'base_commit': job_sha, 'facts': [], 'artifacts': []}
    errors = []

    def route(route):
        req = route.request
        path = req.url.split('/api/')[1]
        if req.method != 'GET':
            raise AssertionError(path)
        value = common(path)
        if value is None and path == 'workers':
            value = {'workers': workers, 'active_job_id': None}
        elif value is None and path == 'worker/jobs':
            value = {'jobs': [job], 'active_job_id': None}
        elif value is None:
            raise AssertionError(path)
        route.fulfill(status=200, content_type='application/json', body=json.dumps(value))

    def show():
        page.reload()
        expect(page.get_by_role('heading', name=job['goal'], exact=True)).to_be_visible()

    def chip():
        return page.locator('.worker-result-head .status')

    def retry():
        return page.locator('.worker-results').get_by_role('button', name='再試一次', exact=True)

    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/api/**', route)
    page.goto(origin + '/#workers')
    expect(page.get_by_role('heading', name=job['goal'], exact=True)).to_be_visible()
    expect(chip()).to_have_text('準備環境')
    expect(chip()).to_have_class(re.compile(r'(^|\s)s-launching(\s|$)'))
    expect(page.locator('.worker-results').get_by_role('button', name='停止這份工作', exact=True)).to_be_visible()
    expect(retry()).to_have_count(0)
    expect(page.get_by_text('已確認工作程序停止。', exact=True)).to_have_count(0)
    coding_frames(page, 'phase-launching', lambda: expect(chip()).to_have_text('準備環境'))

    form = page.locator('.worker-request details').filter(has=page.locator('summary', has_text='技術資料'))
    form.locator('summary').click()
    form_code = form.locator('code')
    expect(form.get_by_text('主機程式版本', exact=False)).to_be_visible()
    expect(form_code).to_have_text(source[:7])
    expect(form_code).to_have_attribute('title', source)
    assert source not in form.inner_text()
    assert install not in (page.locator('.worker-page').text_content() or '')
    page.set_viewport_size({'width': 390, 'height': 844})
    page.get_by_role('button', name='新工作', exact=True).click()
    expect(form_code).to_be_visible()
    expect(form_code).to_have_text(source[:7])
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.get_by_role('button', name='查看成果', exact=True).click()
    page.set_viewport_size({'width': 1440, 'height': 900})

    live = (
        ('queued', 'queued', '排隊中', '停止這份工作'),
        ('dispatching', 'queued', '排隊中', '停止這份工作'),
        ('launching', 'launching', '準備環境', '停止這份工作'),
        ('running', 'running', '執行中', '停止這份工作'),
        ('running', 'verifying', '測試中', '停止這份工作'),
        ('running', 'collecting', '整理結果', '停止這份工作'),
    )
    held = (
        ('cancel_requested', 'running', '正在停止', '核對停止與工作狀態'),
        ('stop_unconfirmed', 'running', '停止尚未確認', '核對停止與工作狀態'),
        ('needs_reconcile', 'running', '需要核對', '核對停止與工作狀態'),
    )
    for state, phase, label, action in (*live, *held):
        job.update(state=state, phase=phase, reason=None, stop_confirmed=False)
        show()
        token = state if state in ('cancel_requested', 'stop_unconfirmed', 'needs_reconcile') else phase
        expect(chip()).to_have_text(label)
        expect(chip()).to_have_class(re.compile(rf'(^|\s)s-{token}(\s|$)'))
        expect(page.locator('.worker-results').get_by_role('button', name=action, exact=True)).to_be_visible()
        expect(retry()).to_have_count(0)
        expect(page.locator('.worker-results [data-reason]')).to_have_count(0)
    job.update(state='running', reason=None, stop_confirmed=False)
    job.pop('phase', None)
    show()
    expect(chip()).to_have_text('處理中')
    expect(page.locator('.worker-results').get_by_role('button', name='停止這份工作', exact=True)).to_be_visible()
    job['phase'] = 'not_a_phase'
    show()
    expect(chip()).to_have_text('處理中')
    job.update(state='succeeded', phase='running', reason=None, stop_confirmed=True)
    show()
    expect(chip()).to_have_text('已完成')
    expect(retry()).to_have_count(0)
    stopped = page.get_by_text('已確認工作程序停止。', exact=True)
    expect(stopped).to_have_count(1)
    expect(stopped).to_be_hidden()
    tech = page.locator('.worker-results details').filter(has=page.locator('summary', has_text='技術資料'))
    tech.locator('summary').click()
    expect(stopped).to_be_visible()
    version = tech.locator('dd').locator('code').filter(has_text=job_sha[:7])
    expect(version).to_have_text(job_sha[:7])
    expect(version).to_have_attribute('title', job_sha)
    assert job_sha not in tech.inner_text()
    expect(tech.get_by_text('原因代碼', exact=False)).to_have_count(0)

    for blank in (None, '', 'abc1234', 'A' * 40):
        job['base_commit'] = blank
        job['reason'] = None
        show()
        opened = page.locator('.worker-results details').filter(has=page.locator('summary', has_text='技術資料'))
        opened.locator('summary').click()
        expect(opened.get_by_text('程式版本', exact=True)).to_have_count(0)
        assert job_sha not in (opened.text_content() or '')
    job.pop('base_commit', None)
    show()
    opened = page.locator('.worker-results details').filter(has=page.locator('summary', has_text='技術資料'))
    opened.locator('summary').click()
    expect(opened.get_by_text('程式版本', exact=True)).to_have_count(0)

    for blank in (None, '', 'not-a-commit'):
        workers[0]['source_commit'] = blank
        show()
        place = page.locator('.worker-request details').filter(has=page.locator('summary', has_text='技術資料'))
        place.locator('summary').click()
        expect(place.get_by_text('主機程式版本', exact=False)).to_have_count(0)
    workers[0].pop('source_commit', None)
    show()
    place = page.locator('.worker-request details').filter(has=page.locator('summary', has_text='技術資料'))
    place.locator('summary').click()
    expect(place.get_by_text('主機程式版本', exact=False)).to_have_count(0)
    assert install[:7] not in (page.locator('.worker-page').text_content() or '')

    for code, sentence in sentences.items():
        job.update(state='failed', phase='failed', reason=code, stop_confirmed=True)
        show()
        notice = page.locator('.worker-results [data-reason]')
        expect(notice).to_have_count(1)
        expect(notice).to_have_attribute('data-reason', code)
        expect(notice).to_have_attribute('role', 'status')
        expect(notice).to_have_text(sentence)
        assert code not in page.locator('body').inner_text()
        if code in retryable:
            expect(retry()).to_be_enabled()
        else:
            expect(retry()).to_have_count(0)
        if code == 'execution_timeout':
            def retry_frame(current=sentence, marker=code):
                expect(page.locator('.worker-results [data-reason]')).to_have_text(current)
                expect(retry()).to_be_visible()
                assert marker not in page.locator('body').inner_text()
            coding_frames(page, 'reason-retry', retry_frame)
        opened = page.locator('.worker-results details').filter(has=page.locator('summary', has_text='技術資料'))
        opened.locator('summary').click()
        expect(opened).to_contain_text(f'原因代碼：{code}')
    job.update(state='failed', phase='failed', reason='retention_workspace_cleanup_pending', stop_confirmed=True)
    show()
    expect(page.locator('.worker-results [data-reason]')).to_have_count(0)
    expect(retry()).to_have_count(0)
    assert 'retention_workspace_cleanup_pending' not in page.locator('body').inner_text()
    opened = page.locator('.worker-results details').filter(has=page.locator('summary', has_text='技術資料'))
    opened.locator('summary').click()
    expect(opened).to_contain_text('原因代碼：retention_workspace_cleanup_pending')
    job['reason'] = None
    show()
    expect(page.locator('.worker-results [data-reason]')).to_have_count(0)
    expect(retry()).to_have_count(0)
    opened = page.locator('.worker-results details').filter(has=page.locator('summary', has_text='技術資料'))
    opened.locator('summary').click()
    expect(opened.get_by_text('原因代碼', exact=False)).to_have_count(0)
    job.pop('reason', None)
    show()
    expect(page.locator('.worker-results [data-reason]')).to_have_count(0)
    expect(retry()).to_have_count(0)
    opened = page.locator('.worker-results details').filter(has=page.locator('summary', has_text='技術資料'))
    opened.locator('summary').click()
    expect(opened.get_by_text('原因代碼', exact=False)).to_have_count(0)
    job.update(state='launching', phase='launching', reason='setup_timeout', stop_confirmed=False)
    show()
    expect(chip()).to_have_text('準備環境')
    expect(page.locator('.worker-results [data-reason]')).to_have_text('準備環境逾時，已停止。')
    expect(page.locator('.worker-results').get_by_role('button', name='停止這份工作', exact=True)).to_be_visible()
    expect(retry()).to_have_count(0)
    assert not errors, errors


def test_retry_submits_a_new_coding_job_only_on_the_worker_page(ui_page):
    """再試一次 is a new submit on the coding page only. Fixtures, not a real run."""
    page, origin = ui_page
    original = {'worker_id': 'host-coder', 'mode': 'development', 'goal': '修正按鈕觸控範圍', 'repo_id': 'agentic-home',
                'provider': 'codex', 'model': 'gpt-6-astra'}
    job_sha = 'abc1234' + 'e' * 33
    host = {'id': 'codex', 'supported': True, 'credential_state': 'configured'}
    home_account = {'id': 'codex', 'supported': True, 'credential_state': 'connected'}
    workers = [
        {'worker_id': 'host-coder', 'available': True, 'state': 'ready', 'reason': '', 'modes': ['research', 'development'],
         'runtime_location': 'host_worker_container', 'providers': [host], 'source_commit': None, 'install_commit': None},
        {'worker_id': 'home-coder', 'available': True, 'state': 'ready', 'reason': '', 'modes': ['research', 'development'],
         'runtime_location': 'haos_app', 'providers': [home_account], 'source_commit': None},
    ]
    listed = {'jobs': [], 'active_job_id': None}
    posted, errors, seen = {}, [], []
    fail = {'on': False}
    home = {'state': 'not_authorized'}
    surface = {'mode': 'host'}
    case_id, convo_id = 'c' * 32, 'd' * 32
    message = {'id': 'a' * 64, 'source': 'web', 'runtime': 'host', 'provider': 'codex', 'coordinator': 'hermes',
               'thread_id': 'b' * 32, 'case_id': case_id, 'state': 'succeeded', 'message': '幫我看這次修正',
               'answer': '成果在下面。', 'created_at': '2026-10-12T09:00:00Z'}
    convo = {'id': convo_id, 'case_id': case_id, 'worker_id': 'host-coder', 'mode': 'development', 'provider': 'codex',
             'model': 'gpt-6-astra', 'goal': '改善手機設定流程', 'state': 'failed', 'phase': 'failed',
             'reason': 'execution_timeout', 'execution_started': True, 'stop_confirmed': True, 'base_commit': job_sha,
             'runtime_location': 'host_worker_container', 'facts': ['結果已保留。'], 'artifacts': []}

    def route(route):
        req = route.request
        path = req.url.split('/api/')[1]
        seen.append((surface['mode'], req.method, path))
        if surface['mode'] == 'bridge' and (path == 'workers' or path.startswith('workers/')):
            raise AssertionError(path)
        body = req.post_data_json if req.method == 'POST' else None
        if body is not None:
            assert req.headers['x-agentic-csrf'] == 'fixture-csrf'
            assert re.fullmatch(r'[a-f0-9]{32}', req.headers['idempotency-key'])
            posts.append((path, body, req.headers['idempotency-key']))
            if path == 'worker/jobs' and fail['on']:
                fail['on'] = False
                route.fulfill(status=409, content_type='application/json', body=json.dumps({'detail': '主機暫時沒有接受這份工作。'}))
                return
        if path == 'deployment' and surface['mode'] == 'bridge':
            value = {'mode': 'bridge', 'authenticated': True, 'bridge_state': 'ready'}
        elif path == 'session' and surface['mode'] == 'bridge':
            value = {'csrf': 'fixture-csrf', 'mode': 'bridge', 'principal': 'owner-a', 'storage_scope': '1' * 64,
                     'resume': {'message_id': message['id'], 'thread_id': message['thread_id'], 'case_id': case_id}}
        else:
            value = common(path)
        if value is None and path == 'workers':
            value = {'workers': workers, 'active_job_id': listed['active_job_id']}
        elif value is None and path == 'worker/jobs' and body is None:
            value = {'jobs': listed['jobs'], 'active_job_id': listed['active_job_id']}
        elif value is None and path == 'worker/jobs':
            key = req.headers['idempotency-key']
            if key in posted:
                value = posted[key]
            else:
                created = {'id': str(len(listed['jobs']) + 1) * 32, 'case_id': 'e' * 32, 'worker_id': body['worker_id'],
                           'mode': body['mode'], 'provider': body['provider'], 'model': body['model'], 'goal': body['goal'],
                           'state': 'failed', 'phase': 'failed', 'reason': 'execution_timeout', 'execution_started': True,
                           'stop_confirmed': True, 'base_commit': job_sha, 'created_at': '2026-10-12T09:00:00Z',
                           'runtime_location': 'host_worker_container', 'facts': [], 'artifacts': []}
                listed['jobs'].insert(0, created)
                posted[key] = created
                value = created
        elif value is None and path == 'host/home-worker/maintenance':
            value = {'contract': 1, 'state': home['state'], 'stage': None, 'code': None, 'restart_allowed': False,
                     'limitations': [], 'release': {'version': '1.0.0'}}
        elif value is None and path == 'gateway':
            value = {'preferences': {'runtime': 'host', 'provider': 'codex', 'coordinator': 'hermes'}, 'channels': []}
        elif value is None and path == 'gateway/messages':
            value = {'messages': [message]}
        elif value is None and path == f'hermes/cases/{case_id}':
            value = {'id': case_id, 'jobs': [{'id': convo_id, 'state': 'failed', 'worker_id': 'host-coder',
                     'artifact_count': 1, 'evidence_count': 1, 'content_expired': False}]}
        elif value is None and path == f'worker/jobs/{convo_id}':
            value = convo
        elif value is None:
            raise AssertionError(path)
        route.fulfill(status=200, content_type='application/json', body=json.dumps(value))

    posts = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/api/**', route)
    page.goto(origin + '/#workers')
    page.get_by_label('要調查或改善什麼？', exact=True).wait_for()
    page.get_by_role('radio', name='修改並測試', exact=True).check()
    page.get_by_text('模型設定', exact=True).click()
    page.get_by_label('開發模型', exact=True).fill(original['model'])
    page.get_by_label('要調查或改善什麼？', exact=True).fill(original['goal'])
    page.get_by_role('button', name='開始修正與測試', exact=True).click()
    retry = page.locator('.worker-results').get_by_role('button', name='再試一次', exact=True)
    expect(retry).to_be_enabled()
    expect(page.get_by_label('要調查或改善什麼？', exact=True)).to_have_value('')
    page.get_by_label('要調查或改善什麼？', exact=True).fill('另一個不該送出的目標')
    page.get_by_label('開發模型', exact=True).fill('other-model')
    page.get_by_role('radio', name='調查問題', exact=True).check()
    retry.click()
    expect(page.locator('.worker-results select')).to_have_value('2' * 32)
    assert [item[1] for item in posts] == [original, original]
    assert posts[0][2] != posts[1][2]
    # The label's text includes the textarea value, so exact get_by_label misses a filled field.
    page.get_by_role('textbox', name='要調查或改善什麼？', exact=True).fill(original['goal'])
    page.get_by_role('combobox', name='開發模型', exact=True).fill(original['model'])
    page.get_by_role('radio', name='修改並測試', exact=True).check()
    page.get_by_role('button', name='開始修正與測試', exact=True).click()
    expect(page.locator('.worker-results select')).to_have_value('3' * 32)
    assert posts[2][1] == original and posts[2][2] not in {posts[0][2], posts[1][2]}
    fail['on'] = True
    retry.click()
    alert = page.locator('.worker-results .notice.error')
    expect(alert.locator('p')).to_have_text('這次沒有送出，請稍後再試。')
    expect(alert.locator('small')).to_have_text('主機暫時沒有接受這份工作。')
    expect(page.locator('.worker-results select')).to_have_value('3' * 32)
    assert len(listed['jobs']) == 3

    listed['active_job_id'] = '3' * 32
    page.reload()
    expect(retry).to_be_disabled()
    listed['active_job_id'] = None
    host['credential_state'] = 'needs_auth'
    page.reload()
    expect(retry).to_be_disabled()
    host['credential_state'] = 'configured'
    listed['jobs'][0]['worker_id'] = 'home-coder'
    page.reload()
    expect(retry).to_be_disabled()
    home['state'] = 'not_authorized'
    page.get_by_role('radio', name='家庭現場', exact=True).check()
    expect(page.locator('.worker-readiness')).to_contain_text('尚未啟用')
    expect(retry).to_be_disabled()
    home['state'] = 'ready'
    home_account['credential_state'] = 'needs_auth'
    page.reload()
    page.get_by_role('radio', name='家庭現場', exact=True).check()
    expect(page.locator('.worker-readiness')).to_contain_text('需要登入')
    expect(retry).to_be_disabled()
    home_account['credential_state'] = 'connected'
    page.reload()
    page.get_by_role('radio', name='家庭現場', exact=True).check()
    expect(page.locator('.worker-readiness')).to_contain_text('已就緒')
    expect(retry).to_be_enabled()

    page.goto(origin + '/#gateway')
    page.get_by_role('button', name='對話紀錄', exact=False).click()
    page.get_by_role('region', name='對話紀錄').get_by_role('button').filter(has_text=message['message']).click()
    page.get_by_role('button', name='查看成果', exact=True).click()
    expect(page.get_by_role('heading', name=convo['goal'], exact=True)).to_be_visible()
    expect(page.get_by_text('執行逾時，已停止。已保留目前的結果。', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='再試一次', exact=True)).to_have_count(0)
    assert 'execution_timeout' not in page.locator('body').inner_text()
    stopped = page.get_by_text('已確認工作程序停止。', exact=True)
    expect(stopped).to_have_count(1)
    expect(stopped).to_be_hidden()
    convo_tech = page.locator('.assistant-result details').filter(has=page.locator('summary', has_text='技術資料'))
    convo_tech.locator('summary').click()
    expect(stopped).to_be_visible()
    expect(convo_tech.locator('code', has_text=job_sha[:7])).to_have_attribute('title', job_sha)
    assert page.get_by_role('button', name='下載接手檢查點').count() >= 1
    page.set_viewport_size({'width': 390, 'height': 844})
    page.evaluate('window.scrollTo(0,0)')
    expect(page.get_by_role('button', name='再試一次', exact=True)).to_have_count(0)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.set_viewport_size({'width': 1440, 'height': 900})

    surface['mode'] = 'bridge'
    # Same URL does not navigate again; reload so the sidebar session is a fresh bridge page.
    page.reload()
    expect(page.locator('.bridge-shell')).to_be_visible()
    page.get_by_role('button', name='查看成果', exact=True).click()
    expect(page.get_by_text('執行逾時，已停止。已保留目前的結果。', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='再試一次', exact=True)).to_have_count(0)
    assert page.get_by_role('button', name='下載接手檢查點').count() == 0
    bridge_stopped = page.get_by_text('已確認工作程序停止。', exact=True)
    expect(bridge_stopped).to_be_hidden()
    page.locator('.assistant-result details').locator('summary', has_text='技術資料').click()
    expect(bridge_stopped).to_be_visible()
    assert 'workers' not in [item[2] for item in seen if item[0] == 'bridge']
    page.set_viewport_size({'width': 390, 'height': 844})
    page.evaluate('window.scrollTo(0,0)')
    expect(page.get_by_role('button', name='再試一次', exact=True)).to_have_count(0)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert not errors, errors
