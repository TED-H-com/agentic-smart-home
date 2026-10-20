"""Read-only synthetic layout measurements; keep browser profiles on disk."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
scratch = Path(tempfile.gettempdir()).resolve()
if not scratch.is_relative_to(Path.home() / 'agent-work/tmp/pytest'):
    raise SystemExit('Set TMPDIR to a unique disk directory under ~/agent-work/tmp/pytest.')
sys.path.insert(0, str(ROOT / 'tests'))
from playwright.sync_api import expect  # noqa: E402
from test_conversation_discovery_ui import conversation_browser, printer_result  # noqa: E402
from test_synology_device_setup_ui import assert_inside_thread, nas_browser, open_card  # noqa: E402


def close(fixture):
    try:
        next(fixture)
    except StopIteration:
        pass


def measure(page):
    return page.evaluate("""()=>({document_height:document.scrollingElement.scrollHeight,
        viewport_height:innerHeight,thread:document.querySelector('.assistant-conversation').getBoundingClientRect().toJSON(),
        overflow_y:getComputedStyle(document.querySelector('.assistant-conversation')).overflowY})""")


def source_digest(directory):
    files = [directory / 'index.html', *sorted((directory / 'assets').glob('*'))]
    return hashlib.sha256(json.dumps({path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                     for path in files}, sort_keys=True).encode()).hexdigest()


observations = {}
for mode in ('host', 'bridge'):
    base = conversation_browser.__wrapped__()
    page, state = next(base)
    nas = nas_browser.__wrapped__((page, state))
    page, state = next(nas)
    try:
        state['mode'] = mode
        page.set_viewport_size({'width': 390, 'height': 844})
        panel = open_card(page, state)
        controls = [panel.get_by_label('NAS 帳號', exact=True), panel.get_by_label('NAS 密碼', exact=True),
                    panel.get_by_role('button', name='登入並接入', exact=True)]
        for control in controls:
            assert_inside_thread(page, control)
        observations[mode] = {**measure(page), 'controls': [control.bounding_box() for control in controls]}
        assert observations[mode]['overflow_y'] == 'auto'
        assert observations[mode]['document_height'] == 844
    finally:
        close(nas)
        close(base)

baseline = {}
with tempfile.TemporaryDirectory(prefix='base-layout-', dir=scratch) as directory:
    directory = Path(directory)
    names = subprocess.check_output(['git', 'ls-tree', '-r', '--name-only', '549b409', 'controller/static'], cwd=ROOT)
    for name in names.decode().splitlines():
        if '/downloads/' in name:
            continue
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(subprocess.check_output(['git', 'show', f'549b409:{name}'], cwd=ROOT))
    previous = os.environ.get('AGENTIC_UI_STATIC_DIR')
    os.environ['AGENTIC_UI_STATIC_DIR'] = str(directory / 'controller/static')
    fixture = conversation_browser.__wrapped__()
    page, state = next(fixture)
    try:
        page.set_viewport_size({'width': 390, 'height': 844})
        state['response'] = {'discovery': printer_result()}
        page.goto(state['url'] + '#gateway')
        expect(page.get_by_label('對話訊息', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='探索家庭設備', exact=True)).to_be_enabled()
        baseline['empty_host'] = measure(page)
        page.get_by_role('button', name='探索家庭設備', exact=True).click()
        expect(page.get_by_role('region', name='設備探索結果', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='協助設定', exact=True)).to_be_enabled()
        baseline['discovery_host'] = measure(page)
        assert all(value['document_height'] <= 845 and value['overflow_y'] == 'auto' for value in baseline.values()), baseline
        baseline_digest = source_digest(directory / 'controller/static')
    finally:
        close(fixture)
        if previous is None:
            os.environ.pop('AGENTIC_UI_STATIC_DIR', None)
        else:
            os.environ['AGENTIC_UI_STATIC_DIR'] = previous

result = {'schema_version': 1, 'classification': 'synthetic_protocol_browser_evidence',
          'observed_at': datetime.now(UTC).isoformat(),
          'code_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT).decode().strip(),
          'baseline_commit': subprocess.check_output(['git', 'rev-parse', '549b409'], cwd=ROOT).decode().strip(),
          'source_locations': ['controller/static/', 'git show 549b409:controller/static/'],
          'source_digests': {'current': source_digest(ROOT / 'controller/static'), 'baseline': baseline_digest},
          'target': '#gateway at 390x844; synthetic NAS proposal after Owner help action and base Host discovery',
          'method': 'Playwright Chromium protocol fixtures; thread/control rectangles and elementFromPoint centres',
          'versions': {'pytest': version('pytest'), 'playwright': version('playwright')},
          'baseline': baseline, 'current': observations,
          'limitations': ['No real NAS, account, Home Assistant or household acceptance.',
                          'Only these synthetic 390x844 surfaces were measured.']}
result['content_digest'] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
Path(__file__).with_name('layout.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(result, ensure_ascii=False))
