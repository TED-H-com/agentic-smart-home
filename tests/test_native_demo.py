"""The HA simulation and real product frames act on one synthetic household."""
import hashlib
import json

import pytest
from playwright.sync_api import expect
from test_site import approve, clean, nav, product_frame, watch
from test_site import browser as browser
from test_site import origin as origin


def native_nav(page, label):
    sidebar = page.locator('.ha-demo-sidebar')
    if not sidebar.is_visible():
        page.get_by_role('button', name='開啟家庭選單', exact=True).click()
    sidebar.get_by_role('button', name=label, exact=True).click()


def outer_entry(page):
    page.get_by_role('navigation', name='示範入口').get_by_role('link', name='管理與救援').click()
    return product_frame(page)


def home_entry(page):
    page.get_by_role('navigation', name='示範入口').get_by_role('link', name='家庭', exact=True).click()
    expect(page.get_by_role('heading', name='我的家', exact=True)).to_be_visible()


def read_api(frame, path):
    return frame.evaluate("async path=>{const r=await fetch('api/'+path);if(!r.ok)throw Error(r.status);return r.json()}", path)


def inventory_row(ui, entity):
    return ui.locator('.entity-row').filter(has=ui.get_by_role('checkbox', name=f'分享 {entity}', exact=True))


def test_native_controls_are_visible_in_product_inventory(browser, origin):
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    errors, external = watch(page, origin)
    page.goto(f'{origin}/demo/')
    expect(page).to_have_url(f'{origin}/demo/#/lovelace/home')
    expect(page.get_by_text('Home Assistant 介面模擬', exact=True)).to_be_visible()
    light = page.get_by_role('button', name='客廳燈電源', exact=True)
    expect(light).to_have_attribute('aria-pressed', 'false')
    light.click()
    expect(light).to_have_attribute('aria-pressed', 'true')
    brightness = page.get_by_role('slider', name='客廳燈亮度', exact=True)
    brightness.focus()
    brightness.press('Home')
    expect(brightness).to_have_value('0')
    expect(light).to_have_attribute('aria-pressed', 'false')
    light.click()
    expect(light).to_have_attribute('aria-pressed', 'true')
    expect(brightness).to_have_value('60')
    brightness.focus()
    brightness.press('Home')
    for _ in range(7):
        brightness.press('ArrowRight')
    expect(brightness).to_have_value('7')
    page.get_by_role('button', name='臥室冷氣電源', exact=True).click()
    page.get_by_role('button', name='臥室冷氣提高溫度', exact=True).click()
    expect(page.get_by_role('button', name='查看臥室冷氣', exact=True)).to_contain_text('25 °C')
    ui = outer_entry(page)
    nav(ui, '設備')
    expect(inventory_row(ui, 'light.demo_living_room').locator('.entity-state')).to_have_text('開啟')
    state = {item['entity_id']: item for item in read_api(ui, 'inventory')['entities']}
    assert state['light.demo_living_room']['state'] == 'on'
    assert state['light.demo_living_room']['brightness'] == 7
    assert state['climate.demo_bedroom']['state'] == 'cool'
    assert state['climate.demo_bedroom']['temperature'] == 25
    home_entry(page)
    page.get_by_role('button', name='客廳燈電源', exact=True).click()
    outer_entry(page)
    nav(ui, '設備')
    expect(inventory_row(ui, 'light.demo_living_room').locator('.entity-state')).to_have_text('關閉')
    clean(page, errors, external)
    page.close()


