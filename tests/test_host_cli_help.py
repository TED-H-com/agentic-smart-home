"""CLI command scopes and automatic wired-NIC request semantics."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from agentic_host import cli


@pytest.mark.parametrize("command", [None, "configure_lan", "ha-lan", "status", "snapshot"])
def test_lan_address_policy_only_in_network_select_help(command, capsys):
    with pytest.raises(SystemExit) as result:
        cli.argument_parser().parse_args(([command] if command else []) + ["--help"])
    assert result.value.code == 0
    assert "--lan" not in capsys.readouterr().out


def test_network_select_help_explains_host_address_policy(capsys):
    with pytest.raises(SystemExit) as result:
        cli.argument_parser().parse_args(["network-select", "--help"])
    assert result.value.code == 0
    assert "--lan {dhcp,reservation}" in capsys.readouterr().out


@pytest.mark.parametrize("arguments", [
    ["configure_lan", "--lan", "dhcp"], ["status", "--remote", "disabled"],
    ["snapshot", "--snapshot", "old"], ["start", "--interface-id", "a" * 64],
    ["configure_lan", "--disable-lan", "--interface-id", "a" * 64],
])
def test_unrelated_or_conflicting_flags_fail_before_broker(arguments, monkeypatch):
    monkeypatch.setattr(cli, "call", lambda *args: pytest.fail("must not contact broker"))
    monkeypatch.setattr(sys, "argv", ["agentic-home", *arguments])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 2


@pytest.mark.parametrize("flags,expected", [([], {}), (["--disable-lan"], {"interface_id": None}),
                                         (["--interface-id", "a" * 64], {"interface_id": "a" * 64})])
def test_connect_cli_preserves_auto_select_vs_explicit_disable(flags, expected, monkeypatch, capsys):
    requests = []
    def broker(method, path, body):
        assert method == "POST" and path == "/actions"
        requests.append(body)
        return {"id": body["id"], "request": body, "state": "succeeded", "result": {}}
    monkeypatch.setattr(cli, "call", broker)
    monkeypatch.setattr(sys, "argv", ["agentic-home", "configure_lan", *flags])
    cli.main()
    assert len(requests) == 1
    assert {key: value for key, value in requests[0].items() if key == "interface_id"} == expected
    assert requests[0]["action"] == "configure_lan"
    capsys.readouterr()
