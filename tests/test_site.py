"""Public page and product frames, using a shared fictional browser household."""
import hashlib
import importlib.util
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('site_screenshots', ROOT / 'scripts/site_screenshots.py')
site = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(site)


@pytest.fixture(scope='module')
def origin(tmp_path_factory):
    demo = tmp_path_factory.mktemp('pages-demo')
    site.build_demo(demo)
    with site.site_server(demo) as url:
        yield url


@pytest.fixture
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        yield browser
        browser.close()


def watch(page, origin):
    errors, external = [], []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
    page.on('request', lambda request: external.append(request.url)
            if urlsplit(request.url).netloc != urlsplit(origin).netloc else None)
    page.add_init_script("document.addEventListener('securitypolicyviolation',e=>console.error(e.violatedDirective))")
    return errors, external


def clean(page, errors, external):
    assert not errors, errors
    assert not external, external
    assert page.evaluate('window.__agenticDemo?.unhandled || []') == []


@pytest.mark.parametrize('lang', ['zh-TW', 'en'])
def test_landing(browser, origin, lang):
    context = browser.new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    errors, external = watch(page, origin)
    page.goto(f'{origin}/?lang={lang}')
    expect(page.locator('html')).to_have_attribute('data-lang', 'zh' if lang == 'zh-TW' else 'en')
    for image in page.locator('img').all():
        assert image.get_attribute('alt')
        assert image.get_attribute('width') and image.get_attribute('height')
        image.evaluate("img=>{img.loading='eager'}")
    for image in page.locator('img').all():
        # decode() settles once the image has loaded and fails for a broken one. A string predicate
        # polled inside the page would need eval, which the page's self-only CSP refuses.
        image.evaluate('img=>img.decode()')
        assert image.evaluate('img=>img.complete && img.naturalWidth>0')
    # Resolve every local resource including below-fold, hidden switcher images and demo links.
    for resource in page.locator('[href], [src]').all():
        raw = resource.get_attribute('href') or resource.get_attribute('src')
        if not raw or raw.startswith('#') or urlsplit(raw).scheme:
            continue
        response = context.request.get(f'{origin}/{raw.split("#")[0]}')
        assert response.ok, raw
    page.get_by_role('button', name='EN', exact=True).click()
    expect(page.locator('html')).to_have_attribute('lang', 'en')
    expect(page).to_have_title('Agentic Smart Home · Your home, your call')
    assert '?lang=en' in page.url
    page.goto(origin)
    expect(page.locator('html')).to_have_attribute('data-lang', 'en')
    page.get_by_role('button', name='繁中', exact=True).click()
    assert '?lang=zh-TW' in page.url
    page.reload()
    expect(page.locator('html')).to_have_attribute('lang', 'zh-Hant')
    page.get_by_role('button', name='設備權限', exact=True).click()
    expect(page.locator('[data-panel=inventory]')).to_be_visible()
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.get_by_role('button', name='選單', exact=True).click()
    expect(page.get_by_role('button', name='繁中', exact=True)).to_be_visible()
    clean(page, errors, external)
    context.close()


def product_frame(page, surface='outer'):
    element = page.locator(f'#agentic-demo-frame-{surface}')
    expect(element).to_be_visible()
    frame = element.element_handle().content_frame()
    frame.locator('#root').wait_for()
    return frame


def nav(page, label):
    page.get_by_role('navigation', name='主選單').get_by_role('button', name=label, exact=True).click()
    expect(page.get_by_role('heading', name=label, exact=True).first).to_be_visible()


def approve(page, status='等待真實觸發', message='已部署並啟用，正在等待真實狀態觸發'):
    page.get_by_role('button', name='批准並執行', exact=True).click()
    expect(page.locator('.task-message')).to_contain_text('備份')
    expect(page.locator('.task-detail .status')).to_have_text(status, timeout=12000)
    expect(page.locator('.task-message')).to_contain_text(message)
    # As in the product, deploying a rule observes nothing until a real trigger.
    expect(page.get_by_role('heading', name='觀測結果')).to_have_count(0)


