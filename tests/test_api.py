import httpx
import pytest
from agentic_controller.app import create_app


async def client_for(store, ha, ip="172.30.32.2", user="owner"):
    app = create_app(store=store, ha=ha)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip, 1234)),
                             base_url="http://home.test", headers={"X-Remote-User-Id": user})


@pytest.mark.parametrize("ip,user", [("172.30.33.7", "owner"), ("172.30.32.2", "normal-user"), ("127.0.0.1", "owner")])
async def test_ingress_source_and_owner_cannot_be_forged(store, ha, ip, user):
    async with await client_for(store, ha, ip, user) as client:
        response = await client.get("/api/session", headers={"X-Forwarded-For": "172.30.32.2"})
        assert response.status_code == 403


async def test_csrf_origin_and_runner_bearer_cannot_approve(store, ha):
    async with await client_for(store, ha) as client:
        token = (await client.get("/api/session")).json()["csrf"]
        for headers in [{}, {"X-Agentic-CSRF": token, "Origin": "https://evil.test"},
                        {"Authorization": "Bearer runner-token", "Origin": "http://home.test"}]:
            response = await client.post("/api/pause", json={"paused": True}, headers=headers)
            assert response.status_code == 403
        response = await client.post("/api/pause", json={"paused": True},
                                    headers={"X-Agentic-CSRF": token, "Origin": "http://home.test"})
        assert response.status_code == 200 and store.setting("paused") is True


async def test_owner_rechecked_on_mutation_even_with_read_cache(store, ha):
    async with await client_for(store, ha) as client:
        token = (await client.get("/api/session")).json()["csrf"]
        ha.revoked = True
        response = await client.post("/api/pause", json={"paused": True},
                                    headers={"X-Agentic-CSRF": token, "Origin": "http://home.test"})
        assert response.status_code == 403


async def test_validation_error_does_not_echo_secret(store, ha):
    async with await client_for(store, ha) as client:
        token = (await client.get("/api/session")).json()["csrf"]
        response = await client.post("/api/providers/codex/auth", json={"method": "unsupported", "secret": "sensitive-value"},
                                    headers={"X-Agentic-CSRF": token, "Origin": "http://home.test"})
        assert response.status_code == 422 and "sensitive-value" not in response.text


async def test_encrypted_snapshot_can_restore(store, ha, tmp_path):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("restore", Path(__file__).parents[1] / "scripts/restore_controller.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    async with await client_for(store, ha) as client:
        token = (await client.get("/api/session")).json()["csrf"]
        response = await client.post("/api/backups/export", json={"passphrase": "long-test-passphrase"},
                                    headers={"X-Agentic-CSRF": token, "Origin": "http://home.test"})
        assert response.status_code == 200 and b"SQLite" not in response.content
        archive = tmp_path / "backup.enc"
        archive.write_bytes(response.content)
        target = tmp_path / "restored.sqlite3"
        module.restore(archive, target, "long-test-passphrase")
        assert target.exists()
        with pytest.raises(ValueError):
            module.restore(archive, target, "long-test-passphrase")


async def test_task_reply_preserves_request_and_deduplicates(store, ha):
    import asyncio

    class FixtureRunner:
        def __init__(self):
            self.prompts = []

        async def run(self, request):
            self.prompts.append(request["prompt"])
            return {"message": "補充房間" if len(self.prompts) == 1 else "目前設備均為 off", "needs_user": len(self.prompts) == 1}

    runner = FixtureRunner()
    app = create_app(store=store, ha=ha, runner=runner)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("172.30.32.2", 1234)),
                                base_url="http://home.test", headers={"X-Remote-User-Id": "owner"}) as client:
        token = (await client.get("/api/session")).json()["csrf"]
        client.headers.update({"X-Agentic-CSRF": token, "Origin": "http://home.test", "Idempotency-Key": "task"})
        task = (await client.post("/api/tasks", json={"message": "檢查設備", "provider": "codex"})).json()
        await asyncio.gather(*list(app.state.running.values()))
        assert store.get("task", task["id"])["state"] == "awaiting_user"
        response = await client.post(f"/api/tasks/{task['id']}/reply", json={"message": "測試區"}, headers={"Idempotency-Key": "reply"})
        assert response.status_code == 202
        await asyncio.gather(*list(app.state.running.values()))
        response = await client.post(f"/api/tasks/{task['id']}/reply", json={"message": "測試區"}, headers={"Idempotency-Key": "reply"})
        assert response.status_code == 202 and len(runner.prompts) == 2
        assert "檢查設備" in runner.prompts[-1] and "測試區" in runner.prompts[-1]
        assert not ha.writes


async def test_onboarding_does_not_count_excluded_entities_as_shared(store, ha):
    store.set_setting('policy', {'entities': [
        {'entity_id': 'input_boolean.agentic_motion_test', 'read': False, 'write': 'none', 'purpose': ''},
        {'entity_id': 'input_boolean.agentic_lamp_test', 'read': True, 'write': 'none', 'purpose': ''},
    ]})
    async with await client_for(store, ha) as client:
        result = (await client.get('/api/status')).json()
        assert result['policy_entities'] == 2
        assert result['policy_readable'] == 1


async def test_health_endpoint_integrations(store, ha):
    # Test 1: Initial state (ha disconnected, runner unpaired, notifications disabled)
    ha.connected = False
    ha.last_observed = None
    async with await client_for(store, ha, ip="127.0.0.1") as client:
        res1 = (await client.get("/health")).json()
        assert res1["integrations"]["ha"] == {"status": "unavailable", "last_poll_at": None}
        assert res1["integrations"]["runner"] == {"status": "unpaired", "last_poll_at": None}
        assert res1["integrations"]["notifications"] == {"status": "disabled", "last_poll_at": None}

    # Test 2: Active state with host_mode=True to test gateway channel status
    ha.connected = True
    ha.last_observed = "2026-10-20T08:00:00Z"
    store.set_setting("runner", {"host": "test-runner", "fingerprint": "a" * 64, "token": "tok"})

    app = create_app(store=store, ha=ha, host_mode=True)
    if app.state.notifications:
        app.state.notifications.integration.state = "ready"
        app.state.notifications.integration.last_seen = "2026-10-20T08:05:00Z"
    if app.state.gateway:
        app.state.gateway.states["line"] = "connected"
        app.state.gateway.channel_last_poll["line"] = "2026-10-20T08:06:00Z"
        store.set_setting("channel_line", {"enabled": True, "chats": ["c1"], "secret": "s"})

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)), base_url="http://localhost:8099") as client:
        res2 = (await client.get("/health")).json()
        assert res2["integrations"]["ha"] == {"status": "connected", "last_poll_at": "2026-10-20T08:00:00Z"}
        assert res2["integrations"]["runner"] == {"status": "paired", "last_poll_at": None}
        assert res2["integrations"]["notifications"] == {"status": "ready", "last_poll_at": "2026-10-20T08:05:00Z"}
        assert res2["integrations"]["line"] == {"status": "connected", "last_poll_at": "2026-10-20T08:06:00Z"}
        assert res2["integrations"]["telegram"] == {"status": "disabled", "last_poll_at": None}
        assert res2["integrations"]["discord"] == {"status": "disabled", "last_poll_at": None}
