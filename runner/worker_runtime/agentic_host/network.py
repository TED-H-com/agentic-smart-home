"""Owner-selected network preferences and fixed Tailscale enrollment.

No static-address edits, subnet routing, Serve/Funnel or remote admin origin.
The existing OpenSSH identity plus a local tunnel preserve both browser origins.
"""
import ipaddress
import json
import pwd
import re
import shutil
import threading
import time

from .manager import HostError, atomic


class Network:
    def __init__(self, manager, available=shutil.which):
        self.manager, self.run, self.available = manager, manager.run, available
        self.path = manager.root / "network-choice.json"
        self.lock = threading.Lock()

    def record(self):
        return json.loads(self.path.read_text()) if self.path.exists() else {"lan": "dhcp", "remote": "disabled", "managed": False}

    def tailscale(self):
        if not self.available("tailscale"):
            return {"state": "not_installed"}
        try:
            result = self.run("tailscale", "status", "--json", "--peers=false", timeout=5, check=False)
            raw = json.loads(result.stdout)
            state = raw.get("BackendState")
            value = {"state": state if state in {"Running", "NeedsLogin", "NeedsMachineAuth", "Stopped"} else "unavailable"}
            address = next((ip for ip in (raw.get("TailscaleIPs") or []) if isinstance(ip, str)
                            and ipaddress.ip_address(ip).version == 4 and ipaddress.ip_address(ip) in ipaddress.ip_network("100.64.0.0/10")), None)
            if address:
                value["address"] = address
            login = raw.get("AuthURL", "")
            if isinstance(login, str) and re.fullmatch(r"https://login\.tailscale\.com/a/[A-Za-z0-9_-]{8,200}", login):
                value["login_url"] = login
            identity = (raw.get("Self") or {}).get("UserID")
            user = (raw.get("User") or {}).get(str(identity), {})
            for source, target in (("LoginName", "identity"), ("DisplayName", "display_name")):
                if isinstance(user.get(source), str):
                    value[target] = user[source][:160]
            return value
        except Exception:
            return {"state": "unavailable"}

    def status(self):
        record = self.record()
        remote = self.tailscale()
        value = {"contract": 1, **record, "tailscale": remote, "admin_origin": "http://localhost:8099",
                 "ha_origin": "http://localhost:8123", "browser_direct": False,
                 "identity_policy": "tailnet_access_and_existing_openssh_account",
                 "rescue": "Local Linux console and localhost Web remain available; no LAN address is changed.",
                 "reservation": "Reserve this host's DHCP lease in your router; the product does not claim to configure the router."}
        if record["remote"] == "tailscale" and remote["state"] == "Running" and remote.get("address"):
            try:
                username = pwd.getpwuid(1000).pw_name
                if re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username):
                    value["tunnel_command"] = ("ssh -N -o ExitOnForwardFailure=yes "
                        "-L 127.0.0.1:8099:127.0.0.1:8099 -L 127.0.0.1:8123:127.0.0.1:8123 "
                        "-L 127.0.0.2:8100:127.0.0.2:8100 "
                        + username + "@" + remote["address"])
            except KeyError:
                pass
        return value

    def choose(self, body):
        if not isinstance(body, dict) or set(body) != {"lan", "remote"} or body["lan"] not in {"dhcp", "reservation"} or body["remote"] not in {"disabled", "tailscale"}:
            raise HostError("network_choice_invalid")
        with self.lock:
            previous = self.record()
            # Disabling a selection must also revoke the managed node; merely
            # hiding the connection form must not retain remote access.
            if body["remote"] == "disabled" and previous.get("managed"):
                self._revoke(previous)
            atomic(self.path, {**previous, **body, "updated_at": time.time()})
        return self.status()

    def login(self):
        with self.lock:
            record = self.record()
            if record["remote"] != "tailscale":
                raise HostError("tailscale_not_selected")
            if not self.available("tailscale"):
                raise HostError("tailscale_package_required")
            observed = self.tailscale()
            if not record.get("managed") and observed["state"] != "NeedsLogin":
                raise HostError("tailscale_existing_identity_not_managed")
            record.update(managed=True, updated_at=time.time())
            atomic(self.path, record)  # Persist intent before enrollment starts.
            self.run("tailscale", "up", "--accept-dns=false", "--accept-routes=false", "--ssh=false",
                     "--advertise-exit-node=false", "--advertise-routes=", "--exit-node=",
                     "--timeout=5s", "--json", timeout=10, check=False)
        return self.status()

    def _revoke(self, record):
        if not record.get("managed"):
            raise HostError("tailscale_existing_identity_not_managed")
        self.run("tailscale", "logout", timeout=15)
        if self.tailscale()["state"] not in {"NeedsLogin", "Stopped"}:
            raise HostError("tailscale_revocation_unconfirmed")
        record.update(remote="disabled", managed=False, revoked_at=time.time())
        atomic(self.path, record)

    def revoke(self):
        with self.lock:
            self._revoke(self.record())
        return self.status()

    def handle(self, method, path, body=None):
        if method == "GET" and path == "/network":
            return 200, self.status()
        if method == "POST" and path == "/network/preferences":
            return 200, self.choose(body)
        if method == "POST" and body == {} and path in {"/network/login", "/network/revoke"}:
            return 200, self.login() if path.endswith("login") else self.revoke()
        raise HostError("network_request_invalid")
