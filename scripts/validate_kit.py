#!/usr/bin/env python3
"""Validate packaged inputs and dependency locks without touching hardware."""
import hashlib
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def main():
    lock = json.loads((ROOT / "providers.lock.json").read_text())
    dependencies = json.loads((ROOT / "runner/package.json").read_text())["dependencies"]
    for item in lock["providers"].values():
        if item.get("npm_package"):
            assert dependencies[item["npm_package"]] == item["version"]
        else:
            assert item["sha512"] == json.loads((ROOT / "runner/agy.lock.json").read_text())["sha512"]
    catalog = ROOT / "runner/agentic_runner/codex-models.json"
    assert hashlib.sha256(catalog.read_bytes()).hexdigest() == lock["codex_model_catalog"]["packaged_sha256"]
    for model in json.loads(catalog.read_text())["models"]:
        assert model["apply_patch_tool_type"] is None and not model["experimental_supported_tools"]
    for app in ("controller", "runner"):
        config = yaml.safe_load((ROOT / app / "config.yaml").read_text())
        assert config["arch"] == ["amd64"] and config["ingress"] and config["apparmor"]
        assert not any(config.get(flag) for flag in ("host_network", "host_pid", "host_dbus", "full_access", "docker_api"))
        assert "map" not in config
        assert config.get("ports", {}) == ({"8443/tcp": 8443} if app == "runner" else {})
        assert bool(config["homeassistant_api"]) == (app == "controller")
        assert bool(config["hassio_api"]) == (app == "controller")
        for line in (ROOT / app / "Dockerfile").read_text().splitlines():
            if line.startswith("FROM "):
                assert "@sha256:" in line
    assert (ROOT / "controller/static/index.html").is_file(), "Build the UI first"
    markdown = [*ROOT.glob("*.md"), *ROOT.glob("docs/*.md"), *ROOT.glob("controller/*.md"), *ROOT.glob("runner/*.md")]
    for path in markdown:
        for target in re.findall(r"\]\(([^\s)]+)\)", path.read_text()):
            if "://" not in target and not target.startswith("#"):
                assert (path.parent / target.split("#")[0]).exists(), f"Broken link in {path.name}: {target}"
    print("App packages, pinned dependencies, tool catalog, compiled UI and document links verified")


if __name__ == "__main__":
    main()
