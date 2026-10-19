"""Shipped-pin trust enrollment; fixture keys never leave isolated disk scratch."""
import json
import os
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "host"))
from agentic_host import publisher
from agentic_host.manager import HostError
from agentic_host.publisher import (
    OFFICIAL_CHANNEL,
    OFFICIAL_IMAGE,
    OFFICIAL_PUBLISHER,
    OFFICIAL_REPOSITORY,
    OfficialPublisher,
    install_official_default,
    public_key,
    validate_official_channel,
)
from test_publisher import key


def metadata(pem=None):
    return {"format": 1, "publisher": OFFICIAL_PUBLISHER, "repository": OFFICIAL_REPOSITORY,
            "channel": OFFICIAL_CHANNEL, "image_repository": OFFICIAL_IMAGE if pem else None,
            "pin": {"public_key_pem": pem, "key_sha256": public_key(pem)[1]} if pem else None}


@pytest.fixture(params=["throwaway", "shipped"])
def official(tmp_path, monkeypatch, request):
    # Simulate root ownership only; preserve real modes/types and symlink checks.
    # No test may enroll anything outside this fixture's explicit trust_path.
    previous_umask = os.umask(0o077)
    original_stat = Path.stat
    def root_stat(path, *args, **kwargs):
        value = original_stat(path, *args, **kwargs)
        fields = list(value)
        if fields[4] == os.getuid():
            fields[4] = 0
        if not path.is_relative_to(tmp_path):
            fields[0] &= ~0o022  # simulate trusted system ancestry outside scratch
        return os.stat_result(fields)
    monkeypatch.setattr(Path, "stat", root_stat)
    monkeypatch.setattr(publisher.os, "geteuid", lambda: 0)
    product = tmp_path / "product"
    (product / "host").mkdir(parents=True)
    descriptor = product / "host/official-update-channel.json"
    value = (json.loads((Path(__file__).parents[1] / "host/official-update-channel.json").read_text())
             if request.param == "shipped" else metadata(key()))
    descriptor.write_text(json.dumps(value))
    manager = SimpleNamespace(product=product, locked=nullcontext)
    service = OfficialPublisher(manager, trust_path=tmp_path / "etc/update-publishers.json")
    try:
        yield service, descriptor
    finally:
        os.umask(previous_umask)


def test_shipped_production_metadata_has_approved_public_key_and_fingerprint():
    value = json.loads((Path(__file__).parents[1] / "host/official-update-channel.json").read_text())
    assert validate_official_channel(value) == value
    assert value["image_repository"] == "ghcr.io/ted-h-com/agentic-smart-home-controller"
    expected_pem = ("-----BEGIN PUBLIC KEY-----\n"
                    "MCowBQYDK2VwAyEA1fUHnQTX7LEJxfdrkmuvpdPw7r8uBwozTfqns+na97c=\n"
                    "-----END PUBLIC KEY-----\n")
    expected_fingerprint = "18dd73f8b96d115d076be6b362cd62ed011c4be2b6dfdaf808e9b3e5a6a53d39"
    assert value["pin"] == {"public_key_pem": expected_pem, "key_sha256": expected_fingerprint}
    assert public_key(expected_pem) == (expected_pem, expected_fingerprint)


def test_unconfigured_metadata_remains_valid_without_inventing_a_publisher():
    value = metadata()
    assert validate_official_channel(value) == value
    assert value["pin"] is None and value["image_repository"] is None
    with pytest.raises(HostError, match="metadata_invalid"):
        validate_official_channel({**value, "image_repository": OFFICIAL_IMAGE})


@pytest.mark.parametrize("field,value", [("format", True), ("repository", "evil/repo"),
                                         ("publisher", "evil"), ("channel", "latest"),
                                         ("image_repository", "ghcr.io/evil/controller")])
def test_metadata_fixes_official_identity_and_repository(field, value):
    with pytest.raises(HostError):
        validate_official_channel({**metadata(key()), field: value})


def test_metadata_rejects_fingerprint_mismatch_extra_fields_and_private_key():
    value = metadata(key())
    value["pin"]["key_sha256"] = "0" * 64
    with pytest.raises(HostError, match="fingerprint_mismatch"):
        validate_official_channel(value)
    with pytest.raises(HostError):
        validate_official_channel({**metadata(), "url": "https://evil.test/key"})
    value["pin"]["public_key_pem"] = "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----"
    with pytest.raises(HostError, match="ed25519"):
        validate_official_channel(value)


