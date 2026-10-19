"""Run real Ubuntu generators against disposable files, without systemd or HAOS."""
import configparser
import json
import subprocess
import tempfile
from pathlib import Path

from agentic_host.appliance_network import reconcile_network
from agentic_host.appliance_power import reconcile_power


def main():
    results = {}
    for renderer in ("networkd", "NetworkManager"):
        with tempfile.TemporaryDirectory(dir="/scratch") as directory:
            root = Path(directory)
            netplan = root / "etc/netplan"
            netplan.mkdir(parents=True)
            (netplan / "50-cloud-init.yaml").write_text(
                "network:\n  version: 2\n  renderer: " + renderer +
                "\n  ethernets:\n    enp1s0:\n      match:\n        macaddress: 02:00:00:00:00:01\n"
                "      dhcp4: true\n")
            report = reconcile_network(root=root)
            assert report["verified"], report
            if renderer == "networkd":
                generated = (root / "run/systemd/network/10-netplan-enp1s0.network").read_text()
                # Netplan omits networkd's default DUID; explicit YAML pins that policy.
                assert "ClientIdentifier=mac" not in generated, generated
                assert "dhcp-identifier: duid" in (netplan / "99-agentic-home-appliance.yaml").read_text()
                links = "\n".join(path.read_text() for path in (root / "run/systemd/network").glob("*.link"))
                assert "WakeOnLan=magic" in links, links
            else:
                path = next((root / "run/NetworkManager/system-connections").glob("*.nmconnection"))
                parser = configparser.ConfigParser()
                parser.read(path)
                assert parser["ethernet"]["wake-on-lan"] == "64", dict(parser["ethernet"])
                assert parser["ipv4"]["dhcp-client-id"] == "mac", dict(parser["ipv4"])
            second = reconcile_network(root=root)
            assert second["verified"] and not second["changed"], second
            results[renderer] = "generated_config_and_idempotency_passed"
    # Real dconf compilation accepts defaults and locks; no session is started.
    reconcile_power({"appliance_lid": True, "desktop": True}, runtime=False)
    subprocess.run(["dconf", "update"], check=True, timeout=10)
    assert Path("/etc/dconf/db/local").is_file()
    results["dconf"] = "compiled_defaults_and_locks"
    results["scope"] = "isolated_ubuntu_utilities_no_physical_hardware"
    results["versions"] = subprocess.check_output(
        ["dpkg-query", "-W", "netplan.io", "systemd", "network-manager", "dconf-cli"], text=True).strip()
    print(json.dumps(results))


if __name__ == "__main__":
    main()
