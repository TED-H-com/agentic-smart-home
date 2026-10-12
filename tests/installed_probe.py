"""Run as the disposable Linux owner; exercise real HTTP auth without printing secrets."""
import http.cookiejar
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


def probe():
    base = "http://localhost:8099"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(path, data=None):
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(base + path, data=body,
                                     headers={"Origin": base, "Content-Type": "application/json"})
        with opener.open(req, timeout=90) as response:
            return json.load(response)

    sys.path.insert(0, "/opt/agentic-home/host")
    from agentic_host.desktop import issue_ticket
    ticket = issue_ticket()
    assert ticket
    assert request("/api/host/desktop-login", {"ticket": ticket})["authenticated"]
    try:
        request("/api/host/desktop-login", {"ticket": ticket})
    except urllib.error.HTTPError as exc:
        assert exc.code == 401
    else:
        raise AssertionError("Consumed desktop ticket was reused")
    key = Path("/var/lib/agentic-home/controller/access-key").read_text().strip()
    assert request("/api/host/login", {"key": key})["authenticated"]
    session = request("/api/session")
    assert session["mode"] == "host" and session["principal"] == "host-owner" and session["csrf"]
    host = request("/api/host/status")
    status = request("/api/status")
    assert status["runner"]["identity"] == "agentic-home-runner"
    assert status["onboarding"]["runner_paired"] is True
    assert {p["provider"] for p in status["runner"]["providers"]} == {"codex", "claude", "agy", "grok"}
    hermes = request("/api/hermes")
    assert hermes["runtime"]["state"] == "needs_auth" and hermes["runtime"]["auth_verified_at"] is None
    assert all(not worker["available"] for worker in hermes["workers"])
    bootstrap = request("/api/bootstrap")
    assert bootstrap["state"] == "idle" and len(bootstrap["steps"]) == 9
    assert not host["provisioned"]
    return {"host_login": "passed", "authenticated_host_api": "passed", "automatic_runner_pairing": "passed",
            "desktop_single_use_login": "passed", "bootstrap_get_has_no_side_effect": "passed",
            "vm_observed": host["vm"], "providers": [{"provider": p["provider"], "status": p["status"]}
                                                    for p in status["runner"]["providers"]],
            "hermes_service": "needs_auth", "coding_workers": "blocked", "cloud_login": "not_run"}


if __name__ == "__main__":
    print(json.dumps(probe(), indent=2))
