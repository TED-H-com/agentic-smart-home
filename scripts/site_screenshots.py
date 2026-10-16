"""Capture the HA interface simulation and product frames with fictional state."""
import hashlib
import json
import subprocess
import threading
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
CLOCK = '2026-10-13T09:00:00+08:00'
SCREENS = [
    ('home', '#/lovelace/home', 1440, 900, 1),
    ('integrations', '#/config/integrations', 1440, 900, 1),
    ('gateway', '#gateway', 1440, 900, 1),
    ('proposal', '#/agentic-home/home/demo-proposal', 1440, 900, 1),
    ('inventory', '#inventory', 1440, 900, 1),
    ('workers', '#workers', 1440, 900, 1),
    ('host', '#host', 1440, 900, 1),
    ('bridge', '#/agentic-home', 1440, 900, 1),
    ('phone', '#/lovelace/home', 390, 844, 2),
]


def build_demo(destination):
    subprocess.run(['npx', 'vite', 'build', '--config', 'demo/vite.config.ts',
                    '--outDir', str(destination.resolve())], cwd=ROOT / 'web', check=True)


@contextmanager
def site_server(demo):
    class Handler(SimpleHTTPRequestHandler):
        def translate_path(self, path):
            relative = unquote(urlsplit(path).path).lstrip('/')
            base = demo if relative.startswith('demo/') else ROOT / 'site'
            if relative.startswith('demo/'):
                relative = relative[5:]
            target = (base / relative).resolve()
            return str(target if target.is_relative_to(base.resolve()) else base / '__missing__')

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def settle(page, name):
    if name in {'home', 'phone', 'integrations'}:
        page.locator('.ha-demo-integration-card' if name == 'integrations' else '.ha-demo-tile').first.wait_for()
        page.evaluate('() => document.fonts.ready')
        return
    surface = 'bridge' if name in {'bridge', 'proposal'} else 'outer'
    frame = page.locator(f'#agentic-demo-frame-{surface}').element_handle().content_frame()
    selector = {'gateway': '#gateway-message',
                'bridge': '#gateway-message', 'proposal': '.approval',
                'inventory': '.entity-row', 'workers': '.worker-result-head',
                'host': '.vm-actions'}[name]
    frame.locator(selector).first.wait_for()
    if name in {'gateway', 'bridge'}:
        frame.locator('.assistant-question').first.wait_for()
    if surface == 'outer':
        frame.get_by_text('家庭已連線', exact=True).wait_for()
    if name == 'proposal':
        frame.get_by_text('查看設定差異', exact=True).click()
    if name == 'workers':
        frame.get_by_role('button', name='查看內容', exact=True).first.click()
        frame.locator('.diff-add').first.wait_for()
    if name == 'host':
        frame.get_by_role('button', name='檢查連線', exact=True).click()
        frame.get_by_text('主機與家庭都有回應。', exact=True).wait_for()
        frame.locator('summary').filter(has_text='快照 ·').click()
    frame.evaluate('() => document.fonts.ready')
    page.evaluate('() => document.fonts.ready')
    page.evaluate('() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))')


def capture():
    demo = ROOT / 'build/site-demo'
    build_demo(demo)
    output = ROOT / 'site/assets/screens'
    output.mkdir(parents=True, exist_ok=True)
    files = []
    with site_server(demo) as origin, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        for name, route, width, height, scale in SCREENS:
            context = browser.new_context(viewport={'width': width, 'height': height},
                                          device_scale_factor=scale, locale='zh-TW',
                                          timezone_id='Asia/Taipei', reduced_motion='reduce')
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error, errors=errors: errors.append(str(error)))
            suffix = route.replace('?', '&') if route.startswith('?') else route
            page.goto(f'{origin}/demo/?screenshot=1{suffix}')
            settle(page, name)
            assert not errors, errors
            assert page.evaluate('window.__agenticDemo.unhandled') == []
            path = output / f'{name}.png'
            page.screenshot(path=str(path))
            files.append({'file': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                          'viewport': {'width': width, 'height': height},
                          'device_scale_factor': scale, 'route': route,
                          'surface': 'ha-interface-simulation' if name in {'home', 'integrations', 'phone'}
                          else 'product-in-ha-simulation' if name in {'bridge', 'proposal'} else 'product',
                          'bytes': path.stat().st_size})
            context.close()
        page = browser.new_page(viewport={'width': 1200, 'height': 630}, locale='en-US')
        page.goto(f'{origin}/?lang=en')
        page.evaluate('() => document.fonts.ready')
        page.wait_for_function('Array.from(document.querySelectorAll(".hero img")).every(i=>i.complete && i.naturalWidth>0)')
        page.screenshot(path=str(ROOT / 'site/assets/og.png'))
        files.append({'file': '../og.png', 'sha256': hashlib.sha256(
            (ROOT / 'site/assets/og.png').read_bytes()).hexdigest(),
            'viewport': {'width': 1200, 'height': 630}, 'device_scale_factor': 1,
            'bytes': (ROOT / 'site/assets/og.png').stat().st_size})
        version = browser.version
        browser.close()
    manifest = {'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'source_tree': subprocess.check_output(['git', 'rev-parse', 'HEAD^{tree}'], cwd=ROOT, text=True).strip(),
                'frontend_source_tree': subprocess.check_output(
                    ['git', 'rev-parse', 'HEAD:web/src'], cwd=ROOT, text=True).strip(),
                'source_files': [{'file': str(p.relative_to(ROOT)),
                                  'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                                 for p in sorted([*(ROOT / 'web/demo').glob('*'), ROOT / 'web/preview/fixtures.json',
                                                  ROOT / 'scripts/site_screenshots.py', ROOT / 'site/index.html',
                                                  ROOT / 'site/styles.css', ROOT / 'site/app.js', ROOT / 'site/lang.js'])
                                 if p.is_file()],
                'fixture_clock': CLOCK, 'timezone': 'Asia/Taipei', 'browser_version': version, 'synthetic_data': True,
                'native_or_live_acceptance': False, 'dom_content_modified': False,
                'ha_interface': {'kind': 'original-interactive-simulation',
                                 'reference_version': 'frontend 20260930.3',
                                 'reference_url': 'https://github.com/home-assistant/frontend/tree/20260930.3',
                                 'official_frontend_runtime': False},
                'fixture_sources': ['web/preview/fixtures.json', 'web/demo/fake-api.ts'], 'files': files}
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    print(f'Captured {len(files)} PNGs, {sum(f["bytes"] for f in files):,} bytes')


if __name__ == '__main__':
    capture()
