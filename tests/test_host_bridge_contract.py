"""Fresh proof over the fixed Unix broker; local HTTP fixtures only."""
import asyncio

import pytest
from agentic_controller import bridge as bridge_module
from agentic_controller import host
from agentic_controller.app import create_app
from aiohttp import web
from fastapi import HTTPException

PROOF = {"controller_update": {"ingress_bridge_contract": 1}}


@pytest.fixture
async def broker(tmp_path, monkeypatch):
    socket = tmp_path / "broker.sock"
    calls, replies = [], {"/bridge-contract": (200, PROOF), "/status": (200, PROOF)}

    async def handle(request):
        calls.append((request.method, request.path))
        reply = replies[request.path]
        if callable(reply):
            return await reply()
        status, body = reply
        return web.json_response(body, status=status)

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    runner = web.AppRunner(app, shutdown_timeout=.1)
    await runner.setup()
    await web.UnixSite(runner, str(socket)).start()
    connector = host.aiohttp.UnixConnector

    def fixed_connector(*, path):
        assert path == "/run/agentic-home/host.sock"
        return connector(path=str(socket))

    monkeypatch.setattr(host.aiohttp, "UnixConnector", fixed_connector)
    monkeypatch.setenv("AGENTIC_DATA", str(tmp_path))
    try:
        yield calls, replies
    finally:
        await runner.cleanup()


async def test_new_host_proof_uses_only_fast_endpoint_and_process_local_cache(broker, store, ha):
    calls, _ = broker
    bridge = create_app(store=store, ha=ha, host_mode=True).state.ingress_bridge
    await bridge.require_host()
    await bridge.require_host()
    assert bridge.host_current() and calls == [("GET", "/bridge-contract")]
    # Expiry forces another actual Unix observation.
    bridge.host_valid_until = 0
    await bridge.require_host()
    assert calls == [("GET", "/bridge-contract")] * 2
    await bridge.close()


async def test_only_absent_fast_route_falls_back_to_actual_legacy_host_proof(broker, store, ha):
    calls, replies = broker
    replies["/bridge-contract"] = (404, {"detail": "Unknown route"})
    bridge = create_app(store=store, ha=ha, host_mode=True).state.ingress_bridge
    await bridge.require_host()
    assert bridge.host_current() and calls == [("GET", "/bridge-contract"), ("GET", "/status")]
    await bridge.close()


@pytest.mark.parametrize("status,body", [(403, {}), (503, {}), (302, {}), (200, {}),
    (200, {"controller_update": {"ingress_bridge_contract": True}}),
    (200, {"controller_update": {"ingress_bridge_contract": 2}})])
async def test_refused_failed_or_malformed_fast_proof_never_uses_legacy_fallback(broker, store, ha, status, body):
    calls, replies = broker
    replies["/bridge-contract"] = (status, body)
    store.set_setting("controller_update", PROOF["controller_update"])
    bridge = create_app(store=store, ha=ha, host_mode=True).state.ingress_bridge
    with pytest.raises(HTTPException) as error:
        await bridge.require_host()
    assert error.value.status_code == 503 and not bridge.host_current()
    assert calls == [("GET", "/bridge-contract")]
    await bridge.close()


@pytest.mark.parametrize("legacy", [False, True])
async def test_fast_and_legacy_proof_share_the_existing_total_deadline(broker, store, ha, monkeypatch, legacy):
    calls, replies = broker
    entered, release = asyncio.Event(), asyncio.Event()
    deadlines, budget = [], object()
    real_timeout = asyncio.timeout

    def controlled_timeout(delay):
        if delay is not budget:
            return real_timeout(delay)
        scope = real_timeout(None)
        deadlines.append(scope)
        return scope

    async def absent():
        assert len(deadlines) == 1
        return web.json_response({"detail": "Unknown route"}, status=404)

    async def blocked():
        entered.set()
        assert len(deadlines) == 1
        # Expire the actual shared cancellation scope only once the Unix
        # request arrived. CI scheduling need not fit two requests in 50 ms.
        deadlines[0].reschedule(asyncio.get_running_loop().time())
        await release.wait()
        return web.json_response(PROOF)

    if legacy:
        replies["/bridge-contract"] = absent
    replies["/status" if legacy else "/bridge-contract"] = blocked
    assert bridge_module.HOST_PROOF_TIMEOUT == 5
    monkeypatch.setattr(bridge_module, "HOST_PROOF_TIMEOUT", budget)
    monkeypatch.setattr(asyncio, "timeout", controlled_timeout)
    bridge = create_app(store=store, ha=ha, host_mode=True).state.ingress_bridge
    try:
        with pytest.raises(HTTPException, match="bridge_host_unavailable"):
            await asyncio.wait_for(bridge.require_host(), timeout=5)
        assert len(deadlines) == 1 and deadlines[0].expired()
        assert entered.is_set() and not bridge.host_current()
        assert calls == [("GET", "/bridge-contract")] + ([("GET", "/status")] if legacy else [])
    finally:
        release.set()
        await bridge.close()
