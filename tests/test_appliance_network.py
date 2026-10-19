"""Network policy tests stage a disk filesystem and inject all OS commands."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from agentic_host.appliance_network import (  # noqa: E402
    LINK,
    NM_DEFAULT,
    OVERLAY,
    POLICY,
    netplan_config,
    network_overlay,
    reconcile_network,
)
from agentic_host.manager import HostError  # noqa: E402


def write(root, path, content):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return target


def ethernet(root, name="enp1s0"):
    folder = root / "sys/class/net" / name
    (folder / "device").mkdir(parents=True)
    write(root, f"sys/class/net/{name}/type", "1\n")
    write(root, f"sys/class/net/{name}/address", "00:11:22:33:44:55\n")


def networkd(root):
    write(root, "etc/netplan/50-cloud-init.yaml", """network:
  version: 2
  ethernets:
    enp1s0:
      dhcp4: true
      nameservers:
        addresses: [192.0.2.1]
""")


class Runner:
    def __init__(self, *, capable=True, enabled=False, fail_enable=False, generate_fails=False):
        self.calls = []
        self.capable = capable
        self.enabled = enabled
        self.fail_enable = fail_enable
        self.generate_fails = generate_fails
        self.nm_saved = False
        self.client_id = "mac"

    def __call__(self, *args, **kwargs):
        assert kwargs == {"check": False, "timeout": 10}
        self.calls.append(args)
        if args[0] == "nmcli" and "-g" in args:
            assert args[1:3] == ("--escape", "no")
            args = (args[0], *args[3:])
        code, output = 0, ""
        if args[:2] == ("netplan", "generate"):
            code = int(self.generate_fails)
        elif args[:2] == ("ethtool", "-s"):
            self.enabled = not self.fail_enable
        elif args[0] == "ethtool":
            output = f"Supports Wake-on: {'g' if self.capable else 'd'}\nWake-on: {'g' if self.enabled else 'd'}\n"
        elif args[:3] == ("nmcli", "-g", "GENERAL.CON-UUID"):
            output = "11111111-2222-3333-4444-555555555555\n"
        elif args[:3] == ("nmcli", "-g", "ipv4.dhcp-client-id"):
            output = self.client_id
        elif args[:3] == ("nmcli", "-g", "DHCP4.OPTION"):
            output = "dhcp_client_identifier = 01:00:11:22:33:44:55\n"
        elif args[:3] == ("nmcli", "-g", "802-3-ethernet.wake-on-lan"):
            output = "64 (magic)\n" if self.nm_saved else "0\n"
        elif args[:3] == ("nmcli", "connection", "modify"):
            if args[-2] == "ipv4.dhcp-client-id":
                self.client_id = args[-1]
            else:
                self.nm_saved = True
        else:
            pytest.fail(f"Unexpected OS command: {args}")
        return SimpleNamespace(returncode=code, stdout=output)


def test_missing_ethtool_observation_cannot_be_verified_as_unsupported(tmp_path):
    networkd(tmp_path)
    ethernet(tmp_path)
    runner = Runner()
    def unavailable(*args, **kwargs):
        if args[0] == "ethtool":
            return SimpleNamespace(returncode=1, stdout="")
        return runner(*args, **kwargs)
    report = reconcile_network(tmp_path, runner=unavailable)
    assert not report["verified"]
    assert "wol_observation_unavailable:enp1s0" in report["warnings"]


def test_networkd_dhcp_duid_pinned_before_desktop_changes_default(tmp_path):
    networkd(tmp_path)
    first = reconcile_network(tmp_path, runtime=False)
    assert first["renderer"] == "networkd"
    write(tmp_path, "usr/lib/netplan/00-network-manager-all.yaml", "network:\n  renderer: NetworkManager\n")
    second = reconcile_network(tmp_path, runtime=False)
    assert second["renderer"] == "networkd"
    overlay = yaml.safe_load((tmp_path / "etc/netplan" / OVERLAY).read_text())["network"]
    assert overlay["renderer"] == "networkd"
    assert overlay["ethernets"]["enp1s0"] == {
        "renderer": "networkd", "wakeonlan": True, "dhcp-identifier": "duid"}
    merged = netplan_config(tmp_path)
    assert merged["ethernets"]["enp1s0"]["nameservers"] == {"addresses": ["192.0.2.1"]}


def test_explicit_networkd_mac_identity_is_preserved():
    overlay = network_overlay({"ethernets": {"eth0": {"dhcp4": True, "dhcp-identifier": "mac"}}}, "networkd")
    assert overlay["network"]["ethernets"]["eth0"]["dhcp-identifier"] == "mac"


def test_netplan_source_precedence_and_names_not_credentials(tmp_path):
    write(tmp_path, "usr/lib/netplan/20-network.yaml", "network:\n  renderer: NetworkManager\n")
    write(tmp_path, "etc/netplan/20-network.yaml", "network:\n  renderer: networkd\n")
    assert netplan_config(tmp_path)["renderer"] == "networkd"
    write(tmp_path, "run/netplan/20-network.yaml", "network:\n  renderer: NetworkManager\n")
    assert netplan_config(tmp_path)["renderer"] == "NetworkManager"


def test_existing_nm_pins_client_identity_and_sets_magic_without_reconnect(tmp_path):
    networkd(tmp_path)
    ethernet(tmp_path)
    write(tmp_path, "usr/lib/netplan/00-network-manager-all.yaml", "network:\n  renderer: NetworkManager\n")
    keyfile = write(tmp_path, "run/NetworkManager/system-connections/netplan-enp1s0.nmconnection",
                    "[connection]\ninterface-name=enp1s0\n[ipv4]\ndhcp-client-id=01:00:11:22:33:44:55\n")
    runner = Runner()
    runner.client_id = "01:00:11:22:33:44:55"
    report = reconcile_network(tmp_path, runner=runner)
    assert report["renderer"] == "NetworkManager"
    assert report["verified"]
    assert report["interfaces"][0]["wol_enabled"]
    overlay = yaml.safe_load((tmp_path / "etc/netplan" / OVERLAY).read_text())["network"]
    setting = overlay["ethernets"]["enp1s0"]
    assert setting["networkmanager"]["passthrough"]["ipv4.dhcp-client-id"] == "01:00:11:22:33:44:55"
    assert "dhcp-identifier" not in setting
    assert runner.nm_saved
    keyfile.unlink()
    second = reconcile_network(tmp_path, runner=runner)
    assert second["changed"] is False
    assert second["verified"]
    assert len([call for call in runner.calls if call[:3] == ("nmcli", "connection", "modify")]) == 1
    forbidden = {"apply", "restart", "reapply", "up", "down", "disconnect"}
    assert all(not forbidden.intersection(call) for call in runner.calls)


def test_staging_has_no_runtime_effect_and_is_idempotent(tmp_path):
    networkd(tmp_path)
    runner = Runner()
    first = reconcile_network(tmp_path, runner=runner, runtime=False)
    state = json.loads((tmp_path / POLICY).read_text())
    paths = [path for path in tmp_path.rglob("*") if path.is_file()]
    times = {path: path.stat().st_mtime_ns for path in paths}
    second = reconcile_network(tmp_path, runner=runner, runtime=False)
    assert first["changed"] is True
    assert second["changed"] is False
    assert times == {path: path.stat().st_mtime_ns for path in paths}
    assert state["renderer"] == "networkd"
    assert runner.calls == []
    assert (tmp_path / "etc/systemd/network/99-default.link.d/60-agentic-home-wol.conf").read_text() == LINK
    assert (tmp_path / "etc/NetworkManager/conf.d/90-agentic-home-wol.conf").read_text() == NM_DEFAULT
    assert (tmp_path / "etc/netplan" / OVERLAY).stat().st_mode & 0o777 == 0o600


def test_readback_failure_blocks_verification(tmp_path):
    networkd(tmp_path)
    ethernet(tmp_path)
    result = reconcile_network(tmp_path, runner=Runner(fail_enable=True))
    assert result["verified"] is False
    assert result["warnings"] == ["wol_readback_failed:enp1s0"]


@pytest.mark.parametrize("physical", [True, False])
def test_unsupported_or_absent_wol_is_warning_not_install_blocker(tmp_path, physical):
    networkd(tmp_path)
    if physical:
        ethernet(tmp_path)
    result = reconcile_network(tmp_path, runner=Runner(capable=False))
    assert result["verified"] is True
    assert result["warnings"]


def test_failed_netplan_generation_restores_previous_inputs(tmp_path):
    networkd(tmp_path)
    existing = write(tmp_path, "etc/netplan/" + OVERLAY, "# previous\nnetwork:\n  renderer: networkd\n")
    existing.chmod(0o640)
    result = reconcile_network(tmp_path, runner=Runner(generate_fails=True))
    assert result["verified"] is False
    assert result["changed"] is False
    assert result["warnings"] == ["network_config_verification_failed"]
    assert existing.read_text().startswith("# previous")
    assert existing.stat().st_mode & 0o777 == 0o640
    assert not (tmp_path / POLICY).exists()


def test_mixed_renderer_is_not_silently_migrated(tmp_path):
    write(tmp_path, "etc/netplan/20-mixed.yaml", """network:
  ethernets:
    eth0: {renderer: networkd, dhcp4: true}
  wifis:
    wlan0: {renderer: NetworkManager, dhcp4: true}
