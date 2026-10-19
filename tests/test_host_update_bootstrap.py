"""One-time conversion retains the complete old tree through an atomic exchange."""
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "host"))
from agentic_host import update_bootstrap as bootstrap
from agentic_host.manager import HostError
from agentic_host.publisher import public_key, validate_official_channel
from test_publisher import key


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


def channel(pem=None):
    return {"format": 1, "publisher": "ted-h-com-agentic-smart-home", "repository": "TED-H-com/agentic-smart-home",
            "channel": "stable", "image_repository": "ghcr.io/ted-h-com/agentic-smart-home-controller" if pem else None,
            "pin": {"public_key_pem": pem, "key_sha256": public_key(pem)[1]} if pem else None}


def engine_source(source, *, pem=None):
    module = source / "host/agentic_host"
    module.mkdir(parents=True, exist_ok=True)
    for name in ("host_update", "host_restore", "host_reconcile"):
        (module / (name + ".py")).write_text("# trusted public product input\n")
    (source / "host/official-update-channel.json").write_text(json.dumps(channel(pem)))
    return source


def test_engine_is_content_addressed_and_cannot_be_replaced(layout, tmp_path):
    manager, _ = layout
    source = manager.product
    module = source / "host/agentic_host"
    module.mkdir(parents=True)
    for name in ("host_update", "host_restore", "host_reconcile"):
        (module / (name + ".py")).write_text("# trusted public product input\n")
    (source / "host/official-update-channel.json").write_text(json.dumps(channel()))
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


def test_publisher_reads_only_the_fixed_owned_version_link(layout, monkeypatch):
    from agentic_host.publisher import official_channel

    manager, bundles = layout
    metadata = {"format": 1, "publisher": "ted-h-com-agentic-smart-home",
                "repository": "TED-H-com/agentic-smart-home", "channel": "stable", "image_repository": None, "pin": None}
    (manager.product / "host").mkdir()
    (manager.product / "host/official-update-channel.json").write_text(json.dumps(metadata))
    monkeypatch.setattr(bootstrap, "ACTIVE", manager.product)
    monkeypatch.setattr(bootstrap, "BUNDLES", bundles)
    baseline = bootstrap.version_layout(manager, bundles=bundles)
    assert bootstrap.active_product(manager.product) == baseline
    assert official_channel(manager.product) == metadata
    other = manager.product.with_name("unowned-link")
    other.symlink_to(baseline)
    with pytest.raises(HostError, match="unowned"):
        official_channel(other)
    manager.product.unlink()
    manager.product.symlink_to(baseline / "host")
    with pytest.raises(HostError, match="unowned"):
        official_channel(manager.product)



def test_pristine_null_pin_defers_engine_until_real_authenticated_media(layout, tmp_path):
    manager, bundles = layout
    engine_source(manager.product)
    engine = tmp_path / "engine"
    baseline = bootstrap.pristine(manager, engine=engine, bundles=bundles)
    assert manager.product.is_symlink() and manager.product.readlink() == baseline
    assert bootstrap.installed_engine_identity(engine=engine) == {"state": "absent", "digest": None}
    assert not engine.exists()
    real_source = engine_source(tmp_path / "real-media", pem=key())
    installed = bootstrap.install_engine(real_source, engine=engine)
    assert bootstrap.installed_engine_identity(engine=engine) == {"state": "active", "digest": installed}


def test_pristine_shipped_pin_installs_active_immutable_engine(layout, tmp_path):
    manager, bundles = layout
    source = engine_source(manager.product)
    shipped = json.loads((Path(__file__).parents[1] / "host/official-update-channel.json").read_text())
    assert validate_official_channel(shipped) == shipped
    assert shipped["image_repository"] == "ghcr.io/ted-h-com/agentic-smart-home-controller"
    assert public_key(shipped["pin"]["public_key_pem"])[1] == shipped["pin"]["key_sha256"] == (
        "18dd73f8b96d115d076be6b362cd62ed011c4be2b6dfdaf808e9b3e5a6a53d39")
    (source / "host/official-update-channel.json").write_text(json.dumps(shipped))
    engine = tmp_path / "engine"
    baseline = bootstrap.pristine(manager, engine=engine, bundles=bundles)
    assert manager.product.readlink() == baseline
    digest = (engine / "current").readlink().name
    assert bootstrap.installed_engine_identity(engine=engine) == {"state": "active", "digest": digest}
    assert json.loads((engine / digest / "official-update-channel.json").read_text()) == shipped
    assert bootstrap.install_engine(baseline, engine=engine) == digest


