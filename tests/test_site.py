"""Public project page and unchanged React UI, using fictional browser data only."""
import hashlib
import importlib.util
import json
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
    page.wait_for_function('Array.from(document.images).every(i=>i.complete && i.naturalWidth>0)')
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


def nav(page, label):
    page.get_by_role('navigation', name='主選單').get_by_role('button', name=label, exact=True).click()
    expect(page.get_by_role('heading', name=label, exact=True).first).to_be_visible()


def approve(page):
    page.get_by_role('button', name='批准並執行', exact=True).click()
    expect(page.locator('.task-message')).to_contain_text('備份')
    expect(page.locator('.task-detail .status')).to_have_text('已完成', timeout=12000)
    expect(page.get_by_role('heading', name='觀測結果')).to_be_visible()


def test_household_flow(browser, origin):
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    errors, external = watch(page, origin)
    page.goto(f'{origin}/demo/')
    expect(page).to_have_url(f'{origin}/demo/#gateway')
    expect(page.get_by_text('示範模式 · 介面是真的，資料是虛構的')).to_be_visible()
    page.locator('#gateway-message').fill('有人經過玄關時打開燈')
    expect(page.get_by_role('button', name='送出訊息')).to_be_enabled()
    page.locator('#gateway-message').press('Enter')
    expect(page.get_by_text('提案已準備好。先看精確內容，再決定是否批准。')).to_be_visible()
    page.get_by_role('link', name='查看提案 →').last.click()
    page.get_by_text('查看設定差異', exact=True).click()
    expect(page.locator('.proposal-diff')).to_contain_text('light.demo_living_room')
    approve(page)
    nav(page, '自動化')
    expect(page.locator('.automation-row').filter(has_text='玄關有人時開啟客廳燈')).to_be_visible()
    nav(page, '紀錄')
    page.locator('.history-table > button').filter(has_text='玄關有人時開啟客廳燈').click()
    expect(page.get_by_role('heading', name='觀測結果')).to_be_visible()
    nav(page, '設備')
    page.get_by_role('checkbox', name='分享 light.demo_living_room').uncheck()
    page.get_by_role('button', name='儲存修改', exact=True).click()
    expect(page.get_by_text('已儲存。受影響的提案需要重新產生。')).to_be_visible()
    nav(page, '設定')
    nav(page, '設備')
    expect(page.get_by_role('checkbox', name='分享 light.demo_living_room')).not_to_be_checked()
    nav(page, '自動化')
    row = page.locator('.automation-row').filter(has_text='玄關有人時開啟客廳燈')
    row.get_by_role('button', name='預覽停用').click()
    approve(page)
    nav(page, '自動化')
    row = page.locator('.automation-row').filter(has_text='玄關有人時開啟客廳燈')
    expect(row).to_contain_text('已停用')
    row.get_by_role('button', name='預覽啟用').click()
    approve(page)
    nav(page, '自動化')
    row = page.locator('.automation-row').filter(has_text='玄關有人時開啟客廳燈')
    expect(row).to_contain_text('已啟用')
    row.locator('summary').click()
    row.get_by_role('button', name='預覽回復').click()
    approve(page)
    clean(page, errors, external)
    page.close()


def test_workers_and_host(browser, origin):
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    errors, external = watch(page, origin)
    page.goto(f'{origin}/demo/#workers')
    for title in ['發現', '測試', '變更']:
        expect(page.get_by_role('heading', name=title, exact=True)).to_be_visible()
    page.get_by_role('button', name='查看內容', exact=True).click()
    expect(page.locator('.diff-add').first).to_contain_text('玄關感應器')
    page.get_by_label('要調查或改善什麼？').fill('調查示範燈的說明')
    page.get_by_role('button', name='開始調查', exact=True).click()
    expect(page.locator('.worker-result-head')).to_contain_text('調查示範燈的說明')
    expect(page.locator('.worker-result-head .status')).to_have_text('已完成', timeout=12000)
    expect(page.get_by_role('heading', name='發現', exact=True)).to_be_visible()
    nav(page, '設定')
    page.locator('.settings-hub a[href="#host"]').click()
    expect(page.get_by_role('heading', name='示範主機', exact=True)).to_be_visible()
    page.get_by_role('button', name='檢查連線', exact=True).click()
    expect(page.get_by_text('主機與家庭都有回應。', exact=True)).to_be_visible()
    page.locator('summary').filter(has_text='版本 0.9.0').click()
    expect(page.get_by_text('先檢查簽署的版本說明，確認後才更新。')).to_be_visible()
    page.locator('summary').filter(has_text='快照 ·').click()
    expect(page.get_by_role('button', name='選擇還原')).to_be_visible()
    current = page.url
    page.get_by_role('link', name='開啟家庭 ↗', exact=True).click()
    assert page.url == current
    expect(page.get_by_text('示範中不會開啟 Home Assistant。', exact=True)).to_be_visible()
    page.evaluate("() => {const a=document.createElement('a');a.href='downloads/demo.zip';a.textContent='download guard';document.body.append(a);a.click();a.remove()}")
    expect(page.get_by_text('示範中無法下載。', exact=True)).to_be_visible()
    clean(page, errors, external)
    page.close()


