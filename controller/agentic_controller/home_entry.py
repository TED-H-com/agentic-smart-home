"""Read-only handoff to HA's native frontend, independent of coding availability.

Use fresh authenticated HA observations. Completion of our own setup alone does
not prove HA is reachable or that the bound owner is still active. Additional
upstream response fields are accepted; missing required semantics fail closed
for automatic navigation only. No HA URL or bearer crosses this contract.
"""
import asyncio

from .ha import HAError
from .models import HomeEntry


async def home_entry(store, ha, *, timeout=5):
    def result(state, reason=None):
        return HomeEntry(state=state, reason_code=reason)

    record = store.get("bootstrap_v1", "setup")
    if not record or record.get("state") != "completed":
        return result("setup", "setup_incomplete")
    if store.setting("updater_gate"):
        return result("unavailable", "maintenance")
    owner_id = store.setting("owner")
    if not isinstance(owner_id, str) or not owner_id:
        return result("unavailable", "home_not_bound")
    try:
        async with asyncio.timeout(timeout):
            owner = await ha.owner(owner_id)
            if not owner:
                return result("unavailable", "home_identity_changed")
            if (not isinstance(owner, dict) or owner.get("id") != owner_id
                    or owner.get("is_owner") is not True or owner.get("is_active") is not True):
                return result("unsupported", "home_contract_changed")
            core = await ha.request("GET", "/config")
        # These are semantic requirements, not a whitelist of Core versions.
        # A newer HA can remain the daily UI while Agent writes stay gated by
        # their separately tested Core compatibility policy.
        if (not isinstance(core, dict) or not isinstance(core.get("version"), str)
                or not core["version"] or len(core["version"]) > 100
                or not isinstance(core.get("components"), list)
                or not all(isinstance(item, str) for item in core["components"])
                or "frontend" not in core["components"]):
            return result("unsupported", "home_contract_changed")
        if "state" in core and core["state"] != "RUNNING":
            return result("unavailable", "home_unavailable")
    except (HAError, TimeoutError, OSError):
        return result("unavailable", "home_unavailable")
    except (ValueError, TypeError, KeyError, AttributeError):
        return result("unsupported", "home_contract_changed")
    # Authority can change while awaiting network observations.
    current = store.get("bootstrap_v1", "setup")
    if store.setting("owner") != owner_id or not current or current.get("state") != "completed":
        return result("unavailable", "home_identity_changed")
    if store.setting("updater_gate"):
        return result("unavailable", "maintenance")
    return result("ready")


def routes(app, store, ha):
    @app.get("/api/host/home-entry", response_model=HomeEntry)
    async def entry():
        return await home_entry(store, ha)
