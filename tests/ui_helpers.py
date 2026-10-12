"""Navigate through the same visible desktop/mobile controls as the owner."""
from playwright.sync_api import expect


def click_navigation(page, name):
    menu = page.get_by_role('button', name='選單', exact=True)
    if menu.is_visible():
        menu.click()
        expect(page.get_by_role('button', name='收起選單', exact=True)).to_have_attribute('aria-expanded', 'true')
    page.get_by_role('navigation', name='主選單').get_by_role('button', name=name).click()
    if page.viewport_size['width'] <= 800:
        expect(page.get_by_role('button', name='選單', exact=True)).to_have_attribute('aria-expanded', 'false')