def test_bridge_and_entry_guard(browser, origin):
    page = browser.new_page()
    errors, external = watch(page, origin)
    for fragment in ['', '#entry', '#desktop-login=fictional']:
        page.goto(f'{origin}/demo/{fragment}')
        page.reload()
        expect(page).to_have_url(f'{origin}/demo/#gateway')
        expect(page.locator('#gateway-message')).to_be_visible()
    page.goto(f'{origin}/demo/?surface=bridge#gateway')
    expect(page.locator('.bridge-shell')).to_be_visible()
    expect(page.locator('#gateway-message')).to_be_visible()
    expect(page.get_by_text('示範家庭的玄關感應器可以在天黑後打開客廳燈嗎？')).to_be_visible()
    current = page.url
    page.get_by_role('link', name='回到家庭', exact=True).click()
    assert page.url == current
    expect(page.get_by_text('示範中不會開啟 Home Assistant。', exact=True)).to_be_visible()
    # Authorization cannot reach bootstrap's location.assign; unknown GETs are explicit.
    result = page.evaluate("async()=>{const r=await fetch('api/host/authorize',{method:'POST',body:'{}'});return {status:r.status,body:await r.json()}}")
    assert result == {'status': 409, 'body': {'detail': '示範中無法進行這個操作。'}}
    clean(page, errors, external)
    page.close()


def test_without_scripts_and_api_fences(browser, origin):
    context = browser.new_context(java_script_enabled=False, viewport={'width': 390, 'height': 844})
    page = context.new_page()
    page.goto(origin)
    expect(page.get_by_role('link', name='安裝', exact=True)).to_be_visible()
    expect(page.locator('[data-panel=gateway]')).to_be_visible()
    context.close()
    page = browser.new_page()
    errors, external = watch(page, origin)
    page.goto(f'{origin}/demo/#gateway')
    page.locator('#gateway-message').fill('你好')
    expect(page.get_by_role('button', name='送出訊息')).to_be_enabled()
    page.get_by_role('button', name='送出訊息').click()
    expect(page.get_by_text('可以先看看設備與規則。想試試提案，可以說「有人經過玄關時打開燈」。')).to_be_visible()
    clean(page, errors, external)
    assert page.evaluate("async()=>{try{await fetch('https://example.invalid/demo');return false}catch{return true}}")
    assert not external
    result = page.evaluate("async()=>{const r=await fetch('api/unknown-demo-read');return {status:r.status,body:await r.json()}}")
    assert result == {'status': 404, 'body': {'detail': '這份資料已過期或不存在。'}}
    assert page.evaluate('window.__agenticDemo.unhandled') == ['unknown-demo-read']
    page.get_by_role('button', name='重設', exact=True).click()
    expect(page).to_have_url(f'{origin}/demo/#gateway')
    expect(page.get_by_text('示範家庭的玄關感應器可以在天黑後打開客廳燈嗎？')).to_be_visible()
    page.set_viewport_size({'width': 390, 'height': 844})
    expect(page.get_by_role('heading', name='智能管家', exact=True)).to_be_in_viewport()
    expect(page.locator('#gateway-message')).to_be_in_viewport()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    clean(page, errors, external)
    page.close()


def test_isolation_and_manifest():
    for path in (ROOT / 'web/src').rglob('*'):
        if path.is_file():
            assert 'demo/' not in path.read_text()
            assert 'AGENTIC_DEMO_BUILD' not in path.read_text()
    for directory in ['controller/static', 'bridge/static']:
        for path in (ROOT / directory).rglob('*'):
            if path.is_file():
                assert b'AGENTIC_DEMO_BUILD' not in path.read_bytes()
    folder = ROOT / 'site/assets/screens'
    manifest = json.loads((folder / 'manifest.json').read_text())
    assert manifest['synthetic_data'] is True
    assert manifest['native_or_live_acceptance'] is False
    assert manifest['dom_content_modified'] is False
    assert manifest['fixture_clock'] == site.CLOCK
    assert len(manifest['source_commit']) == len(manifest['source_tree']) == 40
    for item in manifest['files']:
        path = (folder / item['file']).resolve()
        assert path.is_relative_to(ROOT / 'site/assets')
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item['sha256']
    assert sum(p.stat().st_size for p in (ROOT / 'site/assets').rglob('*') if p.is_file()) < 4_000_000