""")
    with pytest.raises(HostError, match="Mixed network renderers"):
        reconcile_network(tmp_path, runtime=False)
    assert not (tmp_path / POLICY).exists()


def test_wifi_secrets_routes_and_static_addresses_never_copied_into_policy(tmp_path):
    write(tmp_path, "etc/netplan/50-wifi.yaml", """network:
  version: 2
  renderer: NetworkManager
  wifis:
    wlan0:
      dhcp4: true
      access-points:
        example:
          password: private-test-value
      routes: [{to: default, via: 192.0.2.1}]
  ethernets:
    enp1s0:
      addresses: [192.0.2.4/24]
""")
    reconcile_network(tmp_path, runtime=False)
    text = (tmp_path / POLICY).read_text()
    assert "private-test-value" not in text
    assert "routes" not in text
    assert "addresses" not in text
    assert "dhcp-identifier" not in text


def test_pending_network_policy_waits_for_actual_reboot(tmp_path):
    networkd(tmp_path)
    boot = write(tmp_path, "proc/sys/kernel/random/boot_id", "first-boot-id\n")
    first = reconcile_network(tmp_path, runtime=False)
    second = reconcile_network(tmp_path, runtime=False)
    assert first["reboot_required"] is True
    assert second["reboot_required"] is True
    assert second["changed"] is False
    boot.write_text("second-boot-id\n")
    assert reconcile_network(tmp_path, runtime=False)["reboot_required"] is False


@pytest.mark.parametrize("policy", ["{bad", "[]", '{"version": 1, "renderer": "bad", "overlay": {}}'])
def test_malformed_persisted_policy_fails_closed(tmp_path, policy):
    networkd(tmp_path)
    write(tmp_path, POLICY, policy)
    with pytest.raises(HostError, match="Invalid persisted"):
        reconcile_network(tmp_path, runtime=False)
    assert not (tmp_path / "etc/netplan" / OVERLAY).exists()


def test_later_override_is_detected_and_policy_not_claimed_verified(tmp_path):
    networkd(tmp_path)
    write(tmp_path, "etc/netplan/zz-override.yaml", "network:\n  ethernets:\n    enp1s0:\n      wakeonlan: false\n")
    result = reconcile_network(tmp_path, runner=Runner())
    assert result["verified"] is False
    assert result["warnings"] == ["network_config_verification_failed"]
    assert not (tmp_path / "etc/netplan" / OVERLAY).exists()


@pytest.mark.parametrize("content", ["network: [", "network: {ethernets: []}", "network: {ethernets: {eth0: false}}"])
def test_malformed_yaml_is_host_error(tmp_path, content):
    write(tmp_path, "etc/netplan/50-invalid.yaml", content)
    with pytest.raises(HostError, match="Invalid netplan"):
        reconcile_network(tmp_path, runtime=False)


def test_nm_only_profile_pins_observed_wire_id_without_reapply(tmp_path):
    ethernet(tmp_path)
    write(tmp_path, "usr/lib/netplan/00-network-manager-all.yaml", "network:\n  renderer: NetworkManager\n")
    runner = Runner()
    runner.client_id = ""
    result = reconcile_network(tmp_path, runner=runner)
    assert result["verified"]
    assert runner.client_id == "01:00:11:22:33:44:55"
    assert any(call[-2:] == ("ipv4.dhcp-client-id", "01:00:11:22:33:44:55") for call in runner.calls)
    assert not any("reapply" in call or "up" in call or "apply" in call for call in runner.calls)


def test_legacy_pre_reboot_desktop_install_preserves_running_networkd_identity(tmp_path):
    networkd(tmp_path)
    ethernet(tmp_path)
    write(tmp_path, "sys/class/net/enp1s0/ifindex", "2\n")
    write(tmp_path, "run/systemd/netif/links/2", "ADMIN_STATE=configured\nNETWORK_FILE=/run/systemd/network/10-netplan-enp1s0.network\n")
    write(tmp_path, "usr/lib/netplan/00-network-manager-all.yaml", "network:\n  renderer: NetworkManager\n")
    result = reconcile_network(tmp_path, runtime=False)
    assert result["renderer"] == "networkd"
    overlay = yaml.safe_load((tmp_path / "etc/netplan" / OVERLAY).read_text())
    assert overlay["network"]["ethernets"]["enp1s0"]["dhcp-identifier"] == "duid"


def test_unsupported_nm_wol_does_not_modify_unsupported_connection_property(tmp_path):
    networkd(tmp_path)
    ethernet(tmp_path)
    write(tmp_path, "usr/lib/netplan/00-network-manager-all.yaml", "network:\n  renderer: NetworkManager\n")
    runner = Runner(capable=False)
    result = reconcile_network(tmp_path, runner=runner)
    assert result["verified"]
    assert result["warnings"] == ["wol_not_supported:enp1s0"]
    assert not runner.nm_saved


@pytest.mark.parametrize("dhcp4", [True, False])
def test_nm_ethernet_explicit_magic_passthrough_even_without_dhcp(dhcp4):
    config = {"ethernets": {"eth0": {"dhcp4": dhcp4}}}
    setting = network_overlay(config, "NetworkManager")["network"]["ethernets"]["eth0"]
    assert setting["wakeonlan"] is True
    assert setting["networkmanager"]["passthrough"]["ethernet.wake-on-lan"] == "64"
    assert ("ipv4.dhcp-client-id" in setting["networkmanager"]["passthrough"]) == dhcp4


@pytest.mark.parametrize("configured_id", ["custom-client-id", ""])
def test_active_nm_identity_precedes_generation_and_stale_policy(tmp_path, configured_id):
    networkd(tmp_path)
    ethernet(tmp_path)
    write(tmp_path, "usr/lib/netplan/00-network-manager-all.yaml", "network:\n  renderer: NetworkManager\n")
    # Simulate an earlier policy and stale saved keyfile choosing MAC.
    reconcile_network(tmp_path, runtime=False)
    write(tmp_path, "run/NetworkManager/system-connections/stale.nmconnection",
          "[connection]\ninterface-name=enp1s0\n[ipv4]\ndhcp-client-id=stale-id\n")
    runner = Runner()
    runner.client_id = configured_id
    expected = configured_id or "01:00:11:22:33:44:55"
    result = reconcile_network(tmp_path, runner=runner)
    assert result["verified"]
    overlay = yaml.safe_load((tmp_path / "etc/netplan" / OVERLAY).read_text())
    policy = json.loads((tmp_path / POLICY).read_text())
    assert overlay["network"]["ethernets"]["enp1s0"]["networkmanager"]["passthrough"]["ipv4.dhcp-client-id"] == expected
    assert policy["overlay"] == overlay
    generate_index = next(index for index, call in enumerate(runner.calls) if call[:2] == ("netplan", "generate"))
    assert any("ipv4.dhcp-client-id" in call for call in runner.calls[:generate_index])
    if not configured_id:
        assert any("DHCP4.OPTION" in call for call in runner.calls[:generate_index])
    assert runner.client_id == expected


@pytest.mark.parametrize("field", ["GENERAL.CON-UUID", "ipv4.dhcp-client-id", "DHCP4.OPTION"])
def test_unknown_active_nm_identity_fails_before_policy_mutation(tmp_path, field):
    networkd(tmp_path)
    ethernet(tmp_path)
    write(tmp_path, "usr/lib/netplan/00-network-manager-all.yaml", "network:\n  renderer: NetworkManager\n")
    runner = Runner()
    runner.client_id = ""
    def unavailable(*args, **kwargs):
        if field in args:
            return SimpleNamespace(returncode=1, stdout="")
        return runner(*args, **kwargs)
    result = reconcile_network(tmp_path, runner=unavailable)
    assert not result["verified"]
    assert result["warnings"] == ["dhcp_identity_observation_unavailable:enp1s0"]
    assert not (tmp_path / "etc/netplan" / OVERLAY).exists()
    assert not (tmp_path / POLICY).exists()
    assert not any(call[:2] == ("netplan", "generate") for call in runner.calls)


def test_nm_opaque_netplan_id_matches_active_interface_by_mac(tmp_path):
    ethernet(tmp_path)
    write(tmp_path, "etc/netplan/50-network.yaml", """network:
  version: 2
  renderer: NetworkManager
  ethernets:
    wired-network:
      match: {macaddress: '00:11:22:33:44:55'}
      dhcp4: true
""")
    runner = Runner()
    runner.client_id = "01:aa:bb:cc:dd:ee:ff"
    result = reconcile_network(tmp_path, runner=runner)
    assert result["verified"]
    overlay = yaml.safe_load((tmp_path / "etc/netplan" / OVERLAY).read_text())
    passthrough = overlay["network"]["ethernets"]["wired-network"]["networkmanager"]["passthrough"]
    assert passthrough["ipv4.dhcp-client-id"] == runner.client_id
    assert "\\" not in passthrough["ipv4.dhcp-client-id"]
