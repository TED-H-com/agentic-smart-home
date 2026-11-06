import pytest
from agentic_controller.mcp import create_mcp
from httpx import ASGITransport, AsyncClient


class MockCoordinator:
    def __init__(self):
        self.store = MockStore()
        self.device_setup_status = MockSetupStatus()
        self.discovery = MockDiscovery()
        self.household = MockHousehold()
        self.worker_manager = MockWorkerManager()

    def authorize(self, token):
        return {"run_id": "test-run", "case_id": "test-case", "calls": 0}

    async def host_evidence(self, grant):
        return {"host_status": "ok"}

class MockStore:
    def get(self, *args, **kwargs):
        return {"case_id": "test-case"}
    def put(self, *args, **kwargs):
        pass
    def event(self, *args, **kwargs):
        pass
    def transition(self, *args, **kwargs):
        pass
    def request(self, *args, **kwargs):
        pass
    def prune(self):
        pass

    def transaction(self):
        class MockTx:
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc_val, exc_tb):
                pass
            def execute(self, *args, **kwargs):
                pass
        return MockTx()

    async def durable(self):
        pass

class MockSetupStatus:
    async def read(self, *args):
        return {"device_setup": "ok"}

class MockDiscovery:
    async def discover(self, *args):
        return {"discover": "ok"}

class MockHousehold:
    async def observe(self, *args):
        return {"observe": "ok"}

    async def propose(self, *args):
        return {"propose": "ok"}

    def status(self, *args):
        return {"status": "ok"}

class MockWorkerManager:
    async def capabilities(self):
        return {"capabilities": "ok"}

    async def submit(self, *args, **kwargs):
        return {"submit": "ok"}

    async def cancel(self, *args, **kwargs):
        return {"cancel": "ok"}

    def get(self, *args, **kwargs):
        return {"worker": "ok"}

    def public(self, *args):
        return {"public": "ok"}

@pytest.fixture
def mcp_app():
    coordinator = MockCoordinator()
    app = create_mcp(coordinator)
    return app

@pytest.mark.asyncio
async def test_mcp_workflows(mcp_app):
    transport = ASGITransport(app=mcp_app, client=("127.0.0.1", 40000))
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8097") as client:

        async def call_tool(name, arguments={}):
            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments, "_meta": {}}
            }
            res = await client.post("/mcp", json=body, headers={"Authorization": "Bearer test-token"})
            if res.json()["result"]["isError"]:
                print(f"Error for {name}:", res.json())
            return res.json()["result"]

        res = await call_tool("home.device_setup_status")
        assert not res["isError"]
        assert "device_setup" in res["content"][0]["text"]

        res = await call_tool("home.discover")
        assert not res["isError"]
        assert "discover" in res["content"][0]["text"]

        # Valid input for home.observe to pass Pydantic validation (entities not entity_ids)
        res = await call_tool("home.observe", {"entities": ["light.test"]})
        assert not res["isError"]
        assert "observe" in res["content"][0]["text"]

        intent_payload = {
            "title": "A title that is long enough to pass validation",
            "trigger": {"entity_id": "sensor.test", "from_state": "off", "to_state": "on"},
            "actions": [{"entity_id": "switch.test", "action": "turn_on"}]
        }
        res = await call_tool("home.propose", {"idempotency_key": "k"*8, "action": None, "automation": intent_payload})
        assert not res["isError"]
        assert "propose" in res["content"][0]["text"]

        res = await call_tool("home.proposal_status", {"task_id": "a"*32})
        assert not res["isError"]
        assert "status" in res["content"][0]["text"]

        # Test the rest
        res = await call_tool("host.status")
        assert not res["isError"]
        assert "host_status" in res["content"][0]["text"]

        res = await call_tool("worker.capabilities")
        assert not res["isError"]
        assert "capabilities" in res["content"][0]["text"]

        res = await call_tool("worker.submit", {"worker_id": "host-coder", "mode": "research", "idempotency_key": "k"*8, "goal": "g"})
        assert not res["isError"]
        assert "submit" in res["content"][0]["text"]

        res = await call_tool("worker.status", {"job_id": "a"*32})
        assert not res["isError"]
        assert "public" in res["content"][0]["text"]

        res = await call_tool("worker.cancel", {"job_id": "a"*32})
        assert not res["isError"]
        assert "cancel" in res["content"][0]["text"]

        # case.evidence.read
        res = await call_tool("case.evidence.read", {"evidence_ids": ["a"*32]})
        assert not res["isError"]
        assert "evidence" in res["content"][0]["text"]

        # Invalid payload testing
        res = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "invalid.tool", "arguments": {}}}, headers={"Authorization": "Bearer test-token"})
        assert res.json()["result"]["isError"]
        assert "invalid_or_out_of_scope_request" in res.json()["result"]["content"][0]["text"]