def test_household_flow(browser, origin):
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    errors, external = watch(page, origin)
    page.goto(f'{origin}/demo/#gateway')
    ui = product_frame(page)
    expect(page.get_by_text('互動示範 · 虛構家庭', exact=True)).to_be_visible()
    ui.locator('#gateway-message').fill('有人經過玄關時打開燈')
    expect(ui.get_by_role('button', name='送出訊息')).to_be_enabled()
    ui.locator('#gateway-message').press('Enter')
    expect(ui.get_by_text('提案已準備好。先看精確內容，再決定是否批准。')).to_be_visible()
    ui.get_by_role('link', name='查看提案 →').last.click()
    ui.get_by_text('查看設定差異', exact=True).click()
    expect(ui.locator('.proposal-diff')).to_contain_text('light.demo_living_room')
    approve(ui)
    nav(ui, '智能管家')
    expect(ui.get_by_label('目前對話').get_by_text('已部署並啟用，正在等待真實狀態觸發')).to_be_visible()
    nav(ui, '自動化')
    expect(ui.locator('.automation-row').filter(has_text='玄關有人時開啟客廳燈')).to_be_visible()
    nav(ui, '紀錄')
    ui.locator('.history-table > button').filter(has_text='玄關有人時開啟客廳燈').click()
    expect(ui.locator('.task-detail .status')).to_have_text('等待真實觸發')
    nav(ui, '設備')
    expect(ui.locator('.discovery').get_by_text('示範待設定橋接器', exact=True)).to_be_visible()
    expect(ui.locator('.discovery .notice.error')).to_have_count(0)
    ui.get_by_role('checkbox', name='分享 light.demo_living_room').uncheck()
    ui.get_by_role('button', name='儲存修改', exact=True).click()
    expect(ui.get_by_text('已儲存。受影響的提案需要重新產生。')).to_be_visible()
    nav(ui, '設定')
    nav(ui, '設備')
    expect(ui.get_by_role('checkbox', name='分享 light.demo_living_room')).not_to_be_checked()
    nav(ui, '自動化')
    row = ui.locator('.automation-row').filter(has_text='玄關有人時開啟客廳燈')
    row.get_by_role('button', name='預覽停用').click()
    approve(ui, '已完成', '規則已停用並讀回')
    nav(ui, '自動化')
    row = ui.locator('.automation-row').filter(has_text='玄關有人時開啟客廳燈')
    expect(row).to_contain_text('已停用')
    row.get_by_role('button', name='預覽啟用').click()
    approve(ui)
    nav(ui, '自動化')
    row = ui.locator('.automation-row').filter(has_text='玄關有人時開啟客廳燈')
    expect(row).to_contain_text('已啟用')
    row.locator('summary').click()
    row.get_by_role('button', name='預覽回復').click()
    approve(ui)
    clean(page, errors, external)
    page.close()


def test_declined_proposal_keeps_the_conversation_open(browser, origin):
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    errors, external = watch(page, origin)
    page.goto(f'{origin}/demo/#gateway')
    ui = product_frame(page)
    ui.get_by_role('button', name='新對話', exact=True).click()
    ui.locator('#gateway-message').fill('出門時關燈')
    expect(ui.get_by_role('button', name='送出訊息')).to_be_enabled()
    ui.locator('#gateway-message').press('Enter')
    ui.get_by_role('link', name='查看提案 →').click()
    ui.get_by_role('button', name='取消提案', exact=True).click()
    expect(ui.locator('.task-detail .status')).to_have_text('已取消')
    nav(ui, '智能管家')
    expect(ui.get_by_label('目前對話').get_by_text('已取消，沒有變更家庭')).to_be_visible()
    # The owner can say what to change instead of starting over.
    expect(ui.locator('#gateway-message')).to_have_attribute('placeholder', '繼續說…')
    expect(ui.get_by_text('這段對話暫時無法接續', exact=False)).to_have_count(0)
    ui.get_by_role('button', name='對話紀錄', exact=False).click()
    expect(ui.locator('.assistant-history-item').filter(has_text='出門時關燈')).to_contain_text('已取消')
    clean(page, errors, external)
    page.close()


