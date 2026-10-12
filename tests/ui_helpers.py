"""Navigate through the same visible desktop/mobile controls as the owner."""
from playwright.sync_api import expect


def click_navigation(page, name):
    nav = page.get_by_role('navigation', name='主選單', include_hidden=True)
    # Legacy Ingress keeps its existing names. Host settings are reached through
    # the same visible hub links the owner uses, never by changing the URL.
    host_mode = nav.get_by_role('button', name='智能管家', include_hidden=True).count() > 0
    aliases = {'設備與權限': '設備', '家庭自動化': '自動化', '任務紀錄': '紀錄',
               '連線與設定': '設定', '對話與通訊': '智能管家'}
    advanced = {'Agent 環境': '助手帳號', 'Linux 主機': '主機維護', '開始設定': '開始設定',
                '開發工作': '進階工具', '工作台': '家庭提案', '助手帳號': '助手帳號',
                '主機維護': '主機維護', '進階工具': '進階工具', '家庭提案': '家庭提案',
                '通訊入口': '通訊入口'}
    if host_mode and name in advanced:
        click_navigation(page, '設定')
        page.get_by_role('navigation', name='設定入口').get_by_role('link', name=advanced[name]).click()
        return
    menu = page.get_by_role('button', name='選單', exact=True)
    if menu.is_visible():
        menu.click()
        expect(page.get_by_role('button', name='收起選單', exact=True)).to_have_attribute('aria-expanded', 'true')
    nav.get_by_role('button', name=aliases.get(name, name) if host_mode else name).click()
    if page.viewport_size['width'] <= 800:
        expect(page.get_by_role('button', name='選單', exact=True)).to_have_attribute('aria-expanded', 'false')
