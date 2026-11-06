from unittest.mock import AsyncMock

import pytest
from agentic_controller.ha import HAError, HomeAssistant


@pytest.fixture
def mock_ha():
    ha = HomeAssistant("mock_token")
    ha.request = AsyncMock()
    ha.ws = AsyncMock()
    return ha

async def test_validate_success(mock_ha):
    mock_ha.ensure_write_compatibility = AsyncMock()
    mock_ha.ws.return_value = {"triggers": {"valid": True}, "conditions": {"valid": True}, "actions": {"valid": True}}
    config = {"triggers": [], "conditions": [], "actions": []}
    await mock_ha.validate(config)
    mock_ha.ws.assert_called_once_with("validate_config", triggers=[], conditions=[], actions=[])

async def test_validate_fallback_singular(mock_ha):
    mock_ha.ensure_write_compatibility = AsyncMock()
    # First call raises invalid_format HAError, second call succeeds
    def mock_ws(command, **kwargs):
        if "triggers" in kwargs:
            raise HAError("invalid_format")
        return {"trigger": {"valid": True}, "condition": {"valid": True}, "action": {"valid": True}}
    mock_ha.ws.side_effect = mock_ws

    config = {"triggers": [], "conditions": [], "actions": []}
    await mock_ha.validate(config)
    assert mock_ha.ws.call_count == 2

async def test_validate_failure(mock_ha):
    mock_ha.ensure_write_compatibility = AsyncMock()
    mock_ha.ws.return_value = {"triggers": {"valid": True}, "conditions": {"valid": False}, "actions": {"valid": True}}
    config = {"triggers": [], "conditions": [], "actions": []}
    with pytest.raises(HAError, match="Home Assistant 原生設定驗證拒絕此規則"):
        await mock_ha.validate(config)

async def test_config(mock_ha):
    mock_ha.request.return_value = {"key": "value"}
    result = await mock_ha.config("automation_id")
    assert result == {"key": "value"}
    mock_ha.request.assert_called_once_with("GET", "/config/automation/config/automation_id", missing_ok=True)

async def test_write(mock_ha):
    mock_ha.request.return_value = {"success": True}
    config = {"some": "data"}
    result = await mock_ha.write("automation_id", config)
    assert result == {"success": True}
    mock_ha.request.assert_called_once_with("POST", "/config/automation/config/automation_id", config)

async def test_service(mock_ha):
    mock_ha.request.return_value = {"success": True}
    result = await mock_ha.service("light", "turn_on", "light.living_room")
    assert result == {"success": True}
    mock_ha.request.assert_called_once_with("POST", "/services/light/turn_on", {"entity_id": "light.living_room"})

async def test_automation_entity_found(mock_ha):
    mock_ha.request.return_value = [
        {"entity_id": "automation.test", "attributes": {"id": "target_id"}}
    ]
    result = await mock_ha.automation_entity("target_id")
    assert result == {"entity_id": "automation.test", "attributes": {"id": "target_id"}}

async def test_automation_entity_digest_match(mock_ha):
    mock_ha.request.return_value = [
        {"entity_id": "automation.test", "attributes": {"id": "target_id"}}
    ]
    mock_ha.ws.return_value = {"config": {"key": "value"}}
    expected_config = {"key": "value"}

    result = await mock_ha.automation_entity("target_id", expected=expected_config)
    assert result == {"entity_id": "automation.test", "attributes": {"id": "target_id"}}
    mock_ha.ws.assert_called_once_with("automation/config", entity_id="automation.test")

async def test_automation_entity_timeout(mock_ha, monkeypatch):
    mock_ha.request.return_value = []

    import asyncio
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    with pytest.raises(HAError, match="規則已寫入，HA 尚未載入相符的 automation 設定"):
        await mock_ha.automation_entity("target_id", attempts=2)

    assert mock_ha.request.call_count == 2

async def test_dependencies_no_conflict(mock_ha):
    mock_ha.request.return_value = [
        {"entity_id": "automation.other", "attributes": {"id": "other_id"}}
    ]
    mock_ha.config = AsyncMock(return_value={"triggers": [{"entity_id": "light.kitchen"}]})

    result = await mock_ha.dependencies(["light.living_room"], owned={"owned_id"})
    assert result == []

async def test_dependencies_conflict(mock_ha):
    mock_ha.request.return_value = [
        {"entity_id": "automation.conflict", "attributes": {"id": "conflict_id"}}
    ]
    mock_ha.config = AsyncMock(return_value={"triggers": [{"entity_id": "light.living_room"}]})

    result = await mock_ha.dependencies(["light.living_room"], owned={"owned_id"})
    assert result == ["automation.conflict"]