def test_new_installer_enrolls_shipped_pin_and_engine_before_system_packages(layout, tmp_path, monkeypatch):
    from agentic_host.publisher import install_official_default

    manager, bundles = layout
    source = engine_source(manager.product)
    descriptor = source / "host/official-update-channel.json"
    descriptor.write_bytes((Path(__file__).parents[1] / "host/official-update-channel.json").read_bytes())
    shipped = json.loads(descriptor.read_text())
    (source / "BUNDLE.sha256.json").write_text(json.dumps({
        "host/official-update-channel.json": hashlib.sha256(descriptor.read_bytes()).hexdigest()}))
    spec = importlib.util.spec_from_file_location("sandboxed_pinned_installer", Path(__file__).parents[1] / "host/install.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    trust = tmp_path / "etc/update-publishers.json"
    engine = tmp_path / "engine"
    monkeypatch.setattr(installer, "ROOT", source)
    monkeypatch.setattr(installer, "TARGET", source)
    monkeypatch.setattr(installer, "STATE", manager.root.parent)
    monkeypatch.setattr(installer, "preflight", lambda *args: None)
    monkeypatch.setattr(installer, "Manager", lambda **kwargs: manager)
    monkeypatch.setattr(installer, "atomic", lambda path, value: bootstrap.atomic(tmp_path / "profile.json", value))
    monkeypatch.setattr(installer, "install_official_default", lambda product, **kwargs:
                        install_official_default(product, trust_path=trust, **kwargs))
    pristine = bootstrap.pristine
    monkeypatch.setattr(bootstrap, "pristine", lambda manager: pristine(manager, engine=engine, bundles=bundles))
    monkeypatch.setenv("DEBIAN_FRONTEND", "fixture")

    class BeforeSystemEffects(Exception):
        pass

    def stop_before_system_effects(*args):
        assert args == ("apt-get", "update")
        entry = json.loads(trust.read_text())["publishers"]["ted-h-com-agentic-smart-home"]
        assert entry == {"public_key_pem": shipped["pin"]["public_key_pem"],
                         "image_repository": shipped["image_repository"]}
        assert bootstrap.installed_engine_identity(engine=engine)["state"] == "active"
        assert source.is_symlink()
        raise BeforeSystemEffects

    monkeypatch.setattr(installer, "run", stop_before_system_effects)
    with pytest.raises(BeforeSystemEffects):
        installer.install({}, {})


def test_installer_enables_independent_api_with_null_pin_condition():
    import ast

    root = Path(__file__).parents[1]
    tree = ast.parse((root / "host/install.py").read_text())
    install = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "install")
    calls = [node for node in ast.walk(install) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "run"]
    api = [node for node in calls if [arg.value if isinstance(arg, ast.Constant) else None for arg in node.args]
           == ["systemctl", "enable", "--now", "agentic-home-update-api"]]
    assert len(api) == 1
    pristine = next(node for node in ast.walk(install) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name) and node.func.id == "pristine")
    assert pristine.lineno < api[0].lineno
    unit = (root / "host/systemd/agentic-home-update-api.service").read_text()
    assert "ConditionPathExists=/usr/lib/agentic-home-update/current/agentic_host/update_service.py" in unit
    assert "PYTHONPATH=/usr/lib/agentic-home-update/current" in unit
    assert "/opt/agentic-home" not in unit


def test_legacy_null_engine_migrates_only_with_exact_owner_reviewed_digest(layout, tmp_path):
    manager, _ = layout
    engine = tmp_path / "engine"
    source = engine_source(manager.product)
    old_digest = bootstrap.install_engine(source, engine=engine)
    assert bootstrap.installed_engine_identity(engine=engine) == {"state": "inactive", "digest": old_digest}
    replacement = engine_source(tmp_path / "real-media", pem=key())
    for approval in (None, "0" * 64):
        with pytest.raises(HostError, match="already_installed"):
            bootstrap.install_engine(replacement, engine=engine, expected_inactive_engine_digest=approval)
        assert (engine / "current").readlink() == Path(old_digest)
    new_digest = bootstrap.install_engine(replacement, engine=engine, expected_inactive_engine_digest=old_digest)
    assert new_digest != old_digest and (engine / "current").readlink() == Path(new_digest)
    assert (engine / old_digest / "official-update-channel.json").exists()
    assert bootstrap.installed_engine_identity(engine=engine) == {"state": "active", "digest": new_digest}


def test_even_explicit_local_review_cannot_replace_active_key_engine(layout, tmp_path):
    manager, _ = layout
    engine = tmp_path / "engine"
    first = bootstrap.install_engine(engine_source(manager.product, pem=key()), engine=engine)
    for pem in (None, key()):
        source = engine_source(tmp_path / ("null-media" if pem is None else "other-key-media"), pem=pem)
        with pytest.raises(HostError, match="already_installed"):
            bootstrap.install_engine(source, engine=engine, expected_inactive_engine_digest=first)
    assert (engine / "current").readlink() == Path(first)


def test_null_engine_migration_refuses_changed_installed_code_or_pointer(layout, tmp_path):
    manager, _ = layout
    engine = tmp_path / "engine"
    old = bootstrap.install_engine(engine_source(manager.product), engine=engine)
    replacement = engine_source(tmp_path / "real-media", pem=key())
    (engine / old / "agentic_host/host_update.py").write_text("# changed installed code")
    with pytest.raises(HostError, match="engine_changed"):
        bootstrap.install_engine(replacement, engine=engine, expected_inactive_engine_digest=old)
    assert (engine / "current").readlink() == Path(old)


def test_null_engine_migration_rechecks_revision_after_copy(layout, tmp_path, monkeypatch):
    manager, _ = layout
    engine = tmp_path / "engine"
    old = bootstrap.install_engine(engine_source(manager.product), engine=engine)
    replacement = engine_source(tmp_path / "real-media", pem=key())
    identity = bootstrap.installed_engine_identity
    count = 0
    def changed_revision(**kwargs):
        nonlocal count
        count += 1
        observed = identity(**kwargs)
        return observed if count == 1 else {**observed, "digest": "f" * 64}
    monkeypatch.setattr(bootstrap, "installed_engine_identity", changed_revision)
    with pytest.raises(HostError, match="revision_changed"):
        bootstrap.install_engine(replacement, engine=engine, expected_inactive_engine_digest=old)
    assert (engine / "current").readlink() == Path(old)
