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
from agentic_host.source import provision_source, source_identity  # noqa: E402


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


def test_source_identity_is_null_when_missing_or_invalid(tmp_path):
    assert source_identity(tmp_path / 'missing') == {'source_commit': None, 'install_commit': None}
    coding = tmp_path / 'coding'
    coding.mkdir()
    path = coding / 'source.json'
    for text in ('not-json', '[]', json.dumps({'commit': 'a' * 40}),
                 json.dumps({'repo_id': 'other', 'commit': 'a' * 40}),
                 json.dumps({'repo_id': 'agentic-home', 'commit': 'not-a-sha'}),
                 json.dumps({'repo_id': 'agentic-home', 'commit': 'a' * 40, 'install_commit': 'bad'})):
        path.write_text(text)
        assert source_identity(coding) == {'source_commit': None, 'install_commit': None}
    path.unlink()
    path.symlink_to(tmp_path / 'private.json')
    assert source_identity(coding) == {'source_commit': None, 'install_commit': None}
    path.unlink()
    path.write_text(json.dumps({'repo_id': 'agentic-home', 'commit': 'a' * 40}))
    assert source_identity(coding) == {'source_commit': 'a' * 40, 'install_commit': None}
    path.write_text(json.dumps({'repo_id': 'agentic-home', 'commit': 'b' * 40, 'install_commit': 'a' * 40}))
    assert source_identity(coding) == {'source_commit': 'b' * 40, 'install_commit': 'a' * 40}


def test_host_capabilities_report_the_source_a_new_job_binds(tmp_path):
    from types import SimpleNamespace

    from agentic_host.coding_service import CodingService
    coding = tmp_path / 'coding'
    coding.mkdir()
    (coding / 'source.json').write_text(json.dumps({'repo_id': 'agentic-home', 'commit': 'b' * 40, 'install_commit': 'a' * 40}))
    service = CodingService(SimpleNamespace(root=tmp_path / 'runner', product=tmp_path / 'product'), root=coding)
    try:
        caps = service.capabilities()
        assert (caps['source_commit'], caps['install_commit']) == ('b' * 40, 'a' * 40)
        (coding / 'source.json').write_text('not-json')
        caps = service.capabilities()
        assert (caps['source_commit'], caps['install_commit']) == (None, None)
    finally:
        service.close()


def test_provision_keeps_moved_repo_and_rejects_a_foreign_one(source, tmp_path):
    builder, repo, product, assets = source
    lock = builder.coding_source(assets, repo)
    state = tmp_path / 'state'
    assert provision_source(product, state) == lock
    bare = state / 'coding/source'
    (bare / 'canary').write_text('do not reclone')
    (repo / 'controller/app.py').write_text('print("moved")\n')
    git(repo, 'add', 'controller/app.py')
    git(repo, 'commit', '-m', 'Moved fixture')
    commit_b = git(repo, 'rev-parse', 'HEAD')
    subprocess.run(['git', '--git-dir', str(bare), 'fetch', '--no-tags', str(repo),
                    'HEAD:refs/agentic/candidate/' + 'd' * 64], check=True, capture_output=True)
    subprocess.run(['git', '--git-dir', str(bare), 'update-ref', 'HEAD', commit_b], check=True, capture_output=True)
    moved = {'repo_id': 'agentic-home', 'commit': commit_b, 'install_commit': lock['commit'],
             'previous_commit': lock['commit'], 'image_id': 'sha256:' + 'd' * 64, 'candidate_digest': 'e' * 64}
    (state / 'coding/source.json').write_text(json.dumps(moved))
    assert provision_source(product, state) == moved
    assert (bare / 'canary').read_text() == 'do not reclone'
    assert git(bare, 'rev-parse', 'HEAD') == commit_b
    assert json.loads((state / 'coding/source.json').read_text())['install_commit'] == lock['commit']
    foreign = {**moved, 'install_commit': 'a' * 40}
    (state / 'coding/source.json').write_text(json.dumps(foreign))
    with pytest.raises(HostError, match='different content'):
        provision_source(product, state)
    (state / 'coding/source.json').write_text(json.dumps({**moved, 'commit': lock['commit']}))
    with pytest.raises(HostError, match='different content'):
        provision_source(product, state)
    assert (bare / 'canary').read_text() == 'do not reclone'


def test_controller_source_refuses_a_dirty_tree(tmp_path):
    spec = importlib.util.spec_from_file_location('controller_source', ROOT / 'scripts/controller_source.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-b', 'main')
    git(repo, 'config', 'user.email', 'fixture@example.invalid')
    git(repo, 'config', 'user.name', 'Fixture')
    (repo / 'controller').mkdir()
    (repo / 'controller/app.py').write_text("print('clean')\n")
    git(repo, 'add', 'controller')
    git(repo, 'commit', '-m', 'Clean fixture')
    (repo / 'controller/app.py').write_text("print('dirty')\n")
    with pytest.raises(ValueError, match='Commit product source'):
        module.controller_source(repo, tmp_path / 'refused.bundle')
    assert not (tmp_path / 'refused.bundle').exists()
    git(repo, 'checkout', '--', 'controller/app.py')
    destination = repo / 'controller/source/agentic-home.bundle'
    built = module.controller_source(repo, destination)
    assert built['commit'] == git(repo, 'rev-parse', 'HEAD')
    assert subprocess.check_output(['git', 'bundle', 'list-heads', str(destination)], text=True).splitlines() == [
        built['commit'] + ' HEAD']
    assert module.controller_source(repo, destination)['commit'] == built['commit']
