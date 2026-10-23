"""Installed dependency behavior in the restricted image; synthetic bytes only."""

import json
import os
import subprocess
import uuid

import pytest

# The JPEG guard regression follows upstream's 242a059 test boundary: a decoded
# scanline must fit the allocated tile, including when a decoder ignores scaling.
# The empty morphology cases exercise the boundary fixed by upstream 18f7c0d.
IMAGE_PROBE = '''
import importlib.util
import io
import json
import os
from importlib.metadata import PackageNotFoundError, version
from PIL import Image, ImageMorph, features
import pillow_heif

assert os.getuid() == 10003
assert version('Pillow') == '13.0.0'
assert importlib.util.find_spec('browser_harness') is None
for package in ('browser-harness', 'cdp-use', 'fetch-use'):
    try:
        version(package)
    except PackageNotFoundError:
        pass
    else:
        raise AssertionError('Unexposed browser dependency is still installed: ' + package)

checks = []
for mode, color in [('L', 91), ('RGB', (30, 100, 180))]:
    raw = io.BytesIO()
    Image.new(mode, (128, 96), color).save(raw, format='JPEG')
    with Image.open(io.BytesIO(raw.getvalue())) as normal:
        normal.thumbnail((32, 32))
        assert normal.size == (32, 24) and normal.mode == mode
    with Image.open(io.BytesIO(raw.getvalue())) as mismatched:
        tile = mismatched.tile[0]
        mismatched.tile = [tile._replace(extents=(0, 0, 64, 96))]
        try:
            mismatched.load()
        except OSError as error:
            assert 'broken data stream' in str(error)
        else:
            raise AssertionError('JPEG decoded wider scanlines into a smaller tile')
    checks.append('jpeg_geometry_guard_and_thumbnail_' + mode)

morph = ImageMorph.MorphOp(op_name='dilation4')
for size in ((0, 1), (0, 5), (5, 0)):
    changed, empty = morph.apply(Image.new('L', size))
    assert changed == 0 and empty.size == size and empty.tobytes() == b''
source = Image.new('L', (5, 5))
source.putpixel((2, 2), 255)
changed, expanded = morph.apply(source)
assert changed > 0 and expanded.getpixel((2, 2)) == 255
assert expanded.getpixel((1, 2)) == 255
checks.append('morphology_empty_and_nonempty')

pillow_heif.register_heif_opener()
for image_format in ('PNG', 'HEIF'):
    raw = io.BytesIO()
    original = Image.new('RGB', (32, 24), (30, 100, 180))
    original.save(raw, format=image_format, **({'quality': -1} if image_format == 'HEIF' else {}))
    with Image.open(io.BytesIO(raw.getvalue())) as restored:
        restored.load()
        assert restored.size == original.size and restored.mode == 'RGB'
        assert all(abs(a - b) <= 2 for a, b in zip(restored.getpixel((10, 10)), (30, 100, 180)))
    checks.append(image_format.lower() + '_roundtrip')
print(json.dumps({'uid': os.getuid(), 'pillow': version('Pillow'),
    'pillow_heif': version('pillow-heif'), 'jpeg_codec': features.version_codec('jpg'),
    'libjpeg_turbo': features.version_feature('libjpeg_turbo'),
    'browser_harness_installed': False, 'checks': checks}))
'''


def test_packaged_image_dependencies_preserve_formats_and_reject_unsafe_geometry(request):
    if not request.config.getoption('--containers'):
        pytest.skip('Installed Pillow/HEIF and native guards require --containers')
    name = 'agentic-hermes-image-deps-' + uuid.uuid4().hex[:12]
    image = os.environ.get('AGENTIC_HERMES_IMAGE', 'agentic-home-hermes:0.11.1')
    command = ['docker', 'run', '--rm', '--name', name, '-i', '--network=none',
               '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
               '--memory=512m', '--pids-limit=64', '--entrypoint=python', image, '-']
    try:
        result = subprocess.run(command, input=IMAGE_PROBE, capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
        receipt = json.loads(result.stdout)
        assert receipt['checks'] == ['jpeg_geometry_guard_and_thumbnail_L',
            'jpeg_geometry_guard_and_thumbnail_RGB', 'morphology_empty_and_nonempty',
            'png_roundtrip', 'heif_roundtrip']
        print(json.dumps(receipt, sort_keys=True))
    finally:
        subprocess.run(['docker', 'rm', '-fv', name], capture_output=True, check=False)