async def test_dependencies_missing_id(mock_ha):
    mock_ha.request.return_value = [
        {"entity_id": "automation.missing_id", "attributes": {}}
    ]

    with pytest.raises(HAError, match="現有 automation 缺少 ID，請先由原生 HA 編輯器補齊"):
        await mock_ha.dependencies(["light.living_room"], owned=set())

async def test_backup_success(mock_ha):
    mock_ha.external = True
    def mock_request_side_effect(method, path, data=None, **kwargs):
        if path == "/config":
            return {"state": "RUNNING"}
        if path == "/supervisor/info":
            return {"healthy": True}
        if path == "/backups/new/partial":
            return {"slug": "test_slug"}
        if path == "/backups/test_slug/info":
            return {"slug": "test_slug", "homeassistant": True}
        return {}
    mock_ha.request.side_effect = mock_request_side_effect

    result = await mock_ha.backup("test_backup")
    assert result["id"] == "test_slug"
    assert result["name"] == "test_backup"
    assert "created_at" in result

async def test_backup_core_not_running(mock_ha):
    mock_ha.request.return_value = {"state": "STARTING"}

    with pytest.raises(HAError, match="Home Assistant 還在啟動；等它啟動完成後再部署"):
        await mock_ha.backup("test_backup")

async def test_backup_supervisor_unhealthy(mock_ha):
    mock_ha.external = True
    def mock_request_side_effect(method, path, data=None, **kwargs):
        if path == "/config":
            return {"state": "RUNNING"}
        if path == "/supervisor/info":
            return {"healthy": False}
        return {}
    mock_ha.request.side_effect = mock_request_side_effect

    with pytest.raises(HAError, match="Supervisor 尚未就緒；等待 HAOS 啟動完成後再部署"):
        await mock_ha.backup("test_backup")

async def test_backup_incomplete_creation(mock_ha):
    mock_ha.external = True
    def mock_request_side_effect(method, path, data=None, **kwargs):
        if path == "/config":
            return {"state": "RUNNING"}
        if path == "/supervisor/info":
            return {"healthy": True}
        if path == "/backups/new/partial":
            return {} # missing slug
        return {}
    mock_ha.request.side_effect = mock_request_side_effect

    with pytest.raises(HAError, match="Supervisor 尚未完成備份"):
        await mock_ha.backup("test_backup")

async def test_backup_missing_homeassistant(mock_ha):
    mock_ha.external = True
    def mock_request_side_effect(method, path, data=None, **kwargs):
        if path == "/config":
            return {"state": "RUNNING"}
        if path == "/supervisor/info":
            return {"healthy": True}
        if path == "/backups/new/partial":
            return {"slug": "test_slug"}
        if path == "/backups/test_slug/info":
            return {"slug": "test_slug", "homeassistant": False}
        return {}
    mock_ha.request.side_effect = mock_request_side_effect

    with pytest.raises(HAError, match="備份讀回未包含 Home Assistant"):
        await mock_ha.backup("test_backup")

async def test_watch(mock_ha, monkeypatch):
    import asyncio
    call_count = [0]
    async def side_effect_sleep(delay):
        call_count[0] += 1
        if call_count[0] == 2:
            raise Exception("Stop Watch Loop")

    monkeypatch.setattr(asyncio, "sleep", side_effect_sleep)

    mock_ha.request.return_value = []

    mock_ws = AsyncMock()
    mock_ws.receive_json = AsyncMock(return_value={"success": True})

    # We will simulate a message stream containing one state_changed event and then a non-text message
    import aiohttp
    class MockMessage:
        def __init__(self, msg_type, json_data=None):
            self.type = msg_type
            self._json_data = json_data

        def json(self):
            return self._json_data

    class AsyncIter:
        def __init__(self, items):
            self.items = items
            self.index = 0
        def __aiter__(self):
            return self
        async def __anext__(self):
            if self.index < len(self.items):
                item = self.items[self.index]
                self.index += 1
                return item
            raise StopAsyncIteration

    class MagicAsyncIter:
        def __init__(self, items):
            self.items = items
            self.index = 0
        def __aiter__(self):
            return self
        async def __anext__(self):
            if self.index < len(self.items):
                item = self.items[self.index]
                self.index += 1
                return item
            raise StopAsyncIteration

    mock_ws.__aiter__ = lambda *args, **kwargs: MagicAsyncIter([
        MockMessage(aiohttp.WSMsgType.TEXT, {"event": {"time_fired": "2026-10-21T00:00:00Z"}}),
        MockMessage(aiohttp.WSMsgType.BINARY)
    ])

    mock_ha.ws_connect = AsyncMock(return_value=mock_ws)

    with pytest.raises(Exception, match="Stop Watch Loop"):
        await mock_ha.watch()

    assert list(mock_ha.observations) == [{"time_fired": "2026-10-21T00:00:00Z"}, {"time_fired": "2026-10-21T00:00:00Z"}]
    mock_ws.send_json.assert_called_with({"id": 1, "type": "subscribe_events", "event_type": "state_changed"})
    assert mock_ws.send_json.call_count == 2

