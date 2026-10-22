"""Owner-local inventory flow; transport and HA replies are explicit fixtures."""
import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta

import pytest
from agentic_controller import network_inventory, store_journal
from agentic_controller.models import InventoryImport, canonical, digest, utcnow
from agentic_controller.network_inventory import KIND, imported_rows
from agentic_controller.store import Store
from fastapi import HTTPException
from test_bridge_controller import call as bridge_call
from test_bridge_controller import system as system
from test_discovery_setup import setup_system as setup_system

BASE = "/api/discovery/inventory"
REAL_BROKER = network_inventory.broker_call
IMPORT = {"format": "json", "label": "測試路由器", "content": json.dumps([
    {"ip": "192.0.2.8", "mac": "AA:BB:CC:DD:EE:01", "hostname": "桌燈橋接器", "manufacturer": "Philips Hue",
     "model": "Bridge", "services": ["_hue._tcp.local."], "password": "never-persist-this-secret"},
    {"ip": "192.0.2.9", "hostname": "Unknown device"}])}


def collection():
    now = utcnow()
    return {"contract_version": 1, "scope_id": "fixture_lan", "observed_at": now, "sources": [
        {"id": "lan_neighbors", "state": "ok", "method": "neighbor_table", "target": "fixture0", "observed_at": now,
         "item_count": 1},
        {"id": "lan_mdns", "state": "ok", "method": "mdns", "target": "fixture0", "observed_at": now,
         "item_count": 1},
        {"id": "lan_ssdp", "state": "partial", "method": "ssdp", "target": "fixture0", "observed_at": now,
         "item_count": 0, "reason_code": "fixture_timeout"}], "observations": [
        {"id": "neighbor_one", "source_id": "lan_neighbors", "scope_id": "fixture_lan", "address": "192.0.2.8",
         "mac": "aa:bb:cc:dd:ee:01", "observed_at": now},
        {"id": "mdns_one", "source_id": "lan_mdns", "scope_id": "fixture_lan", "address": "192.0.2.8",
         "name": "Hue bridge", "services": ["_hue._tcp.local."], "observed_at": now, "reachability": "responded",
         "metadata": {"manufacturer": "fixture", "secret": "never-persist-host-secret"}}], "limitations": ["fixture only"]}


@pytest.fixture
async def inventory_system(setup_system, monkeypatch):
    calls = []
    async def broker(method, path, body=None, **kwargs):
        calls.append((method, path, body))
        if path == "/discovery/scopes":
            return {"contract_version": 1, "scopes": [{"id": "fixture_lan", "label": "Fixture LAN", "interface": "fixture0",
                "address": "192.0.2.1", "network": "192.0.2.0/24"}], "limitations": ["fixture only"]}
        return collection()
    monkeypatch.setattr(network_inventory, "broker_call", broker)
    return (*setup_system, calls)


async def post(system, action="import", body=None, key="fixture-import"):
    return await system[1].post(BASE + "/" + action, json=IMPORT if body is None else body,
                                headers={"Idempotency-Key": key})


async def view(system):
    response = await system[1].get(BASE)
    assert response.status_code == 200, response.text
    return response.json()


async def test_inventory_import_scan_preserves_sources_unknown_rows_and_never_auto_merges(inventory_system):
    app, _, _, store, calls = inventory_system
    before = store.setting("policy")
    assert (await view(inventory_system))["revision"] == 0
    assert (await inventory_system[1].get(BASE + "/scopes")).json()["scopes"][0]["id"] == "fixture_lan"
    assert (await post(inventory_system)).json()["revision"] == 1
    assert (await post(inventory_system, "scan", {"scope_id": "fixture_lan"}, "scan")).json()["revision"] == 2
    result = await view(inventory_system)
    assert len(result["observations"]) == 4
    assert len({row["id"] for row in result["observations"]}) == 4
    assert sum(row["address"] == "192.0.2.8" for row in result["observations"]) == 3
    assert {row["confidence"] for row in result["recommendations"]} == {"service_match", "hint"}
    assert all(row["reachability"] == "unknown" for row in result["observations"] if row["source_id"] == "router_import")
    assert not any("never-persist" in canonical(record) for record in store.list(KIND))
    assert not any(secret in canonical(result) for secret in ("never-persist", "private-fixture-token", "binding"))
    assert calls == [("GET", "/discovery/scopes", None), ("POST", "/discovery/scan", {"scope_id": "fixture_lan"})]
    assert store.setting("policy") == before
    assert not any(store.list(kind) for kind in ("conversation", "proposal", "approval", "mcp_grant", "case_evidence"))
    assert app.state.network_inventory


