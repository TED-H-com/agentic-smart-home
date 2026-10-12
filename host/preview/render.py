"""Fixed offline renderer. Candidate scripts never enter the owner browser."""
import http.server
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright


class Assets(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory="/input", **kwargs)

    def log_message(self, *args):
        pass


server = http.server.ThreadingHTTPServer(("127.0.0.1", 8765), Assets)
threading.Thread(target=server.serve_forever, daemon=True).start()
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(args=["--disable-dev-shm-usage", "--no-sandbox"])
    for width, height in ((1440, 960), (768, 1024), (390, 844)):
        context = browser.new_context(viewport={"width": width, "height": height},
                                      service_workers="block", accept_downloads=False)
        context.route("**/*", lambda route: route.continue_()
                      if route.request.url.startswith("http://127.0.0.1:8765/") else route.abort())
        page = context.new_page()
        page.goto("http://127.0.0.1:8765/index.html", wait_until="domcontentloaded", timeout=10000)
        page.wait_for_timeout(500)
        page.screenshot(path=str(Path("/work") / f"{width}.png"), timeout=10000, animations="disabled")
        context.close()
    browser.close()
server.shutdown()
