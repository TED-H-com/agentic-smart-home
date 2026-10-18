"""The downloadable integration is complete, deterministic, and excludes local state."""
import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path


def test_notification_download_installs_from_config_root(tmp_path, monkeypatch):
    root = Path(__file__).parents[1]
    spec = importlib.util.spec_from_file_location("notification_package", root / "scripts/package.py")
    package = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(package)
    monkeypatch.setattr(package, "ROOT", tmp_path)
    component = tmp_path / "custom_components/agentic_home_notifications"
    component.mkdir(parents=True)
    payloads = {
        "manifest.json": json.dumps({"domain": "agentic_home_notifications", "version": "0.1.0"}),
        "__init__.py": "# fixture integration\n",
        "services.yaml": "send_text: {}\n",
    }
    for name, content in payloads.items():
        (component / name).write_text(content)
    (component / "__pycache__").mkdir()
    (component / "__pycache__/cache.pyc").write_bytes(b"private scratch")
    private = tmp_path / "private-state"
    private.write_text("never in download")
    (component / "private-link").symlink_to(private)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/HA_NOTIFICATIONS.md").write_text("Install under /config/custom_components.\n")
    package.package_notifications()
    download = tmp_path / "controller/static/downloads/agentic-home-notifications.zip"
    first = download.read_bytes()
    package.package_notifications()
    assert download.read_bytes() == first
    with zipfile.ZipFile(download) as archive:
        manifest = json.loads(archive.read("MANIFEST.sha256.json"))
        assert set(manifest) == {"INSTALL.md", *(
            "custom_components/agentic_home_notifications/" + name for name in payloads)}
        assert set(archive.namelist()) == {*manifest, "MANIFEST.sha256.json"}
        for name, expected in manifest.items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == expected
        config = tmp_path / "ha-config"
        archive.extractall(config)
    assert json.loads((config / "custom_components/agentic_home_notifications/manifest.json").read_text())["domain"] == "agentic_home_notifications"
