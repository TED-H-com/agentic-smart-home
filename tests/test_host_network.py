import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from agentic_host.manager import HostError
from agentic_host.network import Network


@pytest.fixture
def network(tmp_path):
    calls = []
    raw = {"BackendState": "NeedsLogin", "AuthURL": "https://login.tailscale.com/a/12345678",
           "Self": {"UserID": 1}, "User": {"1": {"LoginName": "fixture@example.test"}}, "private": "never_return"}
    def run(*args, **kwargs):
        calls.append(args)
        if args == ("tailscale", "logout"):
            raw["BackendState"] = "NeedsLogin"
        return SimpleNamespace(stdout=json.dumps(raw), returncode=0)
    instance = Network(SimpleNamespace(root=tmp_path, run=run), available=lambda _: "/usr/bin/tailscale")
    return instance, raw, calls


def test_choices_only_record_dhcp_and_never_mutate_host_routes(network):
    service, _, calls = network
    result = service.choose({"lan": "reservation", "remote": "disabled"})
    assert result["lan"] == "reservation" and result["browser_direct"] is False
    assert calls == [("tailscale", "status", "--json", "--peers=false")]
    for bad in ({"lan": "static", "remote": "disabled"}, {"lan": "dhcp", "remote": "tailscale", "authkey": "secret"}):
        with pytest.raises(HostError):
            service.choose(bad)


def test_tailscale_requires_explicit_selection_and_never_adopts_existing_identity(network):
    service, raw, calls = network
    with pytest.raises(HostError, match="not_selected"):
        service.login()
    service.choose({"lan": "dhcp", "remote": "tailscale"})
    raw["BackendState"] = "Running"
    with pytest.raises(HostError, match="existing_identity"):
        service.login()
    assert not any("up" in call for call in calls)


def test_tailscale_login_flags_redaction_tunnel_and_actual_revocation(network, monkeypatch):
    service, raw, calls = network
    service.choose({"lan": "dhcp", "remote": "tailscale"})
    assert service.login()["tailscale"]["login_url"].startswith("https://login.tailscale.com/a/")
    up = next(call for call in calls if "up" in call)
    assert {"--accept-dns=false", "--accept-routes=false", "--ssh=false", "--exit-node=", "--advertise-routes="} <= set(up)
    raw.update(BackendState="Running", TailscaleIPs=["100.77.2.3"])
    monkeypatch.setattr("agentic_host.network.pwd.getpwuid", lambda _: SimpleNamespace(pw_name="owner"))
    result = service.status()
    assert "owner@100.77.2.3" in result["tunnel_command"]
    assert "-L 127.0.0.2:8100:127.0.0.2:8100" in result["tunnel_command"]
    assert "never_return" not in json.dumps(result)
    result = service.choose({"lan": "dhcp", "remote": "disabled"})
    assert ("tailscale", "logout") in calls and result["managed"] is False


def test_missing_package_and_untrusted_login_url_are_honest(network):
    service, raw, _ = network
    raw["AuthURL"] = "https://evil.test/a/code"
    assert "login_url" not in service.tailscale()
    service.available = lambda _: None
    assert service.status()["tailscale"]["state"] == "not_installed"
    service.choose({"lan": "dhcp", "remote": "tailscale"})
    with pytest.raises(HostError, match="package_required"):
        service.login()


def test_fresh_tailscale_nullable_fields_and_stopped_existing_identity(network):
    service, raw, _ = network
    raw.update(Self=None, User=None, TailscaleIPs=None)
    assert service.status()["tailscale"]["state"] == "NeedsLogin"
    service.choose({"lan": "dhcp", "remote": "tailscale"})
    raw["BackendState"] = "Stopped"
    with pytest.raises(HostError, match="existing_identity"):
        service.login()
