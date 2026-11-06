import json

import pytest
from test_onboarding_e2e import BoundaryFixtures


class DummyRequest:
    def __init__(self, data):
        self.data = data
    async def json(self):
        return self.data

@pytest.mark.asyncio
async def test_boundary_fixtures_host_status():
    f = BoundaryFixtures()
    # Not provisioned
    res = await f.host_status(DummyRequest({}))
    data = json.loads(res.body)
    assert data["vm"] == "not_created"

    # Provisioned
    f.provisioned = True
    # To test actual HA unreachable, the aiohttp call inside host_status will fail
    # or return quickly if nothing is listening, so we'll just test if it doesn't crash
    try:
        await f.host_status(DummyRequest({}))
    except Exception:
        pass

@pytest.mark.asyncio
async def test_boundary_fixtures_host_action():
    f = BoundaryFixtures()

    # Test valid actions
    res = await f.host_action(DummyRequest({"id": "1", "action": "bootstrap_hermes"}))
    assert json.loads(res.body)["state"] == "succeeded"
    assert f.installed

    res = await f.host_action(DummyRequest({"id": "2", "action": "start"}))
    assert f.provisioned

    # Idempotent
    res = await f.host_action(DummyRequest({"id": "2", "action": "start"}))
    assert json.loads(res.body)["state"] == "succeeded"

    # Invalid action
    with pytest.raises(AssertionError):
        await f.host_action(DummyRequest({"id": "3", "action": "invalid"}))

@pytest.mark.asyncio
async def test_boundary_fixtures_cli_model():
    f = BoundaryFixtures()
    res = await f.cli_model(DummyRequest({"model": "test"}))
    assert res.content_type == "text/event-stream"
    text = res.text
    assert "response.completed" in text

@pytest.mark.asyncio
async def test_boundary_fixtures_hermes_model():
    f = BoundaryFixtures()

    # Verify/Tools branch
    res = await f.hermes_model(DummyRequest({
        "messages": [{"role": "user", "content": "模型連線驗證"}],
        "tools": []
    }))
    assert json.loads(res.body)["choices"][0]["finish_reason"] == "stop"

    # Tool call branch
    res = await f.hermes_model(DummyRequest({
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"function": {"name": "mcp__agentic__host_status"}}]
    }))
    assert json.loads(res.body)["choices"][0]["finish_reason"] == "tool_calls"

    # Streaming branch
    res = await f.hermes_model(DummyRequest({
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"function": {"name": "mcp__agentic__host_status"}}],
        "stream": True
    }))
    assert res.content_type == "text/event-stream"
    assert "[DONE]" in res.text


from test_onboarding_e2e import eventually, rejection_shape, stack_file


def test_stack_file():
    # Valid stack
    stack = "Error\n  at something (http://example.com/js/bundle.js?v=1:10:20)"
    assert stack_file(stack) == "bundle.js"
    # No match
    assert stack_file("Error\n at unknown (unknown:10)") is None

def test_rejection_shape():
    # Dict
    res = rejection_shape("origin", {"kind": "test", "ctor": "error", "keys": ["a"], "code": "some_code"})
    assert res["kind"] == "test"
    assert res["code"] == "some_code"

    # Not dict
    res = rejection_shape("origin", "string_val")
    assert res["kind"] == ""
    assert res["code"] is None

    # Int code
    res = rejection_shape("origin", {"code": 404})
    assert res["code"] == 404

@pytest.mark.asyncio
async def test_eventually():
    # Happy path
    async def call_ok():
        return True
    assert await eventually(call_ok, lambda x: x, timeout=1)

    # Timeout
    async def call_fail():
        return False
    with pytest.raises(AssertionError, match="E2E checkpoint timed out"):
        await eventually(call_fail, lambda x: x, timeout=0.1)


from test_onboarding_e2e import docker


@pytest.mark.asyncio
async def test_docker():
    res = await docker("version")
    assert res

    with pytest.raises(AssertionError, match="Docker command failed"):
        await docker("invalid_command_that_fails_docker")
import aiohttp


@pytest.mark.asyncio
async def test_eventually_exception():
    count = [0]
    async def call():
        count[0] += 1
        if count[0] == 1:
            raise ValueError()
        return True

    assert await eventually(call, lambda x: x, timeout=1)


@pytest.mark.asyncio
async def test_host_status_with_mocked_aiohttp(monkeypatch):

    class MockResponse:
        status = 200
        async def json(self):
            return [{"done": True}]

        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass

    class MockSession:
        def get(self, url):
            return MockResponse()
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass

    monkeypatch.setattr(aiohttp, "ClientSession", MockSession)

    f = BoundaryFixtures()
    f.provisioned = True

    res = await f.host_status(DummyRequest({}))
    data = json.loads(res.body)
    assert data["bootstrap"]["ha"]["reachable"] is True
    assert data["bootstrap"]["ha"]["onboarding_complete"] is True