@pytest.mark.parametrize("format,content", [
    ("csv", "ip_address,mac_address,name,manufacturer,services,password\n192.0.2.8,AA-BB-CC-DD-EE-01,Lamp,Philips Hue,_hue._tcp,secret\n"),
    ("json", json.dumps({"clients": [{"address": "192.0.2.8", "mac_address": "AA-BB-CC-DD-EE-01", "name": "Lamp"}]})),
    ("dnsmasq", "2000000000 aa:bb:cc:dd:ee:01 192.0.2.8 Lamp private-client-identifier\n"),
])
async def test_inventory_documented_import_formats_project_only_known_fields(inventory_system, format, content):
    response = await post(inventory_system, body={"format": format, "label": "Router", "content": content})
    assert response.status_code == 200
    result = await view(inventory_system)
    row = result["observations"][0]
    assert (row["address"], row["mac"], row["name"], row["reachability"]) == (
        "192.0.2.8", "aa:bb:cc:dd:ee:01", "Lamp", "unknown")
    assert row["source_updated_at"] is None  # A DHCP expiration is never a last-seen timestamp.
    assert "private-client-identifier" not in canonical(result)
    assert "secret" not in canonical(result)


@pytest.mark.parametrize("content", ["{broken", "[]", '[{"ip":"http://127.0.0.1/admin"}]',
    '[{"name":"x","last_seen":"2026-01-01"}]', '[{"ip":"192.0.2.1","services":[null]}]',
    '[{"ip":"192.0.2.1","last_seen":"9999-01-01T00:00:00Z"}]', '[{"ip":"192.0.2.1","mac":"not-a-mac"}]'])
async def test_inventory_invalid_import_never_persists_raw_content(inventory_system, content):
    response = await post(inventory_system, body={"format": "json", "label": "Router", "content": content})
    assert response.status_code == 422 and "content" not in response.json()
    assert inventory_system[3].list(KIND) == []


async def test_inventory_recommendation_expiry_respects_older_source_timestamp(inventory_system):
    earlier = datetime.now(UTC) - timedelta(hours=23)
    content = json.dumps([{"name": "Hue bridge", "last_seen": earlier.isoformat()}])
    assert (await post(inventory_system, body={"format": "json", "label": "Router", "content": content})).status_code == 200
    result = await view(inventory_system)
    assert result["recommendations"]
    assert result["expires_at"] == (earlier + timedelta(hours=24)).isoformat()
    assert datetime.fromisoformat(result["observed_at"]) > earlier


async def test_inventory_import_byte_limit_partial_rows_truncation_and_duplicate_stability(inventory_system):
    content = json.dumps([{"ip": "192.0.2.1", "name": "Lamp"}, {"ip": "192.0.2.1", "name": "Lamp"}, {"bad": True}])
    assert (await post(inventory_system, body={"format": "json", "label": "Router", "content": content})).status_code == 200
    first = await view(inventory_system)
    assert len(first["observations"]) == 2 and first["observations"][0]["id"] != first["observations"][1]["id"]
    source = next(row for row in first["sources"] if row["id"] == "router_import")
    assert source["state"] == "partial" and source["reason_code"] == "invalid_rows"
    assert (await post(inventory_system, body={"format": "json", "label": "Router", "content": content}, key="again")).status_code == 200
    assert [row["id"] for row in (await view(inventory_system))["observations"]] == [row["id"] for row in first["observations"]]
    too_many = json.dumps([{"name": "unknown" + str(i)} for i in range(260)])
    assert (await post(inventory_system, body={"format": "json", "label": "Router", "content": too_many}, key="many")).status_code == 200
    result = await view(inventory_system)
    assert len(result["observations"]) == 256
    assert next(row for row in result["sources"] if row["id"] == "router_import")["truncated"]
    large = "界" * 22000
    response = await post(inventory_system, body={"format": "json", "label": "Router", "content": large}, key="large")
    assert response.status_code == 422 and response.json()["detail"] == "inventory_import_too_large"