async def test_watch_event_subscribe_fail(mock_ha, monkeypatch):
    import asyncio
    call_count = [0]
    async def side_effect_sleep(delay):
        call_count[0] += 1
        if call_count[0] == 2:
            raise Exception("Stop Watch Loop")

    monkeypatch.setattr(asyncio, "sleep", side_effect_sleep)

    mock_ws = AsyncMock()
    mock_ws.receive_json = AsyncMock(return_value={"success": False})

    mock_ha.ws_connect = AsyncMock(return_value=mock_ws)

    with pytest.raises(Exception, match="Stop Watch Loop"):
        await mock_ha.watch()

    assert mock_ha.connected is False

async def test_evidence_no_intent(mock_ha):
    proposal = {"deployed_at": "2026-10-21T00:00:00Z"}
    assert mock_ha.evidence(proposal) is None

async def test_evidence_no_deployed_at(mock_ha):
    proposal = {"intent": {}}
    assert mock_ha.evidence(proposal) is None


async def test_request_missing_ok():
    ha = HomeAssistant("mock_token")
    ha.base = "http://base"
    ha.supervisor = "http://supervisor"

    class MockResponse:
        def __init__(self, status):
            self.status = status
            self.content = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

    class MockSession:
        def request(self, method, url, **kwargs):
            return MockResponse(404)

    ha.session = MockSession()
    assert await ha.request("GET", "/test", missing_ok=True) is None

async def test_dependencies_skip_owned_and_non_automation(mock_ha):
    mock_ha.request = AsyncMock(return_value=[
        {"entity_id": "light.test"},
        {"entity_id": "automation.owned", "attributes": {"id": "owned_id"}}
    ])
    result = await mock_ha.dependencies(["light.test"], owned={"owned_id"})
    assert result == []


async def test_ensure_write_compatibility_unknown_version():
    ha = HomeAssistant("mock_token")
    ha.request = AsyncMock(return_value={"version": "1999.1.1"})
    with pytest.raises(HAError, match="此版本尚未包含在自動化寫入相容範圍"):
        await ha.ensure_write_compatibility()

async def test_automation_entity_error():
    ha = HomeAssistant("mock_token")
    ha.request = AsyncMock(return_value=[{"entity_id": "automation.test", "attributes": {"id": "test_id"}}])
    ha.ws = AsyncMock(side_effect=HAError("error"))
    # it will retry the ws check multiple times if the config hasn't caught up, or fail after attempts
    with pytest.raises(HAError):
        await ha.automation_entity("test_id", expected={"key": "value"}, attempts=1)


async def test_inventory_gathering(mock_ha):
    mock_ha.request.return_value = [{"entity_id": "light.living_room", "state": "on", "attributes": {"friendly_name": "Living Room Light"}}]

    async def mock_ws(command):
        if command == "config/entity_registry/list":
            return [{"entity_id": "light.living_room", "device_id": "dev1", "area_id": "area1", "config_entry_id": "conf1"}]
        elif command == "config/device_registry/list":
            return [{"id": "dev1", "area_id": "area1"}]
        elif command == "config/area_registry/list":
            return [{"area_id": "area1", "name": "Living Room"}]
        elif command == "get_services":
            return {"light": {"turn_on": True, "turn_off": True}}
        return []

    mock_ha.ws = mock_ws

    inventory = await mock_ha.inventory()

    assert len(inventory) == 1
    assert inventory[0]["entity_id"] == "light.living_room"
    assert inventory[0]["area"] == "Living Room"
    assert inventory[0]["domain"] == "light"
    assert inventory[0]["state"] == "on"


async def test_request_supervisor_external():
    ha = HomeAssistant("mock_token")
    ha.external = True
    ha.ws = AsyncMock(return_value={"result": "ok"})

    await ha.request("GET", "/supervisor/info", supervisor=True)

    ha.ws.assert_called_once_with(
        "supervisor/api", endpoint="/supervisor/info", method="get", data={},
        timeout=30.0, _receive_timeout=40.0, _send_guard=None
    )