def test_workers_and_host(browser, origin):
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    errors, external = watch(page, origin)
    page.goto(f'{origin}/demo/#workers')
    ui = product_frame(page)
    for title in ['發現', '測試', '變更']:
        expect(ui.get_by_role('heading', name=title, exact=True)).to_be_visible()
    ui.get_by_role('button', name='查看內容', exact=True).click()
    expect(ui.locator('.diff-add').first).to_contain_text('玄關感應器')
    ui.get_by_label('要調查或改善什麼？').fill('調查示範燈的說明')
    ui.get_by_role('button', name='開始調查', exact=True).click()
    expect(ui.locator('.worker-result-head')).to_contain_text('調查示範燈的說明')
    expect(ui.locator('.worker-result-head .status')).to_have_text('已完成', timeout=12000)
    job_id = ui.get_by_label('查看工作').input_value()
    expect(page).to_have_url(f'{origin}/demo/#workers/{job_id}')
    expect(ui.get_by_role('heading', name='發現', exact=True)).to_be_visible()
    # Select the seed job so the deep link remains meaningful after demo state resets on reload.
    ui.get_by_label('查看工作').select_option('9' * 32)
    expect(page).to_have_url(f"{origin}/demo/#workers/{'9' * 32}")
    page.reload()
    ui = product_frame(page)
    expect(ui.locator('.worker-result-head')).to_contain_text('把示範家庭的客廳燈自動化說明寫清楚')
    nav(ui, '設定')
    ui.locator('.settings-hub a[href="#host"]').click()
    expect(ui.get_by_role('heading', name='示範主機', exact=True)).to_be_visible()
    ui.get_by_role('button', name='檢查連線', exact=True).click()
    expect(ui.get_by_text('主機與家庭都有回應。', exact=True)).to_be_visible()
    ui.locator('summary').filter(has_text='版本 0.11.0').click()
    expect(ui.get_by_text('先檢查簽署的版本說明，確認後才更新。')).to_be_visible()
    ui.locator('summary').filter(has_text='快照 ·').click()
    expect(ui.get_by_role('button', name='選擇還原')).to_be_visible()
    ui.get_by_role('link', name='開啟家庭 ↗', exact=True).click()
    expect(page).to_have_url(f'{origin}/demo/#/lovelace/home')
    expect(page.get_by_role('heading', name='我的家', exact=True)).to_be_visible()
    page.go_back()
    expect(page).to_have_url(f'{origin}/demo/#host')
    # Middle clicks also stay inside the fictional household rather than opening HA.
    ui.get_by_role('link', name='開啟家庭 ↗', exact=True).click(button='middle')
    expect(page).to_have_url(f'{origin}/demo/#/lovelace/home')
    assert len(page.context.pages) == 1
    page.go_back()
    expect(page).to_have_url(f'{origin}/demo/#host')
    ui.evaluate("() => {const a=document.createElement('a');a.href='downloads/demo.zip';a.textContent='download guard';document.body.append(a);a.click();a.remove()}")
    expect(ui.get_by_text('示範中無法下載。', exact=True)).to_be_visible()
    clean(page, errors, external)
    page.close()


