"""Run as the disposable Linux owner; exercise real HTTP auth without printing secrets."""
import http.cookiejar
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

OBSERVATIONS = []
STAGE = "initializing"
DIAGNOSTICS = {}


def require(condition, stage):
    global STAGE
    STAGE = stage
    assert condition, stage


def verify_workers(hermes):
    """Boundary readiness is separate from role authentication and inference."""
    workers = {worker["worker_id"]: worker for worker in hermes["workers"]}
    require(set(workers) == {"host-coder", "home-coder"}, "worker_catalog")
    host, home = workers["host-coder"], workers["home-coder"]
    DIAGNOSTICS.update(host_boundary_ready=host.get("available") is True,
                       home_boundary_ready=home.get("available") is True)
    require(host["work_contract_major"] == 1 and host["runtime_location"] == "host_container"
            and host["observed_from"] == "paired_runner", "host_worker_identity")
    require(host["available"] is True and host["state"] == "ready" and host["reason"] is None
            and set(host["modes"]) == {"research", "development"}, "host_worker_boundary")
    require(len(host["providers"]) == 1, "host_worker_provider_catalog")
    provider = host["providers"][0]
    DIAGNOSTICS["host_role_needs_auth"] = provider.get("credential_state") == "needs_auth"
    require(provider["id"] == "codex" and provider["supported"] is True
            and set(provider["auth_methods"]) == {"device_code", "api_key"}
            and set(provider["models"]) == {
                "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"},
            "host_worker_codex_contract")
    require(provider["credential_state"] == "needs_auth", "host_worker_requires_role_login")
    require(home["work_contract_major"] == 1 and home["runtime_location"] == "haos_app"
            and home["available"] is False and home["state"] == "blocked"
            and home["reason"] == "runner_unavailable" and home["observed_from"] == "controller"
            and home["providers"] == [] and home["modes"] == [], "home_worker_not_paired")


def verify_auth_blocked(job):
    require(job["worker_id"] == "host-coder" and job["provider"] == "codex"
            and job["state"] == "blocked" and job["reason"] == "coding_needs_auth"
            and job["execution_started"] is False and job["stop_confirmed"] is True
            and job["artifacts"] == [] and job["evidence_ids"] == [], "coding_without_login_blocked")


def probe():
    global STAGE
    base = "http://localhost:8099"
    csrf = ""
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(path, data=None, key=None):
        global STAGE
        STAGE = "http_" + path.removeprefix("/api/").replace("/", "_")
        body = json.dumps(data).encode() if data is not None else None
        headers = {"Origin": base, "Content-Type": "application/json"}
        if csrf:
            headers["X-Agentic-CSRF"] = csrf
        if key:
            headers["Idempotency-Key"] = key
        req = urllib.request.Request(base + path, data=body,
                                     headers=headers)
        try:
            with opener.open(req, timeout=90) as response:
                OBSERVATIONS.append({"path": path, "status": response.status})
                return json.load(response)
        except urllib.error.HTTPError as exc:
            OBSERVATIONS.append({"path": path, "status": exc.code})
            raise

    sys.path.insert(0, "/opt/agentic-home/host")
    from agentic_host.desktop import issue_ticket
    ticket = issue_ticket()
    require(bool(ticket), "desktop_ticket_issued")
    require(request("/api/host/desktop-login", {"ticket": ticket})["authenticated"], "desktop_ticket_login")
    try:
        request("/api/host/desktop-login", {"ticket": ticket})
    except urllib.error.HTTPError as exc:
        require(exc.code == 403, "desktop_ticket_replay_rejected")
    else:
        raise AssertionError("Consumed desktop ticket was reused")
    key = Path("/var/lib/agentic-home/controller/access-key").read_text().strip()
    require(request("/api/host/login", {"key": key})["authenticated"], "owner_key_login")
    session = request("/api/session")
    require(session["mode"] == "host" and session["principal"] == "host-owner" and session["csrf"],
            "owner_session")
    csrf = session["csrf"]
    host = request("/api/host/status")
    bridge_contract = host.get("controller_update", {}).get("ingress_bridge_contract")
    require(type(bridge_contract) is int and bridge_contract == 1, "fixed_host_bridge_update_contract")
    status = request("/api/status")
    require(status["runner"]["identity"] == "agentic-home-runner", "runner_identity")
    require(status["onboarding"]["runner_paired"] is True, "automatic_runner_pairing")
    require({p["provider"] for p in status["runner"]["providers"]} == {"codex", "claude", "agy", "grok"},
            "operator_provider_catalog")
    hermes = request("/api/hermes")
    DIAGNOSTICS.update(hermes_needs_auth=hermes["runtime"].get("state") == "needs_auth",
                       hermes_inference_verified=hermes["runtime"].get("auth_verified_at") is not None)
    require(hermes["runtime"]["state"] == "needs_auth" and hermes["runtime"]["auth_verified_at"] is None,
            "hermes_requires_login")
    verify_workers(hermes)
    bootstrap = request("/api/bootstrap")
    require(bootstrap["state"] == "idle" and len(bootstrap["steps"]) == 9, "bootstrap_get_stays_idle")
    require(not host["provisioned"], "home_not_provisioned")
    body = {"worker_id": "host-coder", "provider": "codex", "mode": "research",
            "goal": "Disposable installation check: no role login must block execution", "repo_id": "agentic-home"}
    blocked = request("/api/worker/jobs", body, key="installed-coding-without-login")
    verify_auth_blocked(blocked)
    replay = request("/api/worker/jobs", body, key="installed-coding-without-login")
    verify_auth_blocked(replay)
    require(replay["id"] == blocked["id"], "blocked_coding_request_idempotent")
    require(request("/api/workers")["active_job_id"] is None, "blocked_coding_has_no_writer_lease")
    return {"host_login": "passed", "authenticated_host_api": "passed", "automatic_runner_pairing": "passed",
            "fixed_host_bridge_update_contract": bridge_contract,
            "desktop_single_use_login": "passed", "bootstrap_get_has_no_side_effect": "passed",
            "vm_observed": host["vm"], "providers": [{"provider": p["provider"], "status": p["status"]}
                                                    for p in status["runner"]["providers"]],
            "hermes_service": "needs_auth", "coding_workers": {"host": "ready_needs_auth", "home": "not_paired"},
            "coding_without_login": "blocked_no_execution", "cloud_login": "not_run"}


if __name__ == "__main__":
    try:
        print(json.dumps({**probe(), "http_observations": OBSERVATIONS, "diagnostics": DIAGNOSTICS}, indent=2))
    except Exception as exc:
        # Never print request bodies, access keys, cookies or raw exception text.
        print(json.dumps({"result": "failed", "error_type": type(exc).__name__, "stage": STAGE,
                          "http_observations": OBSERVATIONS, "diagnostics": DIAGNOSTICS}, indent=2))
        raise SystemExit(1) from None