async def test_request_supervisor_error():
    ha = HomeAssistant("mock_token")
    ha.base = "http://base"
    ha.supervisor = "http://supervisor"

    class MockResponse:
        def __init__(self, status):
            self.status = status
            self.content = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

        async def iter_chunked(self, size):
            import json
            yield json.dumps({"result": "error"}).encode()

    class MockSession:
        def request(self, method, url, **kwargs):
            return MockResponse(200)

    ha.session = MockSession()

    with pytest.raises(HAError, match="Supervisor 操作未成功"):
        await ha.request("GET", "/supervisor/info", supervisor=True)

async def test_request_oversize():
    ha = HomeAssistant("mock_token")
    ha.base = "http://base"
    ha.supervisor = "http://supervisor"

    class MockResponse:
        def __init__(self, status):
            self.status = status
            self.content = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

        async def iter_chunked(self, size):
            yield b"a" * (8 * 1024 * 1024 + 1)

    class MockSession:
        def request(self, method, url, **kwargs):
            return MockResponse(200)

    ha.session = MockSession()

    with pytest.raises(HAError, match="HA 回應超過大小上限"):
        await ha.request("GET", "/test")


async def test_request_supervisor_external_result():
    ha = HomeAssistant("mock_token")
    ha.base = "http://base"
    ha.supervisor = "http://supervisor"

    class MockResponse:
        def __init__(self, status):
            self.status = status
            self.content = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

        async def iter_chunked(self, size):
            import json
            yield json.dumps({"result": "ok", "data": {"key": "value"}}).encode()

    class MockSession:
        def request(self, method, url, **kwargs):
            return MockResponse(200)

    ha.session = MockSession()

    result = await ha.request("GET", "/supervisor/info", supervisor=True)
    assert result == {"key": "value"}

async def test_validate_invalid_format_other_error():
    ha = HomeAssistant("mock_token")
    ha.ensure_write_compatibility = AsyncMock()
    ha.ws = AsyncMock(side_effect=HAError("other_error"))

    with pytest.raises(HAError, match="other_error"):
        await ha.validate({"triggers": [], "conditions": [], "actions": []})


async def test_request_supervisor_no_data():
    ha = HomeAssistant("mock_token")
    ha.base = "http://base"
    ha.supervisor = "http://supervisor"

    class MockResponse:
        def __init__(self, status):
            self.status = status
            self.content = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

        async def iter_chunked(self, size):
            import json
            yield json.dumps({"result": "ok"}).encode()

    class MockSession:
        def request(self, method, url, **kwargs):
            return MockResponse(200)

    ha.session = MockSession()

    result = await ha.request("GET", "/supervisor/info", supervisor=True)
    assert result == {}


async def test_request_normal_result():
    ha = HomeAssistant("mock_token")
    ha.base = "http://base"
    ha.supervisor = "http://supervisor"

    class MockResponse:
        def __init__(self, status):
            self.status = status
            self.content = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

        async def iter_chunked(self, size):
            import json
            yield json.dumps({"key": "value"}).encode()

    class MockSession:
        def request(self, method, url, **kwargs):
            return MockResponse(200)

    ha.session = MockSession()

    result = await ha.request("GET", "/api/test")
    assert result == {"key": "value"}


async def test_watch_exception_loop(mock_ha, monkeypatch):
    import asyncio

    mock_ha.request.return_value = []

    mock_ws = AsyncMock()
    mock_ws.receive_json = AsyncMock(return_value={"success": True})

    # We will simulate a message stream containing one state_changed event and then an exception
    import aiohttp
    class MockMessage:
        def __init__(self, msg_type, json_data=None):
            self.type = msg_type
            self._json_data = json_data

        def json(self):
            return self._json_data

    class MagicAsyncIter:
        def __init__(self, items):
            self.items = items
            self.index = 0
        def __aiter__(self):
            return self
        async def __anext__(self):
            if self.index < len(self.items):
                item = self.items[self.index]
                self.index += 1
                return item
            raise ValueError("Intentional error for watch loop")

    mock_ws.__aiter__ = lambda *args, **kwargs: MagicAsyncIter([
        MockMessage(aiohttp.WSMsgType.TEXT, {"event": {"time_fired": "2026-10-21T00:00:00Z"}})
    ])

    mock_ha.ws_connect = AsyncMock(return_value=mock_ws)

    # Let sleep raise an exception to exit the infinite loop after it catches the ValueError
    call_count = [0]
    async def side_effect_sleep(delay):
        call_count[0] += 1
        if call_count[0] == 2:
            raise Exception("Stop Watch Loop")

    monkeypatch.setattr(asyncio, "sleep", side_effect_sleep)

    with pytest.raises(Exception, match="Stop Watch Loop"):
        await mock_ha.watch()

    assert list(mock_ha.observations) == [{"time_fired": "2026-10-21T00:00:00Z"}, {"time_fired": "2026-10-21T00:00:00Z"}]
    assert mock_ha.connected is False