@pytest.mark.parametrize('width', [390, 768, 1440])
def test_ha_lan_shutdown_disable_save_start_is_browser_only(browser, origin, width):
    from agentic_controller.ha_lan import HomeLanStatus

    page = browser.new_page(viewport={'width': width, 'height': 900 if width == 1440 else 844})
    errors, external = watch(page, origin)
    page.clock.install()
    page.goto(f'{origin}/demo/?screenshot=1#host')
    ui = product_frame(page)
    ui.locator('summary').filter(has_text='家庭區網接入').click()
    lan = ui.get_by_role('region', name='家庭區網接入')
    select = lan.get_by_label('接到家庭路由器的網路線')
    expect(select).to_be_disabled()
    expect(lan.get_by_text('已讀回家庭的區網位址、探索介面與本機網址。', exact=True)).to_be_visible()
    expect(lan.get_by_text('接著到「裝置與服務」新增設備，並實測控制；這項檢查不代表所有設備已接通。')).to_be_visible()
    lan.locator('summary').filter(has_text='連線資料').click()
    expect(lan.get_by_text('http://192.168.50.20:8123', exact=True)).to_be_visible()
    assert ui.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')

    def readback():
        value = page.evaluate("window.__agenticDemoModel.request('network/ha-lan','GET',{},'outer').body")
        HomeLanStatus.model_validate(value)
        return value

    initial = readback()
    assert initial['home']['state'] == 'ready'
    assert 'no_device_or_matter_control_verified' in initial['home']['limits']
    ui.get_by_role('button', name='正常關機', exact=True).click()
    expect(ui.get_by_text('關閉家庭系統？家中自動化會暫停。', exact=True)).to_be_visible()
    assert readback()['vm_state'] == 'running'  # Opening the confirmation makes no change.
    ui.get_by_role('button', name='確認關機', exact=True).click()
    page.clock.run_for(6500)
    expect(ui.get_by_role('region', name='家庭系統').get_by_text('已關機', exact=True)).to_be_visible()
    expect(select).to_be_enabled()
    assert readback()['home']['state'] == 'unavailable'
    expect(lan.get_by_text('http://192.168.50.20:8123', exact=True)).to_have_count(0)
    select.select_option('')
    lan.get_by_role('button', name='保存家庭接線', exact=True).click()
    expect(lan.get_by_role('button', name='正在保存…', exact=True)).to_be_disabled()
    page.clock.run_for(6500)
    expect(lan.get_by_text('尚未接入區網', exact=True)).to_be_visible()
    disabled = readback()
    assert disabled['mode'] == 'nat' and disabled['selected_interface_id'] is None
    assert disabled['home']['state'] == 'not_configured' and disabled['home']['ipv4'] == []
    assert disabled['home']['internal_url'] is None
    ui.get_by_role('button', name='啟動', exact=True).click()
    page.clock.run_for(6500)
    expect(ui.get_by_role('region', name='家庭系統').get_by_text('運作中', exact=True)).to_be_visible()
    expect(select).to_be_disabled()
    restarted = readback()
    assert restarted['vm_state'] == 'running' and restarted['mode'] == 'nat'
    assert restarted['home']['state'] == 'not_configured'
    # Collapsing/reopening reads the same memory state; reloading resets the fictional household.
    ui.locator('summary').filter(has_text='家庭區網接入').click()
    ui.locator('summary').filter(has_text='家庭區網接入').click()
    expect(ui.get_by_role('region', name='家庭區網接入').get_by_text('尚未接入區網', exact=True)).to_be_visible()
    jobs = page.evaluate("window.__agenticDemoModel.request('host/status','GET',{},'outer').body.jobs")
    assert [job['request']['action'] for job in jobs[-3:]] == ['shutdown', 'configure_lan', 'start']
    assert all(job['state'] == 'succeeded' for job in jobs[-3:])
    clean(page, errors, external)
    page.reload()
    ui = product_frame(page)
    ui.locator('summary').filter(has_text='家庭區網接入').click()
    expect(ui.get_by_role('region', name='家庭區網接入').get_by_text('已設定接線', exact=True)).to_be_visible()
    assert readback()['home']['state'] == 'ready'
    clean(page, errors, external)
    page.close()