async def test_inventory_idempotency_conflicts_clear_cas_and_reboot_tombstones(inventory_system):
    _, _, _, store, _ = inventory_system
    first = (await post(inventory_system)).json()
    assert (await post(inventory_system)).json() == first
    assert (await post(inventory_system, body={**IMPORT, "label": "Changed"})).status_code == 409
    assert (await post(inventory_system, "clear", {"expected_revision": 0}, "clear-stale")).status_code == 409
    cleared = await post(inventory_system, "clear", {"expected_revision": 1}, "clear")
    assert cleared.json()["revision"] == 2
    assert (await post(inventory_system, "clear", {"expected_revision": 1}, "clear")).json() == cleared.json()
    assert (await post(inventory_system)).json() == first  # Retry is receipt-only, never restores cleared rows.
    assert (await view(inventory_system))["observations"] == []
    await store.durable()
    reopened = Store(store.path)
    try:
        assert reopened.list(KIND) == store.list(KIND)
        assert reopened.list(KIND)[0]["observations"] == []
        assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == 3
    finally:
        reopened.close()
    assert (await post(inventory_system, key="new-import")).json()["revision"] == 3
    assert (await post(inventory_system, "clear", {"expected_revision": 1}, "older-clear")).status_code == 409


@pytest.mark.parametrize("field", ["owner", "token", "base", "websocket"])
async def test_inventory_private_binding_never_crosses_owner_or_ha_connection(inventory_system, field):
    _, _, ha, store, _ = inventory_system
    await post(inventory_system)
    if field == "owner":
        store.set_setting("owner", "other")
        async def owner(value):
            return {"id": value}
        ha.owner = owner
    else:
        setattr(ha, field, "different")
    assert (await view(inventory_system))["observations"] == []
    assert len(store.list(KIND)) == 1


async def test_inventory_expiration_source_retention_and_prune_keep_only_revision(inventory_system):
    _, _, _, store, _ = inventory_system
    await post(inventory_system)
    await post(inventory_system, "scan", {"scope_id": "fixture_lan"}, "scan")
    record = store.list(KIND)[0]
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    for source in record["sources"]:
        if source["id"] == "router_import":
            source["observed_at"] = old
    for row in record["observations"]:
        if row["source_id"] == "router_import":
            row["observed_at"] = old
    store.put(KIND, record)
    result = await view(inventory_system)
    assert len(result["observations"]) == 4 and len(result["recommendations"]) == 1
    assert next(row for row in result["sources"] if row["id"] == "router_import")["state"] == "expired"
    store.set_setting("retention_pending", {"context_days": 1, "audit_days": 30})
    assert len((await view(inventory_system))["observations"]) == 2
    store.prune()
    assert len(store.list(KIND)[0]["observations"]) == 2
    record = store.list(KIND)[0]
    for source in record["sources"]:
        source["observed_at"] = old
    for row in record["observations"]:
        row["observed_at"] = old
    store.put(KIND, record)
    store.prune()
    assert store.list(KIND)[0]["sources"] == [] and store.list(KIND)[0]["observations"] == []
    assert (await view(inventory_system))["revision"] == 2
    assert "桌燈橋接器" not in canonical(store.list(KIND))


@pytest.mark.parametrize("change", ["owner", "token", "base", "websocket", "epoch", "revoked", "updater"])
async def test_inventory_scan_rechecks_authority_after_host_await(inventory_system, monkeypatch, change):
    _, _, ha, store, _ = inventory_system
    async def broker(*args, **kwargs):
        if change in {"token", "base", "websocket"}:
            setattr(ha, change, "changed")
        elif change == "owner":
            store.set_setting("owner", "other")
        elif change == "epoch":
            store.set_setting("hermes_web_epoch", "other")
        elif change == "revoked":
            ha.revoked = True
        else:
            store.set_setting("updater_gate", True)
        return collection()
    monkeypatch.setattr(network_inventory, "broker_call", broker)
    response = await post(inventory_system, "scan", {"scope_id": "fixture_lan"}, "scan")
    assert response.status_code in {401, 403, 423}
    assert store.list(KIND) == []


