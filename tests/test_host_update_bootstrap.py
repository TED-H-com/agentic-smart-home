"""One-time conversion retains the complete old tree through an atomic exchange."""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "host"))
from agentic_host import update_bootstrap as bootstrap
from agentic_host.manager import HostError


@pytest.fixture
def layout(tmp_path, monkeypatch):
    previous_umask = os.umask(0o077)
    original_stat, original_lstat = Path.stat, Path.lstat
    def root_stat(path, *args, **kwargs):
        fields = list(original_stat(path, *args, **kwargs))
        fields[4] = 0
        if not path.is_relative_to(tmp_path):
            fields[0] &= ~0o022
        return os.stat_result(fields)
    def root_lstat(path, *args, **kwargs):
        fields = list(original_lstat(path, *args, **kwargs))
        fields[4] = 0
        if not path.is_relative_to(tmp_path):
            fields[0] &= ~0o022
        return os.stat_result(fields)
    monkeypatch.setattr(Path, "stat", root_stat)
    monkeypatch.setattr(Path, "lstat", root_lstat)
    monkeypatch.setattr(bootstrap.os, "geteuid", lambda: 0)
    active = tmp_path / "opt/agentic-home"
    active.mkdir(parents=True)
    (active / "original").write_text("untouched previous product")
    root = tmp_path / "state/host"
    root.mkdir(parents=True)
    manager = SimpleNamespace(product=active, root=root, state=lambda: {})
    try:
        yield manager, tmp_path / "opt/agentic-home-bundles"
    finally:
        os.umask(previous_umask)


def test_atomic_conversion_never_loses_old_tree_and_is_idempotent(layout):
    manager, bundles = layout
    inode = manager.product.stat().st_ino
    baseline = bootstrap.version_layout(manager, bundles=bundles)
    assert manager.product.is_symlink()
    assert manager.product.readlink() == baseline
    assert baseline.stat().st_ino == inode
    assert (manager.product / "original").read_text() == "untouched previous product"
    assert bootstrap.version_layout(manager, bundles=bundles) == baseline
    assert len(list(bundles.iterdir())) == 1
    assert json.loads((manager.root / "host-update-bootstrap.json").read_text())["phase"] == "complete"


@pytest.mark.parametrize("boundary", ["before", "after"])
def test_interrupted_exchange_replays_exact_pair(layout, monkeypatch, boundary):
    manager, bundles = layout
    original = bootstrap.exchange
    def interrupted(first, second):
        if boundary == "after":
            original(first, second)
        raise SystemExit("simulated loss of process")
    monkeypatch.setattr(bootstrap, "exchange", interrupted)
    with pytest.raises(SystemExit):
        bootstrap.version_layout(manager, bundles=bundles)
    assert (manager.product / "original").read_text() == "untouched previous product"
    monkeypatch.setattr(bootstrap, "exchange", original)
    bootstrap.version_layout(manager, bundles=bundles)
    assert (manager.product / "original").read_text() == "untouched previous product"


def test_engine_is_content_addressed_and_cannot_be_replaced(layout, tmp_path):
    manager, _ = layout
    source = manager.product
    module = source / "host/agentic_host"
    module.mkdir(parents=True)
    for name in ("host_update", "host_restore", "host_reconcile"):
        (module / (name + ".py")).write_text("# trusted public product input\n")
    (source / "host/official-update-channel.json").write_text('{"pin":null}')
    engine = tmp_path / "usr/lib/agentic-home-update"
    first = bootstrap.install_engine(source, engine=engine)
    assert (engine / "current").readlink() == Path(first)
    assert bootstrap.install_engine(source, engine=engine) == first
    (module / "host_update.py").write_text("# different engine\n")
    with pytest.raises(HostError, match="already_installed"):
        bootstrap.install_engine(source, engine=engine)
    assert (engine / "current").readlink() == Path(first)


def test_pristine_install_refuses_used_household(layout, tmp_path):
    manager, bundles = layout
    manager.state = lambda: {"owner": "existing-home"}
    with pytest.raises(HostError, match="owner_approval"):
        bootstrap.pristine(manager, engine=tmp_path / "engine", bundles=bundles)
    assert not manager.product.is_symlink()


def test_bootstrap_rejects_unowned_active_link(layout, tmp_path):
    manager, bundles = layout
    (manager.product / "original").unlink()
    manager.product.rmdir()
    manager.product.symlink_to(tmp_path / "other-product")
    with pytest.raises(HostError, match="unowned"):
        bootstrap.version_layout(manager, bundles=bundles)