def test_ha_lan_demo_rejects_unsafe_selection_and_reuses_jobs(browser, origin):
    from agentic_controller.ha_lan import HomeLanStatus

    page = browser.new_page()
    errors, external = watch(page, origin)
    page.clock.install()
    page.goto(f'{origin}/demo/?screenshot=1#host')
    product_frame(page)

    def request(path, method='GET', body=None, key=None):
        return page.evaluate("([path,method,body,key])=>window.__agenticDemoModel.request(path,method,body,'outer',key)",
                             [path, method, body or {}, key])

    def submit(body, key):
        return request('host/actions', 'POST', body, key)

    def complete():
        page.clock.run_for(700)
        value = request('network/ha-lan')['body']
        HomeLanStatus.model_validate(value)
        return value

    initial = request('network/ha-lan')['body']
    change = {'action': 'configure_lan', 'interface_id': None}
    assert submit(change, None)['status'] == 400
    assert submit(change, 'demo-running') == {'status': 409, 'body': {'detail': 'ha_lan_shutdown_required'}}
    assert submit({'action': 'configure_lan'}, 'demo-missing')['status'] == 422
    assert submit({'action': 'configure_lan', 'interface_id': 'invalid'}, 'demo-invalid')['status'] == 422
    assert request('network/ha-lan')['body'] == initial
    shutdown = submit({'action': 'shutdown'}, 'demo-shutdown')
    assert shutdown['status'] == 202 and shutdown['body']['state'] == 'running'
    assert submit({'action': 'start'}, 'demo-concurrent')['status'] == 409
    assert complete()['vm_state'] == 'shut off'
    assert submit({'action': 'configure_lan', 'interface_id': 'b' * 64}, 'demo-unknown') == {
        'status': 409, 'body': {'detail': 'ha_lan_selected_interface_unavailable'}}
    first = submit(change, 'demo-disable')
    assert first['status'] == 202 and submit(change, 'demo-disable') == first
    disabled = complete()
    assert disabled['mode'] == 'nat' and disabled['home']['internal_url'] is None
    configure = {'action': 'configure_lan', 'interface_id': initial['selected_interface_id']}
    assert submit(configure, 'demo-disable')['status'] == 409
    assert submit(configure, 'demo-enable')['status'] == 202
    wired = complete()
    assert wired['mode'] == 'wired' and wired['home']['state'] == 'unavailable'
    assert wired['home']['ipv4'] == [] and wired['home']['internal_url'] is None
    assert submit({'action': 'start'}, 'demo-start')['status'] == 202
    assert complete()['home']['state'] == 'ready'
    # Replaying completed work returns its original job, without undoing later state.
    repeated = submit(change, 'demo-disable')
    assert repeated['body']['id'] == first['body']['id'] and repeated['body']['state'] == 'succeeded'
    assert request('network/ha-lan')['body']['mode'] == 'wired'
    jobs = request('host/status')['body']['jobs']
    assert [job['id'] for job in jobs].count('demo-disable') == 1
    clean(page, errors, external)
    page.close()


def test_bridge_and_entry_guard(browser, origin):
    page = browser.new_page()
    errors, external = watch(page, origin)
    for fragment in ['', '#entry', '#desktop-login=fictional']:
        page.goto(f'{origin}/demo/{fragment}')
        page.reload()
        expect(page).to_have_url(f'{origin}/demo/#/lovelace/home')
        expect(page.get_by_role('heading', name='我的家', exact=True)).to_be_visible()
        assert page.locator('iframe').count() == 0
    page.goto(f'{origin}/demo/?surface=bridge#gateway')
    expect(page).to_have_url(f'{origin}/demo/#/agentic-home')
    ui = product_frame(page, 'bridge')
    expect(ui.locator('.bridge-shell')).to_be_visible()
    expect(ui.locator('#gateway-message')).to_be_visible()
    expect(ui.get_by_text('示範家庭的玄關感應器可以在天黑後打開客廳燈嗎？')).to_be_visible()
    # Authorization cannot reach bootstrap's location.assign.
    result = ui.evaluate("async()=>{const r=await fetch('api/host/authorize',{method:'POST',body:'{}'});return {status:r.status,body:await r.json()}}")
    assert result == {'status': 409, 'body': {'detail': '示範中無法進行這個操作。'}}
    ui.get_by_role('link', name='回到家庭', exact=True).click()
    expect(page).to_have_url(f'{origin}/demo/#/lovelace/home')
    expect(page.get_by_role('heading', name='我的家', exact=True)).to_be_visible()
    clean(page, errors, external)
    page.close()


