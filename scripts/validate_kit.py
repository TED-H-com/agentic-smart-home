#!/usr/bin/env python3
"""Validate packaged inputs and dependency locks without touching hardware."""
import ast
import hashlib
import json
import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def main():
    from worker_runtime import package_worker_runtime
    package_worker_runtime(check=True)
    release = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    for name in ("host/agentic_host/__init__.py", "controller/agentic_controller/__init__.py",
                 "runner/agentic_runner/__init__.py", "bridge/agentic_bridge/__init__.py",
                 "scripts/build_host.py", "scripts/package.py"):
        assignments = {target.id: ast.literal_eval(node.value) for node in ast.parse((ROOT / name).read_text()).body
                       if isinstance(node, ast.Assign) for target in node.targets
                       if isinstance(target, ast.Name) and target.id == "VERSION"}
        assert assignments.get("VERSION") == release, "Release version drift: " + name
    assert json.loads((ROOT / "web/package.json").read_text())["version"] == release
    compose = yaml.safe_load((ROOT / "host/compose.yaml").read_text())
    for service in ("controller", "runner", "linejs", "hermes"):
        expected = f"agentic-home-{service}:{release}"
        if service in {"hermes", "controller"}:
            expected = "${AGENTIC_" + service.upper() + "_IMAGE:-" + expected + "}"
        assert compose["services"][service]["image"] == expected
    network = json.loads((ROOT / "host/network.lock.json").read_text())
    assert (ROOT / "host/preview/Dockerfile").read_text().splitlines()[0] == "FROM " + network["preview"]["base"]
    assert re.fullmatch(r"[a-f0-9]{64}", network["tailscale"]["sha256"])
    assert network["tailscale"]["url"].startswith("https://pkgs.tailscale.com/stable/ubuntu/pool/")
    assert "/run/agentic-home:/run/agentic-home" in compose["services"]["runner"]["volumes"]
    assert not any("docker.sock" in mount for service in compose["services"].values() for mount in service.get("volumes", []))
    lock = json.loads((ROOT / "providers.lock.json").read_text())
    dependencies = json.loads((ROOT / "runner/package.json").read_text())["dependencies"]
    for item in lock["providers"].values():
        if item.get("npm_package"):
            assert dependencies[item["npm_package"]] == item["version"]
        else:
            assert item["sha512"] == json.loads((ROOT / "runner/agy.lock.json").read_text())["sha512"]
    catalog = ROOT / "runner/agentic_runner/codex-models.json"
    worker_dependencies = json.loads((ROOT / "runner/worker-dependencies.lock.json").read_text())
    runner_dockerfile = (ROOT / "runner/Dockerfile").read_text()
    for package, version in worker_dependencies["packages"].items():
        assert package + "=" + version in runner_dockerfile, "Worker package version drift: " + package
    worker_source = json.loads((ROOT / "runner/coding-source.lock.json").read_text())
    assert worker_source["repository"] == "https://github.com/TED-H-com/agentic-smart-home.git"
    assert worker_source["repo_id"] == "agentic-home" and re.fullmatch(r"[a-f0-9]{40}", worker_source["commit"])
    assert hashlib.sha256(catalog.read_bytes()).hexdigest() == lock["codex_model_catalog"]["packaged_sha256"]
    for model in json.loads(catalog.read_text())["models"]:
        assert model["apply_patch_tool_type"] is None and not model["experimental_supported_tools"]
    for app in ("controller", "runner", "bridge"):
        config = yaml.safe_load((ROOT / app / "config.yaml").read_text())
        assert config["version"] == release, "App release version drift: " + app
        assert config["arch"] == ["amd64"] and config["ingress"] and config["apparmor"]
        assert not any(config.get(flag) for flag in ("host_network", "host_pid", "host_dbus", "full_access", "docker_api"))
        assert "map" not in config
        assert config.get("ports", {}) == ({"8443/tcp": 8443} if app == "runner" else {})
        assert bool(config["homeassistant_api"]) == (app == "controller")
        assert bool(config["hassio_api"]) == (app == "controller")
        if app == "runner":
            assert config["privileged"] == ["SYS_ADMIN"] and config["tmpfs"] is False
            assert config["devices"] == ["/dev/fuse"]
            assert config["timeout"] == 60 and "coding/*" in config["backup_exclude"]
            assert (ROOT / "runner/apparmor.txt").is_file(), "Home worker confinement profile missing"
        for line in (ROOT / app / "Dockerfile").read_text().splitlines():
            if line.startswith("FROM "):
                assert "@sha256:" in line
    assert (ROOT / "bridge/requirements.txt").read_bytes() == (ROOT / "controller/requirements.txt").read_bytes()
    for asset in [ROOT / "controller/static/index.html", *(ROOT / "controller/static/assets").glob("*")]:
        assert (ROOT / "bridge/static" / asset.relative_to(ROOT / "controller/static")).read_bytes() == asset.read_bytes(), "Bridge UI differs from compiled product UI"
    assert (ROOT / "controller/static/index.html").is_file(), "Build the UI first"
    markdown = [*ROOT.glob("*.md"), *ROOT.glob("docs/*.md"), *ROOT.glob("docs/v3/*.md"),
                *ROOT.glob("controller/*.md"), *ROOT.glob("runner/*.md"), *ROOT.glob("hermes/*.md"), *ROOT.glob("bridge/*.md")]
    for path in markdown:
        for target in re.findall(r"\]\(([^\s)]+)\)", path.read_text()):
            if "://" not in target and not target.startswith("#"):
                assert (path.parent / target.split("#")[0]).exists(), f"Broken link in {path.name}: {target}"
    hermes = json.loads((ROOT / "hermes/hermes.lock.json").read_text())
    assert re.fullmatch(r"[a-f0-9]{40}", hermes["commit"])
    assert re.fullmatch(r"[a-f0-9]{64}", hermes["sha256"])
    assert hermes["commit"] in hermes["archive_url"]
    assert (ROOT / "hermes/Dockerfile").read_text().splitlines()[0] == "FROM " + hermes["base_image"]
    print("App packages, pinned dependencies, tool catalog, compiled UI and document links verified")


if __name__ == "__main__":
    main()
