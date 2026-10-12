"""Real offline Git release source provisioning and tamper/dirty-source rejection."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'host'))
from agentic_host.manager import HostError  # noqa: E402
from agentic_host.source import provision_source  # noqa: E402


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


@pytest.fixture
def source(tmp_path):
    spec = importlib.util.spec_from_file_location('source_builder', ROOT / 'scripts/build_host.py')
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-b', 'main')
    git(repo, 'config', 'user.name', 'Synthetic release')
    git(repo, 'config', 'user.email', 'fixture@example.invalid')
    (repo / 'controller').mkdir()
    (repo / 'controller/app.py').write_text('print("committed product")\n')
    git(repo, 'add', 'controller')
    git(repo, 'commit', '-m', 'Product fixture')
    product = tmp_path / 'product'
    assets = product / 'assets'
    assets.mkdir(parents=True)
    return builder, repo, product, assets


def test_exact_offline_commit_private_independent_source_and_no_other_refs(source, tmp_path):
    builder, repo, product, assets = source
    commit = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'checkout', '-b', 'excluded')
    (repo / 'other.txt').write_text('other branch only')
    git(repo, 'add', 'other.txt')
    git(repo, 'commit', '-m', 'Must not ship other branch')
    git(repo, 'checkout', 'main')
    (repo / 'local-note.txt').write_text('local material must never ship')
    lock = builder.coding_source(assets, repo)
    assert lock['commit'] == commit
    state = tmp_path / 'state'
    assert provision_source(product, state) == lock
    bare = state / 'coding/source'
    assert git(bare, 'rev-parse', '--is-bare-repository') == 'true'
    assert git(bare, 'rev-parse', 'HEAD') == commit
    assert git(bare, 'ls-tree', '--name-only', 'HEAD') == 'controller'
    assert bare.stat().st_mode & 0o777 == 0o700
    assert (state / 'coding/source.json').stat().st_mode & 0o777 == 0o600
    clone = tmp_path / 'private-clone'
    subprocess.run(['git', 'clone', '--no-hardlinks', str(bare), str(clone)], check=True, capture_output=True)
    (clone / 'controller/app.py').write_text('private change')
    assert git(bare, 'show', 'HEAD:controller/app.py') == 'print("committed product")'
    assert provision_source(product, state) == lock
    (state / 'coding/source.json').write_text('{}')
    with pytest.raises(HostError, match='different content'):
        provision_source(product, state)


def test_dirty_product_and_altered_bundle_or_commit_are_refused(source, tmp_path):
    builder, repo, product, assets = source
    (repo / 'controller/app.py').write_text('dirty')
    with pytest.raises(ValueError, match='Commit product source'):
        builder.coding_source(assets, repo)
    git(repo, 'checkout', '--', 'controller/app.py')
    builder.coding_source(assets, repo)
    lockpath = assets / 'coding-source.json'
    lock = json.loads(lockpath.read_text())
    lockpath.write_text(json.dumps({**lock, 'commit': 'a' * 40}))
    with pytest.raises(HostError, match='commit mismatch'):
        provision_source(product, tmp_path / 'wrong-commit')
    lockpath.write_text(json.dumps(lock))
    with (assets / 'coding-source.bundle').open('ab') as output:
        output.write(b'corruption')
    with pytest.raises(HostError, match='checksum mismatch'):
        provision_source(product, tmp_path / 'wrong-checksum')