def test_owner_can_approve_only_exact_shipped_pin_once(official):
    service, _ = official
    assert service.review()[0]["state"] == "approval_required"
    assert not service.trust.exists()
    offer = service.offer({})
    assert "public_key_pem" not in json.dumps(offer)
    assert offer["scope"] == "controller_only_same_schema"
    assert offer["expires_at"] > publisher.time.time()
    body = {"approval": offer["approval"], "review_digest": offer["review_digest"]}
    result = service.approve(body)
    assert result["state"] == "trusted"
    assert set(json.loads(service.trust.read_text())["publishers"]) == {OFFICIAL_PUBLISHER}
    assert service.trust.stat().st_mode & 0o777 == 0o600
    with pytest.raises(HostError, match="approval_invalid"):
        service.approve(body)


def test_no_key_never_offers_or_changes_trust(official):
    service, descriptor = official
    descriptor.write_text(json.dumps(metadata()))
    assert service.status()["state"] == "unavailable"
    assert service.status()["review_digest"] is None
    with pytest.raises(HostError, match="not_available"):
        service.offer({})
    assert install_official_default(service.manager.product, new_install=True, trust_path=service.trust)["state"] == "unavailable"
    assert not service.trust.exists()


@pytest.mark.parametrize("body", [{"public_key_pem": "browser-key"}, {"repository": OFFICIAL_IMAGE},
                                  {"url": "https://github.com/key"}, None, []])
def test_offer_rejects_browser_key_url_repository_inputs(official, body):
    with pytest.raises(HostError, match="request_invalid"):
        official[0].offer(body)


def test_offer_expires_and_broker_restart_cannot_reuse_approval(official, monkeypatch):
    service, _ = official
    offer = service.offer({})
    body = {"approval": offer["approval"], "review_digest": offer["review_digest"]}
    restarted = OfficialPublisher(service.manager, trust_path=service.trust)
    with pytest.raises(HostError, match="approval_invalid"):
        restarted.approve(body)
    monkeypatch.setattr(publisher.time, "monotonic", lambda: service.pending["deadline"] + 1)
    with pytest.raises(HostError, match="approval_invalid"):
        service.approve(body)
    assert not service.trust.exists()


@pytest.mark.parametrize("change", ["key", "trust"])
def test_approval_rechecks_shipped_key_and_all_local_trust_revisions(official, change):
    service, descriptor = official
    offer = service.offer({})
    if change == "key":
        descriptor.write_text(json.dumps(metadata(key())))
    else:
        service.trust.parent.mkdir()
        service.trust.write_text(json.dumps({"format": 1, "publishers": {"another": {}}}))
    before = service.trust.read_text() if service.trust.exists() else None
    with pytest.raises(HostError, match="revision_changed"):
        service.approve({"approval": offer["approval"], "review_digest": offer["review_digest"]})
    assert (service.trust.read_text() if service.trust.exists() else None) == before
    assert service.pending is None


def test_conflicting_official_key_never_rotates_automatically(official):
    service, _ = official
    service.trust.parent.mkdir()
    value = {"format": 1, "publishers": {OFFICIAL_PUBLISHER: {"public_key_pem": key(), "image_repository": OFFICIAL_IMAGE}}}
    service.trust.write_text(json.dumps(value))
    assert service.status()["state"] == "conflict"
    with pytest.raises(HostError, match="not_available"):
        service.offer({})
    assert json.loads(service.trust.read_text()) == value


def test_new_install_enrolls_but_used_install_or_existing_store_is_preserved(official):
    service, _ = official
    assert install_official_default(service.manager.product, new_install=False, trust_path=service.trust)["state"] == "preserved"
    assert not service.trust.exists()
    assert install_official_default(service.manager.product, new_install=True, trust_path=service.trust)["state"] == "trusted"
    assert service.status()["state"] == "trusted"
    service.trust.write_text('{"format":1,"publishers":{}}')
    assert install_official_default(service.manager.product, new_install=True, trust_path=service.trust)["state"] == "preserved"
    assert not json.loads(service.trust.read_text())["publishers"]