async def test_inventory_concurrent_scan_and_import_are_serialized_without_dropped_sources(inventory_system, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    async def broker(*args, **kwargs):
        entered.set()
        await release.wait()
        return collection()
    monkeypatch.setattr(network_inventory, "broker_call", broker)
    scan = asyncio.create_task(post(inventory_system, "scan", {"scope_id": "fixture_lan"}, "scan"))
    await asyncio.wait_for(entered.wait(), 3)
    imported = asyncio.create_task(post(inventory_system))
    await asyncio.sleep(.02)
    assert not imported.done()
    release.set()
    replies = await asyncio.gather(scan, imported)
    assert {reply.json()["revision"] for reply in replies} == {1, 2}
    assert len((await view(inventory_system))["observations"]) == 4


@pytest.mark.parametrize("fault", ["unsupported", "scope", "extra_source", "duplicate_id", "wrong_source", "future"])
async def test_inventory_host_faults_do_not_replace_existing_import(inventory_system, monkeypatch, fault):
    await post(inventory_system)
    async def broker(*args, **kwargs):
        if fault == "unsupported":
            raise HTTPException(503, "inventory_host_unavailable")
        result = collection()
        if fault == "scope":
            result["scope_id"] = "wrong"
        elif fault == "extra_source":
            result["sources"][0]["id"] = "router_import"
        elif fault == "duplicate_id":
            result["observations"][1]["id"] = result["observations"][0]["id"]
        elif fault == "wrong_source":
            result["observations"][0]["source_id"] = "router_import"
        else:
            result["observed_at"] = "2099-01-01T00:00:00Z"
        return result
    monkeypatch.setattr(network_inventory, "broker_call", broker)
    response = await post(inventory_system, "scan", {"scope_id": "fixture_lan"}, "scan")
    assert response.status_code == 503
    assert (await view(inventory_system))["revision"] == 1


@pytest.mark.parametrize("service,name,domain", [("_hue._tcp", "Lamp", "hue"), ("_googlecast._tcp", "TV", "cast"),
    ("_esphomelib._tcp", "Board", "esphome"), ("_hap._tcp", "Switch", "homekit_controller"),
    ("_hap._udp", "Thread Border Router", "homekit_controller"), ("_shelly._tcp", "Relay", "shelly"),
    ("_http._tcp", "shelly-plug", "shelly"), ("_ipp._tcp", "Office queue", "ipp"),
    ("_ipps._tcp", "Secure queue", "ipp"), ("_matterc._udp", "Pairing service", "matter")])
async def test_inventory_reviewed_mdns_fingerprints_use_actual_service_without_identity_claim(inventory_system, monkeypatch, service, name, domain):
    async def broker(*args, **kwargs):
        result = collection()
        result["observations"][1].update(services=[service], name=name)
        return result
    monkeypatch.setattr(network_inventory, "broker_call", broker)
    assert (await post(inventory_system, "scan", {"scope_id": "fixture_lan"}, "scan")).status_code == 200
    recommendation = (await view(inventory_system))["recommendations"][0]
    assert recommendation["integration_domain"] == domain and recommendation["confidence"] == "service_match"
    assert service in recommendation["reason"]
    assert recommendation["documentation_url"] == f"https://www.home-assistant.io/integrations/{domain}/"


@pytest.mark.parametrize("service,domain", [("_IPP._TCP.LOCAL.", "ipp"), ("_ipps._tcp.local.", "ipp"),
    ("_matterc._udp.local.", "matter")])
async def test_inventory_import_service_declarations_remain_hints(inventory_system, service, domain):
    content = json.dumps([{"name": "Imported service", "services": [service]}])
    assert (await post(inventory_system, body={"format": "json", "label": "Router", "content": content})).status_code == 200
    result = await view(inventory_system)
    suggestion = result["recommendations"][0]
    assert suggestion["integration_domain"] == domain and suggestion["confidence"] == "hint"
    assert "尚未驗證設備回應" in suggestion["reason"]
    assert result["observations"][0]["reachability"] == "unknown"


@pytest.mark.parametrize("service,metadata,source,expected", [
    ("_http._tcp.local.", {"vendor": "synology"}, "lan_mdns", "service_match"),
    ("_http._tcp.local.", {"vendor": "SYNOLOGY"}, "lan_mdns", "service_match"),
    ("urn:schemas-upnp-org:device:Basic:1", {"manufacturer": "Synology"}, "lan_ssdp", "hint"),
    ("_http._tcp.local.", {}, "lan_mdns", None),
    ("_http._tcp.local.", {"manufacturer": "Synology"}, "lan_mdns", None),
    ("_smb._tcp.local.", {"vendor": "synology"}, "lan_mdns", None),
    ("urn:schemas-upnp-org:device:Basic:1", {"server": "Synology NAS"}, "lan_ssdp", None),
    ("urn:schemas-upnp-org:device:Basic:1", {"manufacturer": "Other"}, "lan_ssdp", None),
    ("urn:schemas-upnp-org:device:Basic:2", {"manufacturer": "Synology"}, "lan_ssdp", None),
    ("urn:schemas-synology-com:device:NAS:1", {"manufacturer": "Synology"}, "lan_ssdp", None),
])
async def test_inventory_synology_requires_exact_reviewed_predicates(inventory_system, monkeypatch,
        service, metadata, source, expected):
    async def broker(*args, **kwargs):
        result = collection()
        result["observations"][1].update(source_id=source, services=[service], name="Synology NAS", metadata=metadata)
        return result
    monkeypatch.setattr(network_inventory, "broker_call", broker)
    assert (await post(inventory_system, "scan", {"scope_id": "fixture_lan"}, "scan")).status_code == 200
    result = await view(inventory_system)
    if expected is None:
        assert result["recommendations"] == []
    else:
        suggestion = result["recommendations"][0]
        assert suggestion["integration_domain"] == "synology_dsm" and suggestion["confidence"] == expected
        assert "家庭設定" in suggestion["next_step"] and "不傳給管家" in suggestion["next_step"]
        assert "尚未確認接入" in suggestion["next_step"]
    if source == "lan_mdns" and "vendor" in metadata:
        row = next(row for row in result["observations"] if row["source_id"] == "lan_mdns")
        assert row["metadata"].get("vendor") == "synology"


@pytest.mark.parametrize("metadata", [{"vendor": "synology token=private-secret"}, {"vendor": "other"},
    {"vendor": "synology", "password": "private-secret", "txt": "private-secret", "mac_address": "aa:bb:cc:dd:ee:ff"}])
async def test_inventory_persists_only_fixed_vendor_marker_not_raw_txt(inventory_system, monkeypatch, metadata):
    async def broker(*args, **kwargs):
        result = collection()
        result["observations"][1].update(services=["_http._tcp.local."], name="NAS", metadata=metadata)
        return result
    monkeypatch.setattr(network_inventory, "broker_call", broker)
    await post(inventory_system, "scan", {"scope_id": "fixture_lan"}, "scan")
    result = await view(inventory_system)
    raw = canonical(result) + canonical(inventory_system[3].list(KIND))
    assert "private-secret" not in raw and "aa:bb:cc:dd:ee:ff" not in raw
    projected = next(row["metadata"] for row in result["observations"] if row["source_id"] == "lan_mdns")
    assert projected == ({"vendor": "synology"} if metadata["vendor"] == "synology" else {})
    assert bool(result["recommendations"]) == (metadata["vendor"] == "synology")


async def test_inventory_imported_synology_manufacturer_is_only_unverified_hint(inventory_system):
    content = json.dumps([{"name": "NAS", "manufacturer": "Synology", "password": "private-password"},
        {"name": "Synology NAS"}, {"name": "Other", "manufacturer": "Synology-compatible"}])
    await post(inventory_system, body={"format": "json", "label": "Router", "content": content})
    result = await view(inventory_system)
    assert len(result["observations"]) == 3 and len(result["recommendations"]) == 1
    recommendation = result["recommendations"][0]
    assert recommendation["integration_domain"] == "synology_dsm" and recommendation["confidence"] == "hint"
    assert "廠商欄位標示 Synology" in recommendation["reason"]
    assert "private-password" not in canonical(result)


@pytest.mark.parametrize("name,services", [("Matter device", ["_matter._tcp.local."]),
    ("Matter controller", ["_matterd._udp.local."]), ("Matter printer", ["_matterc._tcp.local."]),
    ("Thread Border Router", ["_meshcop._udp.local."]), ("Home Connect dishwasher", []),
    ("Bosch washing machine", ["_http._tcp.local."]), ("Synology DSM", ["_http._tcp.local."]),
    ("Office IPP printer", ["_printer._tcp.local."])])
async def test_inventory_does_not_guess_new_device_types_from_names_or_nearby_services(inventory_system, name, services):
    await post(inventory_system, body={"format": "json", "label": "Router", "content":
        json.dumps([{"name": name, "services": services}])})
    result = await view(inventory_system)
    assert len(result["observations"]) == 1 and result["recommendations"] == []


async def test_inventory_printer_proxy_and_matter_hints_never_merge_or_grant_setup_authority(inventory_system, monkeypatch):
    _, client, ha, store, calls = inventory_system
    ha.results["/config"]["version"] = "2026.10.2"
    async def broker(*args, **kwargs):
        calls.append((args, kwargs))
        result = collection()
        original = result["observations"][1]
        result["observations"] = [{**original, "id": f"service_{index}", "name": "Same shared endpoint",
                                  "services": [service], "metadata": {}}
            for index, service in enumerate(("_ipp._tcp.local.", "_ipps._tcp.local.", "_matterc._udp.local."))]
        return result
    monkeypatch.setattr(network_inventory, "broker_call", broker)
    before = store.setting("policy")
    await post(inventory_system, "scan", {"scope_id": "fixture_lan"}, "scan")
    result = await view(inventory_system)
    assert len(result["observations"]) == len(result["recommendations"]) == 3
    assert len({row["address"] for row in result["observations"]}) == 1
    assert len({row["id"] for row in result["recommendations"]}) == 3
    for row in result["recommendations"]:
        if row["integration_domain"] == "ipp":
            assert "分享的印表機" in row["next_step"] and "家庭系統也找到可支援的印表機後" in row["next_step"]
            assert "尚未確認印表機身分" in row["reason"]
            response = await client.post("/api/device-setups", json={"candidate_id": row["id"]},
                                         headers={"Idempotency-Key": row["id"]})
            assert response.status_code == 409  # A LAN clue is not a native HA discovered flow.
            assert response.json()["detail"] == "device_setup_candidate_changed"
        else:
            assert "尚未確認接入" in row["next_step"] and "配對碼" in row["next_step"]
    assert len(calls) == 1 and store.setting("policy") == before
    assert not any(store.list(kind) for kind in ("device_setup_v1", "proposal", "approval", "conversation"))
    assert set(ha.calls) <= {"/config", "config_entries/flow/progress", "config_entries/get"}


async def test_inventory_new_rules_expire_and_version_digest_without_rewriting_records(inventory_system):
    content = json.dumps([{"name": "Printer", "services": ["_ipp._tcp"]},
        {"name": "NAS", "manufacturer": "Synology"}, {"name": "Pairing", "services": ["_matterc._udp"]}])
    await post(inventory_system, body={"format": "json", "label": "Router", "content": content})
    before = inventory_system[3].list(KIND)[0]
    result = await view(inventory_system)
    assert len(result["recommendations"]) == 3 and before["version"] == 1
    assert result["content_digest"] == digest({"rule_adapter_version": 2,
        **{key: result[key] for key in ("sources", "observations", "recommendations")}})
    assert inventory_system[3].list(KIND)[0] == before
    old = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    for row in before["observations"]:
        row["source_updated_at"] = old
    inventory_system[3].put(KIND, before)
    assert (await view(inventory_system))["recommendations"] == []


async def test_inventory_account_hint_unknown_devices_and_old_source_timestamp(inventory_system):
    old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    content = json.dumps([{"name": "Smart Life plug"}, {"name": "Unknown hub"},
                          {"name": "Old Hue", "manufacturer": "Philips Hue", "last_seen": old}])
    await post(inventory_system, body={"format": "json", "label": "Router", "content": content})
    result = await view(inventory_system)
    assert len(result["observations"]) == 3 and len(result["recommendations"]) == 1
    assert result["recommendations"][0]["integration_domain"] == "tuya"
    assert result["recommendations"][0]["confidence"] == "hint"


async def test_inventory_setup_uses_explicit_source_fresh_baseline_and_no_physical_association(inventory_system):
    _, client, ha, store, _ = inventory_system
    await post(inventory_system)
    suggestion = (await view(inventory_system))["recommendations"][0]
    body = {"candidate_id": suggestion["id"], "integration_domain": suggestion["integration_domain"], "source_id": "network_inventory"}
    ha.results["config_entries/get"].append({"entry_id": "existing-hue", "domain": "hue", "title": "Unrelated Hue", "state": "loaded"})
    response = await client.post("/api/discovery/setups", json=body, headers={"Idempotency-Key": "setup-inventory"})
    assert response.status_code == 200
    identifier = response.json()["id"]
    detail = (await client.get("/api/discovery/setups/" + identifier)).json()
    assert detail["candidate_state"] == "present" and detail["setup"]["candidate"]["source_id"] == "network_inventory"
    assert detail["options"][0]["known_before"] is True
    assert detail["integration"] is None and detail["devices"] == [] and detail["physical_verification"] == "not_performed"
    assert any(source["id"] == "network_inventory" for source in detail["sources"])
    assert (await client.post("/api/discovery/setups", json={**body, "source_id": "ha_discovery"},
        headers={"Idempotency-Key": "wrong-source"})).status_code == 409
    await post(inventory_system, "clear", {"expected_revision": 1}, "clear")
    assert (await client.get("/api/discovery/setups/" + identifier)).json()["candidate_state"] == "unknown"
    assert (await client.post("/api/discovery/setups", json=body,
        headers={"Idempotency-Key": "no-longer-current"})).status_code == 409
    assert store.list("discovery_setup")[0]["candidate"]["title"] == "桌燈橋接器"


async def test_inventory_legacy_ha_setup_receipt_digest_survives_optional_source_upgrade(inventory_system):
    _, client, _, store, _ = inventory_system
    body = {"candidate_id": "pending", "integration_domain": "fixture_light"}
    first = await client.post("/api/discovery/setups", json=body, headers={"Idempotency-Key": "legacy"})
    assert first.status_code == 200
    receipt = store.db.execute("SELECT digest FROM requests WHERE key='legacy'").fetchone()
    assert receipt[0] == digest(body)
    explicit = await client.post("/api/discovery/setups", json={**body, "source_id": "ha_discovery"},
        headers={"Idempotency-Key": "legacy"})
    assert explicit.json() == first.json()


async def test_inventory_response_waits_for_disk_then_checks_remote_owner(inventory_system, monkeypatch):
    _, _, ha, store, _ = inventory_system
    await store.durable()
    entered, release = threading.Event(), threading.Event()
    def hold():
        entered.set()
        assert release.wait(10)
    monkeypatch.setattr(store_journal, "before_disk_commit", hold)
    pending = asyncio.create_task(post(inventory_system))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        assert store.list(KIND) and not pending.done()
        ha.revoked = True
    finally:
        release.set()
    response = await asyncio.wait_for(pending, 3)
    assert response.status_code == 403 and "revision" not in response.json()
    assert "桌燈橋接器" not in response.text


async def test_inventory_bridge_reads_same_record_and_allows_only_fixed_routes(system, store):
    _, _, _, host, _, runtime = system
    response = await host.post(BASE + "/import", json=IMPORT, headers={"Idempotency-Key": "host-import"})
    assert response.status_code == 200
    result = await bridge_call(system, BASE)
    assert result.status_code == 200 and result.json()["revision"] == 1
    assert len(result.json()["observations"]) == 2
    assert (await bridge_call(system, BASE + "/clear", "POST", {"expected_revision": 1})).status_code == 200
    assert (await host.get(BASE)).json()["observations"] == []
    assert not runtime.calls
    assert not any(store.list(kind) for kind in ("conversation", "proposal", "approval", "mcp_grant"))


@pytest.mark.parametrize("path,method", [(BASE + "?all=true", "GET"), (BASE + "/scan", "GET"),
    (BASE + "/scopes", "POST"), (BASE + "/router/login", "POST"), (BASE + "/import/extra", "POST"),
    (BASE + "/probe?target=192.0.2.1", "POST")])
async def test_inventory_bridge_rejects_generic_targets_queries_and_wrong_methods(system, path, method):
    assert (await bridge_call(system, path, method, {} if method == "POST" else None)).status_code == 403


def test_inventory_import_projection_never_reinterprets_code_or_accepts_nested_labels():
    source, rows = imported_rows(InventoryImport.model_validate({"format": "json", "label": "Router", "content":
        json.dumps([{"ip": "192.0.2.1", "hostname": "$(touch /never)", "url": "http://127.0.0.1/secret"}])}))
    assert rows[0].name == "$(touch /never)" and source.item_count == 1
    assert rows[0].metadata == {}
    with pytest.raises(HTTPException):
        imported_rows(InventoryImport.model_validate({"format": "json", "label": "Router", "content":
            json.dumps([{"ip": "192.0.2.1", "manufacturer": {"password": "secret"}}])}))


async def test_inventory_unauthorized_and_missing_idempotency_never_reach_host(inventory_system):
    _, client, ha, store, calls = inventory_system
    assert (await client.post(BASE + "/scan", json={"scope_id": "fixture_lan"})).status_code == 400
    assert calls == [] and store.list(KIND) == []
    ha.revoked = True
    assert (await client.get(BASE + "/scopes")).status_code == 403
    assert (await post(inventory_system, "scan", {"scope_id": "fixture_lan"}, "scan")).status_code == 403
    assert calls == []


@pytest.mark.parametrize("fault", ["unavailable_rows", "source_state", "source_timestamp", "row_timestamp", "source_limit"])
async def test_inventory_host_evidence_contract_is_bounded_and_source_consistent(inventory_system, monkeypatch, fault):
    async def broker(*args, **kwargs):
        result = collection()
        if fault == "unavailable_rows":
            result["sources"][0]["state"] = "unavailable"
        elif fault == "source_state":
            result["sources"][0]["state"] = "expired"
        elif fault == "source_timestamp":
            result["sources"][0]["observed_at"] = "2025-01-01T00:00:00Z"
        elif fault == "row_timestamp":
            result["observations"][0]["observed_at"] = "2025-01-01T00:00:00Z"
        else:
            row = result["observations"][0]
            result["observations"] = [{**row, "id": f"row_{i}"} for i in range(257)]
        return result
    monkeypatch.setattr(network_inventory, "broker_call", broker)
    assert (await post(inventory_system, "scan", {"scope_id": "fixture_lan"}, "scan")).status_code == 503
    assert inventory_system[3].list(KIND) == []


async def test_inventory_maximum_unicode_snapshot_stays_within_bridge_byte_budget(inventory_system, monkeypatch):
    async def broker(*args, **kwargs):
        result = collection()
        result["observations"] = []
        for source in result["sources"]:
            source["state"] = "ok"
            source["item_count"] = 999  # Controller must derive counts after projection.
            source["content_digest"] = "0" * 64
            for number in range(256):
                result["observations"].append({"id": f"{source['id']}_{number:03}", "source_id": source["id"],
                    "scope_id": "fixture_lan", "address": "192.0.2.8", "name": "界" * 160,
                    "services": ["_hue._tcp"], "observed_at": result["observed_at"],
                    "metadata": {"manufacturer": "界" * 160, "model": "界" * 160, "server": "界" * 160}})
        return result
    monkeypatch.setattr(network_inventory, "broker_call", broker)
    assert (await post(inventory_system, "scan", {"scope_id": "fixture_lan"}, "scan")).status_code == 200
    response = await inventory_system[1].get(BASE)
    assert response.status_code == 200 and len(response.content) <= 750 * 1024
    result = response.json()
    assert len(result["observations"]) < 768
    assert any(source["truncated"] for source in result["sources"])
    assert any("傳輸大小限制" in limitation for limitation in result["limitations"])
    for source in result["sources"]:
        if source["state"] == "not_configured":
            continue
        rows = [row for row in result["observations"] if row["source_id"] == source["id"]]
        assert source["item_count"] == len(rows)
        assert source["content_digest"] == digest(rows)
    assert result["observed_at"] == (await inventory_system[1].get(BASE)).json()["observed_at"]


async def test_inventory_network_setup_readback_rechecks_inventory_after_owner_await(inventory_system):
    app, client, ha, store, _ = inventory_system
    await post(inventory_system)
    suggestion = (await view(inventory_system))["recommendations"][0]
    response = await client.post("/api/discovery/setups", json={"candidate_id": suggestion["id"],
        "integration_domain": suggestion["integration_domain"], "source_id": "network_inventory"},
        headers={"Idempotency-Key": "tracking"})
    identifier = response.json()["id"]
    original, count = ha.owner, 0
    async def owner(identifier):
        nonlocal count
        count += 1
        if count == 3:
            record = store.list(KIND)[0]
            record.update(sources=[], observations=[], revision=record["revision"] + 1)
            store.put(KIND, record)
        return await original(identifier)
    ha.owner = owner
    response = await client.get("/api/discovery/setups/" + identifier)
    assert response.status_code == 409 and "桌燈橋接器" not in response.text
    assert app.state.network_inventory


async def test_inventory_fixed_broker_accumulates_chunks_and_enforces_raw_limit(inventory_system, monkeypatch):
    from contextlib import asynccontextmanager

    class Content:
        def __init__(self, chunks):
            self.chunks = chunks
        async def iter_chunked(self, size):
            for chunk in self.chunks:
                await asyncio.sleep(0)
                yield chunk

    class Response:
        status = 200
        def __init__(self, chunks):
            self.content = Content(chunks)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False

    chunks = [b'{"scopes":', b'[],"limitations":', b'[]}']
    class Client:
        def request(self, method, url, **kwargs):
            assert method == "GET" and url == "http://host/discovery/scopes" and kwargs["allow_redirects"] is False
            return Response(chunks)

    @asynccontextmanager
    async def session(*args, **kwargs):
        assert kwargs["trust_env"] is False and kwargs["timeout"].total == 10
        yield Client()

    # Exercise the real function hidden by inventory_system's transport fixture.
    monkeypatch.setattr(network_inventory, "client_session", session)
    monkeypatch.setattr(network_inventory.aiohttp, "UnixConnector", lambda **kwargs: object())
    assert await REAL_BROKER("GET", "/discovery/scopes", store=inventory_system[3]) == {"scopes": [], "limitations": []}
    chunks[:] = [b"x" * 65536] * 17
    with pytest.raises(HTTPException) as oversized:
        await REAL_BROKER("GET", "/discovery/scopes", store=inventory_system[3])
    assert oversized.value.status_code == 503
    with pytest.raises(HTTPException) as arbitrary:
        await REAL_BROKER("GET", "http://192.0.2.1/api", store=inventory_system[3])
    assert arbitrary.value.status_code == 422
