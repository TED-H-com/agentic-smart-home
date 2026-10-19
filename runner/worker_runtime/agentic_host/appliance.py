"""Reconcile only fixed OS policy shipped in the trusted installed Host product.

Controller source bundles are development inputs, never root activation inputs.
This module runs only after the Host itself has been installed/updated.
"""
import json
import time
from pathlib import Path

from .appliance_power import reconcile_power
from .manager import atomic


def reconcile_appliance(manager, root=Path("/")):
    root = Path(root)
    profile_path = root / "etc/agentic-home/profile.json"
    if not profile_path.is_file():
        return {"state": "not_installed"}
    profile = json.loads(profile_path.read_text())
    from .appliance_network import reconcile_network
    report = {"observed_at": time.time(), "scope": "installed_host_os_policy", "checks": {}}
    for name, operation in (
        ("power", lambda: reconcile_power(profile, root=root, runner=manager.run)),
        ("network", lambda: reconcile_network(root=root, runner=manager.run)),
    ):
        try:
            report["checks"][name] = operation()
        except Exception:
            # Do not log raw subprocess stderr, profile content or network credentials.
            report["checks"][name] = {"verified": False, "error": "appliance_" + name + "_unavailable"}
    report["state"] = "verified" if all(value.get("verified") or value.get("applicable") is False
                                               for value in report["checks"].values()) else "needs_attention"
    atomic(manager.root / "appliance-reconcile.json", report)
    return report