@pytest.mark.parametrize("location", ["metadata", "metadata-parent", "trust", "trust-parent", "lock", "temporary"])
def test_symlink_paths_cannot_import_key_or_redirect_trust_writes(official, location):
    service, descriptor = official
    offer = service.offer({})
    outside = descriptor.parent.parent / "outside.json"
    outside.write_text("must remain untouched")
    if location == "metadata":
        saved = descriptor.read_text()
        descriptor.unlink()
        target = descriptor.with_name("key.json")
        target.write_text(saved)
        descriptor.symlink_to(target)
    elif location == "metadata-parent":
        folder = descriptor.parent
        moved = folder.with_name("moved-host")
        folder.rename(moved)
        folder.symlink_to(moved, target_is_directory=True)
    else:
        service.trust.parent.mkdir()
        if location == "trust-parent":
            folder = service.trust.parent
            moved = folder.with_name("moved-etc")
            folder.rename(moved)
            folder.symlink_to(moved, target_is_directory=True)
        else:
            target = {"trust": service.trust, "lock": service.trust.with_suffix(".lock"),
                      "temporary": service.trust.with_name(service.trust.name + ".new")}[location]
            target.symlink_to(outside)
    with pytest.raises(HostError, match="untrusted"):
        service.approve({"approval": offer["approval"], "review_digest": offer["review_digest"]})
    assert outside.read_text() == "must remain untouched"


def test_group_writable_metadata_and_ancestry_are_rejected(official):
    service, descriptor = official
    descriptor.chmod(0o666)
    assert service.status()["state"] == "unavailable"
    assert not service.trust.exists()
    descriptor.chmod(0o644)
    descriptor.parent.chmod(0o775)
    assert service.status()["state"] == "unavailable"


def test_updater_routes_expose_only_fixed_shipped_pin_onboarding(official):
    from agentic_host.updater import Updater
    service, _ = official
    updater = object.__new__(Updater)
    updater.official_publisher = service
    code, status = updater.handle("GET", "/updates/publisher")
    assert code == 200 and status["state"] == "approval_required"
    code, offer = updater.handle("POST", "/updates/publisher/offer", {})
    assert code == 200
    code, trusted = updater.handle("POST", "/updates/publisher/approve",
                                   {"approval": offer["approval"], "review_digest": offer["review_digest"]})
    assert code == 200 and trusted["state"] == "trusted"


def test_empty_local_store_and_dangling_local_symlink_do_not_fallback(official, monkeypatch):
    from agentic_host import updater as updater_module
    from agentic_host.updater import UpdateError, Updater
    service, descriptor = official
    fallback = descriptor.with_name("update-publishers.json")
    fallback.write_text(json.dumps({"format": 1, "publishers": {"bundled": {}}}))
    updater = object.__new__(Updater)
    updater.trust = fallback
    monkeypatch.setattr(updater_module, "TRUST", service.trust)
    service.trust.parent.mkdir()
    service.trust.write_text('{"format":1,"publishers":{}}')
    assert updater.publishers() == {}
    service.trust.unlink()
    service.trust.symlink_to(service.trust.with_name("missing.json"))
    with pytest.raises(UpdateError, match="store_untrusted"):
        updater.publishers()


def test_store_revision_is_checked_again_under_enrollment_file_lock(official, monkeypatch):
    service, _ = official
    offer = service.offer({})
    enroll = publisher.enroll
    def racing_enroll(*args, **kwargs):
        service.trust.parent.mkdir()
        service.trust.write_text('{"format":1,"publishers":{"another":{}}}')
        return enroll(*args, **kwargs)
    monkeypatch.setattr(publisher, "enroll", racing_enroll)
    with pytest.raises(HostError, match="revision_changed"):
        service.approve({"approval": offer["approval"], "review_digest": offer["review_digest"]})
    assert set(json.loads(service.trust.read_text())["publishers"]) == {"another"}