def test_discovery_setup_updates_all_views_without_granting_permission(browser, origin):
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    errors, external = watch(page, origin)
    page.goto(f'{origin}/demo/#inventory')
    ui = product_frame(page)
    expect(ui.get_by_role('checkbox')).to_have_count(3)
    before = read_api(ui, 'discovery')
    before_policy = read_api(ui, 'inventory')['policy']
    ui.get_by_role('link', name='開啟家庭設定 ↗', exact=True).click()
    expect(page).to_have_url(f'{origin}/demo/#/config/integrations')
    setup = page.get_by_role('button', name='設定示範待設定橋接器', exact=True)
    setup.click()
    dialog = page.get_by_role('dialog', name='設定示範設備', exact=True)
    expect(dialog).to_contain_text('不會登入品牌帳號或連接實際設備。')
    dialog.get_by_role('button', name='取消', exact=True).click()
    assert read_api(ui, 'discovery') == before
    setup.click()
    dialog.get_by_role('button', name='完成示範設定', exact=True).click()
    expect(page.get_by_text('目前沒有等待設定的示範設備。', exact=True)).to_be_visible()
    integration = page.locator('.ha-demo-integration-card').filter(has=page.get_by_role('heading', name='示範橋接器', exact=True))
    expect(integration).to_contain_text('2 個設備')
    expect(integration).to_contain_text('已載入')
    native_nav(page, '概覽')
    expect(page.get_by_role('button', name='查看書桌燈', exact=True)).to_be_visible()
    page.get_by_role('button', name='新增與管理設備', exact=True).click()
    page.get_by_role('button', name='管家設備授權', exact=True).click()
    expect(page).to_have_url(f'{origin}/demo/#inventory')
    # The real inventory keeps its explicit refresh behavior when already mounted.
    ui.get_by_role('button', name='重新盤點', exact=True).click()
    expect(ui.get_by_role('checkbox')).to_have_count(4)
    expect(ui.get_by_role('checkbox', name='分享 light.demo_desk')).not_to_be_checked()
    expect(ui.get_by_label('light.demo_desk 操作權限', exact=True)).to_be_disabled()
    expect(ui.get_by_label('light.demo_desk 操作權限', exact=True)).to_have_value('none')
    expect(ui.locator('.discovery')).to_contain_text('目前沒有待設定的探索結果')
    inventory = read_api(ui, 'inventory')
    assert inventory['policy'] == before_policy
    assert sum(item['entity_id'] == 'light.demo_desk' for item in inventory['entities']) == 1
    discovery = read_api(ui, 'discovery')
    device = next(item for item in discovery['devices'] if item['id'] == 'demo-desk-device')
    assert device['via_device_id'] == 'demo-bridge'
    assert device['integration_ids'] == ['demo-bridge-integration']
    assert next(item for item in discovery['devices'] if item['id'] == 'demo-bridge')['entity_count'] == 0
    assert discovery['candidates'] == []
    digest = discovery.pop('content_digest')
    expected = hashlib.sha256(json.dumps(discovery, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    assert digest == expected and digest != before['content_digest']
    clean(page, errors, external)
    page.close()


def test_discovery_context_is_reviewed_before_send_and_does_not_create_a_control_proposal(browser, origin):
    page = browser.new_page(viewport={'width': 390, 'height': 844})
    errors, external = watch(page, origin)
    page.goto(f'{origin}/demo/#/agentic-home')
    ui = product_frame(page, 'bridge')
    ui.get_by_role('button', name='新對話', exact=True).click()
    ui.locator('#gateway-message').fill('請協助確認接入方式。')
    before_messages = read_api(ui, 'gateway/messages')
    before_tasks = read_api(ui, 'tasks')
    before_policy = read_api(ui, 'inventory')['policy']
    candidate = read_api(ui, 'discovery')['candidates'][0]
    ui.get_by_role('button', name='分享探索結果', exact=True).click()
    dialog = ui.get_by_role('dialog', name='分享探索結果', exact=True)
    expect(dialog.get_by_role('checkbox')).not_to_be_checked()
    dialog.get_by_role('checkbox', name=f'選取 {candidate["title"]}', exact=True).check()
    preview = dialog.get_by_role('textbox', name='對話預覽', exact=True).input_value()
    assert preview.startswith('請協助確認接入方式。\n\n【設備探索快照】')
    assert candidate['title'] in preview and len(preview) <= 5000
    dialog.get_by_role('button', name='帶入對話', exact=True).click()
    expect(ui.locator('#gateway-message')).to_have_value(preview)
    assert read_api(ui, 'gateway/messages') == before_messages
    assert read_api(ui, 'tasks') == before_tasks
    ui.get_by_role('button', name='送出訊息', exact=True).click()
    expect(ui.locator('.assistant-answer').filter(has_text='這是固定示範回覆')).to_be_visible()
    sent = read_api(ui, 'gateway/messages')['messages'][0]
    assert sent['message'] == preview
    assert read_api(ui, f"hermes/cases/{sent['case_id']}")['jobs'] == []
    expect(ui.get_by_role('button', name='查看成果', exact=True)).to_have_count(0)
    assert read_api(ui, 'tasks') == before_tasks
    assert read_api(ui, 'inventory')['policy'] == before_policy
    clean(page, errors, external)
    page.close()


def test_bridge_approval_publishes_native_automation_and_shared_history(browser, origin):
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    errors, external = watch(page, origin)
    page.goto(f'{origin}/demo/#/agentic-home')
    ui = product_frame(page, 'bridge')
    ui.get_by_role('button', name='新對話', exact=True).click()
    ui.locator('#gateway-message').fill('回家時開啟臥室冷氣')
    ui.get_by_role('button', name='送出訊息', exact=True).click()
    proposal = ui.get_by_role('link', name='查看提案 →', exact=True)
    task_route = proposal.get_attribute('href')
    assert task_route.startswith('#home/')
    proposal.click()
    expect(page).to_have_url(f'{origin}/demo/#/agentic-home/{task_route[1:]}')
    assert not any(rule['intent']['title'] == '回家時開啟臥室冷氣' for rule in read_api(ui, 'automations')['automations'])
    approve(ui)
    native_nav(page, '自動化')
    row = page.locator('.ha-demo-automations li').filter(has_text='回家時開啟臥室冷氣')
    expect(row).to_be_visible()
    expect(row).to_contain_text('開啟')
    outer = outer_entry(page)
    nav(outer, '紀錄')
    outer.locator('.history-table > button').filter(has_text='回家時開啟臥室冷氣').click()
    expect(outer.locator('.task-detail .status')).to_have_text('等待真實觸發')
    expect(outer.get_by_role('heading', name='觀測結果')).to_have_count(0)
    clean(page, errors, external)
    page.close()


def test_drafts_and_frame_history_survive_surface_switches(browser, origin):
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    errors, external = watch(page, origin)
    page.goto(f'{origin}/demo/#gateway')
    outer = product_frame(page)
    outer.locator('#gateway-message').fill('尚未送出的管理需求')
    home_entry(page)
    native_nav(page, '智能管家')
    bridge = product_frame(page, 'bridge')
    bridge.locator('#gateway-message').fill('尚未送出的家庭需求')
    native_nav(page, '概覽')
    page.go_back()
    expect(page).to_have_url(f'{origin}/demo/#/agentic-home')
    expect(bridge.locator('#gateway-message')).to_have_value('尚未送出的家庭需求')
    outer_entry(page)
    expect(outer.locator('#gateway-message')).to_have_value('尚未送出的管理需求')
    assert len(page.frames) == 3
    # Inventory's unsaved permission choice also survives leaving the outer surface.
    nav(outer, '設備')
    outer.get_by_role('checkbox', name='分享 light.demo_living_room').uncheck()
    home_entry(page)
    page.go_back()
    expect(page).to_have_url(f'{origin}/demo/#inventory')
    expect(outer.get_by_role('checkbox', name='分享 light.demo_living_room')).not_to_be_checked()
    expect(outer.get_by_text('設備授權有未儲存的修改', exact=True)).to_be_visible()
    outer.get_by_role('button', name='儲存修改', exact=True).click()
    expect(outer.get_by_text('已儲存。受影響的提案需要重新產生。', exact=True)).to_be_visible()
    home_entry(page)
    native_nav(page, '智能管家')
    expect(bridge.locator('#gateway-message')).to_have_value('尚未送出的家庭需求')
    # Child hash navigation and browser Back must move the parent URL together.
    bridge.locator('#gateway-message').fill('出門時關燈')
    bridge.get_by_role('button', name='送出訊息', exact=True).click()
    bridge.get_by_role('link', name='查看提案 →', exact=True).last.click()
    expect(bridge.get_by_role('button', name='批准並執行', exact=True)).to_be_visible()
    assert '/agentic-home/home/' in page.url
    page.go_back()
    expect(page).to_have_url(f'{origin}/demo/#/agentic-home')
    expect(bridge.locator('#gateway-message')).to_be_visible()
    page.go_forward()
    expect(bridge.get_by_role('button', name='批准並執行', exact=True)).to_be_visible()
    clean(page, errors, external)
    page.close()


def test_real_product_native_links_map_to_simulated_pages(browser, origin):
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    errors, external = watch(page, origin)
    page.goto(f'{origin}/demo/#automations')
    ui = product_frame(page)
    ui.get_by_role('link', name='開啟家庭自動化 ↗', exact=True).click()
    expect(page.get_by_role('heading', name='家庭規則', exact=True)).to_be_visible()
    assert '#/config/automation' in page.url
    page.go_back()
    expect(page).to_have_url(f'{origin}/demo/#automations')
    nav(ui, '設備')
    ui.get_by_role('link', name='開啟家庭設定 ↗', exact=True).click(button='middle')
    expect(page).to_have_url(f'{origin}/demo/#/config/integrations')
    expect(page.get_by_role('heading', name='裝置與服務', exact=True)).to_be_visible()
    assert len(page.context.pages) == 1
    clean(page, errors, external)
    page.close()


def test_reset_clears_device_changes_and_both_frame_drafts(browser, origin):
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    errors, external = watch(page, origin)
    page.goto(f'{origin}/demo/#/config/integrations')
    page.get_by_role('button', name='設定示範待設定橋接器', exact=True).click()
    page.get_by_role('button', name='完成示範設定', exact=True).click()
    native_nav(page, '概覽')
    page.get_by_role('button', name='書桌燈電源', exact=True).click()
    expect(page.get_by_role('button', name='書桌燈電源', exact=True)).to_have_attribute('aria-pressed', 'true')
    native_nav(page, '智能管家')
    bridge = product_frame(page, 'bridge')
    bridge.locator('#gateway-message').fill('重設前的家庭草稿')
    outer = outer_entry(page)
    outer.locator('#gateway-message').fill('重設前的管理草稿')
    page.get_by_role('button', name='重設', exact=True).click()
    expect(page).to_have_url(f'{origin}/demo/#/lovelace/home')
    expect(page.get_by_role('button', name='查看客廳燈', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='書桌燈電源', exact=True)).to_have_count(0)
    assert page.locator('iframe').count() == 0
    native_nav(page, '智能管家')
    expect(product_frame(page, 'bridge').locator('#gateway-message')).to_have_value('')
    expect(outer_entry(page).locator('#gateway-message')).to_have_value('')
    clean(page, errors, external)
    page.close()


@pytest.mark.parametrize('target', [
    'http://192.168.1.1/', 'https://router.local/', 'http://localhost:8123/auth/authorize',
    'http://user:secret@localhost:8123/config/integrations',
    'http://localhost:8123/config/integrations?token=fictional', 'http://[::1]:8123/',
])
def test_private_links_never_leave_the_demo(browser, origin, target):
    page = browser.new_page()
    errors, external = watch(page, origin)
    page.goto(f'{origin}/demo/#gateway')
    ui = product_frame(page)
    ui.locator('#gateway-message').wait_for()
    current = page.url
    ui.evaluate("href=>{const a=document.createElement('a');a.href=href;a.target='_top';a.textContent='private link';document.body.append(a)}", target)
    ui.get_by_role('link', name='private link', exact=True).click()
    expect(ui.get_by_role('status').filter(has_text='此連結未包含在示範中。')).to_be_visible()
    assert page.url == current and len(page.context.pages) == 1
    clean(page, errors, external)
    page.close()


@pytest.mark.parametrize(('surface', 'route', 'destination'), [
    ('outer', '#inventory', '#inventory'),
    ('bridge', '#gateway', '#/agentic-home'),
])
def test_standalone_frame_urls_restore_the_labeled_shell(browser, origin, surface, route, destination):
    page = browser.new_page(viewport={'width': 390, 'height': 844})
    errors, external = watch(page, origin)
    page.goto(f'{origin}/demo/?frame={surface}{route}')
    expect(page).to_have_url(f'{origin}/demo/{destination}')
    expect(page.get_by_text('互動示範 · 虛構家庭', exact=True)).to_be_in_viewport()
    ui = product_frame(page, surface)
    expect(ui.locator('#gateway-message') if surface == 'bridge' else ui.locator('.inventory-table')).to_be_visible()
    if surface == 'outer':
        home_entry(page)
    expect(page.get_by_text('HA 介面模擬', exact=True)).to_be_in_viewport()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    clean(page, errors, external)
    page.close()


@pytest.mark.parametrize('width', [390, 768, 1440])
def test_native_layout_keyboard_dialogs_and_embedded_assistant(browser, origin, width):
    page = browser.new_page(viewport={'width': width, 'height': 900 if width > 390 else 844})
    errors, external = watch(page, origin)
    page.goto(f'{origin}/demo/')
    expect(page.get_by_role('button', name='客廳燈電源', exact=True)).to_be_in_viewport()
    expect(page.get_by_text('HA 介面模擬', exact=True)).to_be_in_viewport()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert page.locator('.ha-demo-content').evaluate('el=>el.scrollWidth<=el.clientWidth')
    if width <= 900:
        menu = page.get_by_role('button', name='開啟家庭選單', exact=True)
        menu.focus()
        menu.press('Enter')
        drawer = page.get_by_role('dialog', name='家庭選單', exact=True)
        expect(drawer).to_be_visible()
        close = drawer.get_by_role('button', name='關閉家庭選單', exact=True)
        expect(close).to_be_focused()
        page.keyboard.press('Shift+Tab')
        expect(drawer.get_by_role('button', name='設定', exact=True)).to_be_focused()
        page.keyboard.press('Tab')
        expect(close).to_be_focused()
        page.keyboard.press('Escape')
        expect(drawer).to_have_count(0)
        expect(menu).to_be_focused()
    details = page.get_by_role('button', name='查看客廳燈', exact=True)
    details.focus()
    details.press('Enter')
    dialog = page.get_by_role('dialog', name='客廳燈', exact=True)
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role('button', name='關閉對話框', exact=True)).to_be_focused()
    page.keyboard.press('Shift+Tab')
    slider = dialog.get_by_role('slider', name='客廳燈詳細亮度', exact=True)
    expect(slider).to_be_focused()
    slider.press('End')
    expect(slider).to_have_value('100')
    expect(dialog).to_contain_text('開啟 · 100%')
    page.keyboard.press('Escape')
    expect(dialog).to_have_count(0)
    expect(details).to_be_focused()
    native_nav(page, '設定')
    page.get_by_role('button', name='裝置與服務 管理整合與已發現的設備', exact=True).click()
    expect(page.get_by_role('button', name='設定示範待設定橋接器', exact=True)).to_be_in_viewport()
    assert page.locator('.ha-demo-content').evaluate('el=>el.scrollWidth<=el.clientWidth')
    native_nav(page, '智能管家')
    bridge = product_frame(page, 'bridge')
    expect(bridge.locator('#gateway-message')).to_be_visible()
    expect(bridge.get_by_role('heading', name='智能管家', exact=True)).to_be_in_viewport()
    assert bridge.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    clean(page, errors, external)
    page.close()


def test_channel_settings_route_survives_reload_and_back(browser, origin):
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    errors, external = watch(page, origin)
    page.goto(f'{origin}/demo/#gateway')
    ui = product_frame(page)
    ui.get_by_role('link', name='通訊設定', exact=True).click()
    expect(page).to_have_url(f'{origin}/demo/#gateway/channels')
    expect(ui.get_by_role('heading', name='通訊設定', exact=True)).to_be_visible()
    page.reload()
    ui = product_frame(page)
    expect(ui.get_by_role('heading', name='通訊設定', exact=True)).to_be_visible()
    ui.get_by_role('button', name='回到對話', exact=True).click()
    expect(page).to_have_url(f'{origin}/demo/#gateway')
    expect(ui.get_by_role('heading', name='通訊設定', exact=True)).to_have_count(0)
    page.go_back()
    expect(page).to_have_url(f'{origin}/demo/#gateway/channels')
    expect(ui.get_by_role('heading', name='通訊設定', exact=True)).to_be_visible()
    clean(page, errors, external)
    page.close()


def test_bridge_new_conversation_clears_parent_deep_link(browser, origin):
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    errors, external = watch(page, origin)
    page.goto(f"{origin}/demo/#/agentic-home/gateway/{'3' * 64}")
    ui = product_frame(page, 'bridge')
    expect(ui.get_by_text('示範家庭的玄關感應器可以在天黑後打開客廳燈嗎？', exact=True)).to_be_visible()
    ui.get_by_role('button', name='新對話', exact=True).click()
    expect(page).to_have_url(f'{origin}/demo/#/agentic-home')
    expect(ui.locator('#gateway-message')).to_have_value('')
    expect(ui.locator('.assistant-question')).to_have_count(0)
    page.reload()
    ui = product_frame(page, 'bridge')
    expect(ui.locator('#gateway-message')).to_be_visible()
    expect(ui.locator('.assistant-question')).to_have_count(0)
    expect(page).to_have_url(f'{origin}/demo/#/agentic-home')
    clean(page, errors, external)
    page.close()


@pytest.mark.parametrize('query', ['frame=bridge', 'surface=bridge'])
def test_legacy_bridge_links_preserve_selected_conversation(browser, origin, query):
    page = browser.new_page(viewport={'width': 390, 'height': 844})
    errors, external = watch(page, origin)
    message_id = '3' * 64
    page.goto(f'{origin}/demo/?{query}#gateway/{message_id}')
    expect(page).to_have_url(f'{origin}/demo/#/agentic-home/gateway/{message_id}')
    ui = product_frame(page, 'bridge')
    expect(ui.get_by_text('示範家庭的玄關感應器可以在天黑後打開客廳燈嗎？', exact=True)).to_be_visible()
    expect(page.get_by_text('HA 介面模擬', exact=True)).to_be_in_viewport()
    assert ui.url.endswith(f'#gateway/{message_id}')
    clean(page, errors, external)
    page.close()