def test_without_scripts_and_api_fences(browser, origin):
    context = browser.new_context(java_script_enabled=False, viewport={'width': 390, 'height': 844})
    page = context.new_page()
    page.goto(origin)
    expect(page.get_by_role('link', name='安裝', exact=True)).to_be_visible()
    expect(page.locator('[data-panel=home]')).to_be_visible()
    context.close()
    page = browser.new_page()
    errors, external = watch(page, origin)
    page.goto(f'{origin}/demo/#gateway')
    ui = product_frame(page)
    ui.locator('#gateway-message').fill('你好')
    expect(ui.get_by_role('button', name='送出訊息')).to_be_enabled()
    ui.get_by_role('button', name='送出訊息').click()
    expect(ui.get_by_text('可以先看看設備與規則。想試試提案，可以說「有人經過玄關時打開燈」。')).to_be_visible()
    clean(page, errors, external)
    assert ui.evaluate("async()=>{try{await fetch('https://example.invalid/demo');return false}catch{return true}}")
    assert not external
    result = ui.evaluate("async()=>{const r=await fetch('api/unknown-demo-read');return {status:r.status,body:await r.json()}}")
    assert result == {'status': 404, 'body': {'detail': '這份資料已過期或不存在。'}}
    assert page.evaluate('window.__agenticDemo.unhandled') == ['unknown-demo-read']
    page.get_by_role('button', name='重設', exact=True).click()
    expect(page).to_have_url(f'{origin}/demo/#/lovelace/home')
    expect(page.get_by_role('heading', name='我的家', exact=True)).to_be_visible()
    page.get_by_role('navigation', name='示範入口').get_by_role('link', name='管理與救援').click()
    ui = product_frame(page)
    expect(ui.get_by_text('示範家庭的玄關感應器可以在天黑後打開客廳燈嗎？')).to_be_visible()
    page.set_viewport_size({'width': 390, 'height': 844})
    expect(ui.get_by_role('heading', name='智能管家', exact=True)).to_be_in_viewport()
    expect(ui.locator('#gateway-message')).to_be_in_viewport()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    clean(page, errors, external)
    page.close()


def test_isolation_and_manifest():
    for path in (ROOT / 'web/src').rglob('*'):
        if path.is_file():
            assert 'demo/' not in path.read_text()
            assert 'AGENTIC_DEMO_BUILD' not in path.read_text()
    # A bundler renames the constant, so look for its value too.
    marker = re.search(r"AGENTIC_DEMO_BUILD='([^']+)'", (ROOT / 'web/demo/fake-api.ts').read_text()).group(1).encode()
    for directory in ['controller/static', 'bridge/static']:
        for path in (ROOT / directory).rglob('*'):
            if path.is_file():
                assert b'AGENTIC_DEMO_BUILD' not in path.read_bytes()
                assert marker not in path.read_bytes()
    folder = ROOT / 'site/assets/screens'
    manifest = json.loads((folder / 'manifest.json').read_text())
    assert manifest['synthetic_data'] is True
    assert manifest['native_or_live_acceptance'] is False
    assert manifest['dom_content_modified'] is False
    assert manifest['fixture_clock'] == site.CLOCK
    assert len(manifest['source_commit']) == len(manifest['source_tree']) == 40
    assert manifest['ha_interface']['kind'] == 'original-interactive-simulation'
    assert manifest['ha_interface']['official_frontend_runtime'] is False
    assert {item['file'] for item in manifest['files']} == {
        f'{name}.png' for name, *_ in site.SCREENS
    } | {'../og.png'}
    source_files = {
        'scripts/site_screenshots.py', 'web/preview/fixtures.json',
        'site/index.html', 'site/styles.css', 'site/app.js', 'site/lang.js',
    } | {str(path.relative_to(ROOT)) for path in (ROOT / 'web/demo').iterdir() if path.is_file()}
    assert {item['file'] for item in manifest['source_files']} == source_files
    assert len(manifest['source_files']) == len(source_files)
    for item in manifest['source_files']:
        path = (ROOT / item['file']).resolve()
        assert path.is_relative_to(ROOT)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item['sha256']
    for item in manifest['files']:
        path = (folder / item['file']).resolve()
        assert path.is_relative_to(ROOT / 'site/assets')
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item['sha256']
    assert sum(p.stat().st_size for p in (ROOT / 'site/assets').rglob('*') if p.is_file()) < 4_000_000