def installer_module():
    import importlib.util
    path = Path(__file__).parents[1] / "host/install.py"
    spec = importlib.util.spec_from_file_location("official_test_installer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_installer_never_uses_pin_outside_explicit_bundle_manifest(official, monkeypatch):
    service, _ = official
    installer = installer_module()
    monkeypatch.setattr(installer, "ROOT", service.manager.product)
    monkeypatch.setattr(installer, "TARGET", service.manager.product)
    monkeypatch.setattr(installer, "STATE", service.manager.product.parent / "state")
    monkeypatch.setattr(installer, "preflight", lambda *args: None)
    monkeypatch.setattr(installer, "verify_bundle", lambda *args: None)
    monkeypatch.setattr(installer, "install_official_default", lambda *args, **kw: pytest.fail("uncovered pin must never be used"))
    (service.manager.product / "BUNDLE.sha256.json").write_text("{}")
    with pytest.raises(HostError, match="not covered"):
        installer.install({}, {})


@pytest.mark.parametrize("used_marker", [None, "controller/access-key", "host/state.json", "host/runtime.env"])
def test_installer_distinguishes_new_and_used_system_before_trust_enrollment(official, monkeypatch, used_marker):
    service, _ = official
    installer = installer_module()
    state = service.manager.product.parent / "state"
    if used_marker:
        marker = state / used_marker
        marker.parent.mkdir(parents=True)
        marker.write_text("fixture-existing-installation")
    monkeypatch.setattr(installer, "ROOT", service.manager.product)
    monkeypatch.setattr(installer, "TARGET", service.manager.product)
    monkeypatch.setattr(installer, "STATE", state)
    monkeypatch.setattr(installer, "preflight", lambda *args: None)
    monkeypatch.setattr(installer, "verify_bundle", lambda *args: None)
    (service.manager.product / "BUNDLE.sha256.json").write_text('{"host/official-update-channel.json":"fixture"}')
    def stop_before_any_installer_effects(product, *, new_install):
        assert new_install is (used_marker is None)
        raise HostError("fixture_stopped_before_install")
    monkeypatch.setattr(installer, "install_official_default", stop_before_any_installer_effects)
    with pytest.raises(HostError, match="fixture_stopped_before_install"):
        installer.install({}, {})


def test_actual_root_approves_only_shipped_pin_in_disposable_container(request):
    """Real root-owned files and enrollment; no host devices or writable mounts."""
    import subprocess
    import uuid

    if not request.config.getoption('--containers'):
        pytest.skip('requires isolated root publisher container')
    root = Path(__file__).resolve().parents[1]
    name = 'agentic-official-publisher-' + uuid.uuid4().hex
    script = '''import base64,json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from agentic_host.publisher import (OFFICIAL_PUBLISHER,OFFICIAL_REPOSITORY,OFFICIAL_IMAGE,OFFICIAL_CHANNEL,
                                    OfficialPublisher,public_key)
from agentic_host.manager import HostError
# RFC8032 public verification vector; no private key is present.
der=bytes.fromhex('302a300506032b6570032100d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a')
pem='-----BEGIN PUBLIC KEY-----\\n'+base64.b64encode(der).decode()+'\\n-----END PUBLIC KEY-----\\n'
pem,fingerprint=public_key(pem)
product=Path('/opt/fixture-product');(product/'host').mkdir(parents=True)
metadata={'format':1,'publisher':OFFICIAL_PUBLISHER,'repository':OFFICIAL_REPOSITORY,'channel':OFFICIAL_CHANNEL,
          'image_repository':OFFICIAL_IMAGE,'pin':{'public_key_pem':pem,'key_sha256':fingerprint}}
(product/'host/official-update-channel.json').write_text(json.dumps(metadata))
trust=Path('/etc/agentic-home/update-publishers.json')
service=OfficialPublisher(SimpleNamespace(product=product,locked=nullcontext),trust_path=trust)
assert service.status()['state']=='approval_required' and not trust.exists()
try:service.offer({'public_key_pem':pem})
except HostError:pass
else:raise AssertionError('unexpected caller key accepted')
offer=service.offer({});body={key:offer[key] for key in ['review_digest','approval']}
assert service.approve(body)['state']=='trusted'
assert trust.stat().st_uid==0 and trust.stat().st_mode&0o777==0o600
assert json.loads(trust.read_text())['publishers'][OFFICIAL_PUBLISHER]['image_repository']==OFFICIAL_IMAGE
try:service.approve(body)
except HostError:pass
else:raise AssertionError('approval replay accepted')
print(json.dumps({'shipped_pin_only':True,'root_store_mode':'0600','approval_one_use':True,'host_modified':False}))
'''
    try:
        result = subprocess.run([
            'docker', 'run', '--rm', '--pull=never', '--name', name, '--network', 'none', '--user', '0:0',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--memory', '128m', '--pids-limit', '32',
            '--cpus', '1', '--entrypoint', 'python', '--env', 'PYTHONPATH=/product/host',
            '--mount', f'type=bind,src={root / "host"},dst=/product/host,readonly',
            'agentic-home-preview:0.11.0', '-c', script,
        ], capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {'shipped_pin_only': True, 'root_store_mode': '0600',
                                             'approval_one_use': True, 'host_modified': False}
    finally:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=20)
