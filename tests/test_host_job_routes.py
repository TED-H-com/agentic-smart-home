"""Read exact saved lifecycle work; never replay actions or widen Ingress authority."""
import asyncio
import copy
import sqlite3
import threading

import pytest
from agentic_controller import host
from agentic_controller.app import create_app
from agentic_controller.store import Store
from fastapi import HTTPException
from test_host_sessions import KEY, client, failing_disk_app, host_app, login  # noqa: F401

JOB = "a" * 32


def receipt(state="running"):
    return {"id": JOB, "request": {"id": JOB, "action": "configure_lan"},
            "state": state, "at": 1792195200.0}


@pytest.mark.parametrize("state", ["running", "succeeded", "failed", "needs_reconcile"])
async def test_exact_receipt_bypasses_recent_list_and_projects_only_progress(host_app, monkeypatch, state):  # noqa: F811
    calls = []
    value = {**receipt(state), "message": "合成工作進度", "result": {"private": "not-for-progress"},
             "future_field": "ignored"}
    if state != "running":
        value["finished_at"] = 1792195260.0

    async def broker(method, path, body=None, *, store):
        calls.append((method, path, body))
        assert method == "GET" and path == "/jobs/" + JOB
        return copy.deepcopy(value)

    monkeypatch.setattr(host, "_host_call", broker)
    async with client(host_app) as browser:
        await login(browser)
        response = await browser.get("/api/host/jobs/" + JOB)
    assert response.status_code == 200
    assert response.json() == {k: v for k, v in value.items() if k not in {"result", "future_field"}}
    assert response.headers["cache-control"] == "no-store"
    assert calls == [("GET", "/jobs/" + JOB, None)]


@pytest.mark.parametrize("status", [404, 409, 503])
async def test_missing_or_unavailable_job_never_falls_back_or_resubmits(host_app, monkeypatch, status):  # noqa: F811
    calls = []

    async def broker(method, path, body=None, *, store):
        calls.append((method, path, body))
        raise HTTPException(status, "host_job_not_found" if status == 404 else "host_service_unavailable")

    monkeypatch.setattr(host, "_host_call", broker)
    async with client(host_app) as browser:
        await login(browser)
        response = await browser.get("/api/host/jobs/" + JOB)
    assert response.status_code == status
    assert calls == [("GET", "/jobs/" + JOB, None)]


@pytest.mark.parametrize("identifier", ["short", "a" * 101, "abc%20defghi", "abc%3Fdefghi", "abc%23defghi"])
async def test_invalid_lookup_key_cannot_select_a_broker_path(host_app, monkeypatch, identifier):  # noqa: F811
    async def forbidden(*args, **kwargs):
        pytest.fail("Invalid identifier reached the broker")

    monkeypatch.setattr(host, "_host_call", forbidden)
    async with client(host_app) as browser:
        await login(browser)
        response = await browser.get("/api/host/jobs/" + identifier)
    assert response.status_code == 400


@pytest.mark.parametrize("change", [
    {"id": "b" * 32}, {"request": {"id": "b" * 32, "action": "configure_lan"}},
    {"state": "complete"}, {"state": True}, {"request": None},
    {"request": {"id": JOB, "action": "shell"}},
    {"request": {"id": JOB, "action": "configure_lan", "command": "secret-command"}},
    {"request": {"id": JOB, "action": "configure_lan", "interface_id": "not-an-interface"}},
    {"at": True}, {"at": float("nan")}, {"at": "1792195200"},
    {"finished_at": float("inf")}, {"message": "x" * 501},
])
async def test_wrong_identity_or_malformed_receipt_is_not_completion(host_app, monkeypatch, change):  # noqa: F811
    async def broker(*args, **kwargs):
        return {**receipt(), **change}

    monkeypatch.setattr(host, "_host_call", broker)
    async with client(host_app) as browser:
        await login(browser)
        response = await browser.get("/api/host/jobs/" + JOB)
    assert response.status_code == 503
    assert response.json() == {"detail": "主機工作進度尚未確認，請稍後再讀取"}


async def test_saved_handle_is_not_authority_without_current_host_login(host_app, monkeypatch):  # noqa: F811
    async def forbidden(*args, **kwargs):
        pytest.fail("Unauthenticated lookup reached the broker")

    monkeypatch.setattr(host, "_host_call", forbidden)
    async with client(host_app) as browser:
        assert (await browser.get("/api/host/jobs/" + JOB)).status_code == 401
        saved = await login(browser)
        assert (await browser.post("/api/host/logout")).status_code == 200
        browser.headers["Cookie"] = "agentic_host=" + saved
        assert (await browser.get("/api/host/jobs/" + JOB)).status_code == 401


@pytest.mark.parametrize("change", ["session", "owner", "epoch", "access_key"])
async def test_scope_change_during_read_rejects_late_receipt(host_app, store, tmp_path, monkeypatch, change):  # noqa: F811
    async def broker(*args, **kwargs):
        if change == "session":
            with store.transaction() as tx:
                tx.execute("DELETE FROM records WHERE kind='host_session'")
        elif change == "access_key":
            (tmp_path / "access-key").write_text("another-host-key-" + "b" * 32)
        else:
            store.set_setting("owner" if change == "owner" else "hermes_web_epoch", "changed")
        return {**receipt(), "message": "must-not-be-delivered"}

    monkeypatch.setattr(host, "_host_call", broker)
    async with client(host_app) as browser:
        await login(browser)
        response = await browser.get("/api/host/jobs/" + JOB)
    assert response.status_code == 403
    assert "must-not-be-delivered" not in response.text
    if change == "access_key":
        # A late key rotation must commit revocation even though this is GET.
        with sqlite3.connect(store.path) as disk:
            assert disk.execute("SELECT COUNT(*) FROM records WHERE kind='host_session'").fetchone()[0] == 0


async def test_scope_is_rechecked_after_response_serialization(host_app, store, monkeypatch):  # noqa: F811
    async def broker(*args, **kwargs):
        return receipt()

    original = host.HostJobObservation.model_dump

    def serialize(value, *args, **kwargs):
        result = original(value, *args, **kwargs)
        store.set_setting("hermes_web_epoch", "revoked-before-send")
        return result

    monkeypatch.setattr(host, "_host_call", broker)
    monkeypatch.setattr(host.HostJobObservation, "model_dump", serialize)
    async with client(host_app) as browser:
        await login(browser)
        response = await browser.get("/api/host/jobs/" + JOB)
    assert response.status_code == 403
    assert JOB not in response.text


def rotate_during_read(monkeypatch, tmp_path, phase):
    def rotate():
        (tmp_path / "access-key").write_text("rotated-key-" + "c" * 32)

    async def broker(*args, **kwargs):
        if phase == "broker":
            rotate()
        return receipt()

    original = host.HostJobObservation.model_dump

    def serialize(value, *args, **kwargs):
        result = original(value, *args, **kwargs)
        if phase == "response":
            rotate()
        return result

    monkeypatch.setattr(host, "_host_call", broker)
    monkeypatch.setattr(host.HostJobObservation, "model_dump", serialize)


@pytest.mark.parametrize("phase", ["broker", "response"])
async def test_late_key_rotation_waits_for_disk_and_old_cookie_stays_revoked(
        host_app, store, ha, tmp_path, monkeypatch, phase):  # noqa: F811
    from agentic_controller import store_journal

    entered, release = threading.Event(), threading.Event()

    def hold():
        entered.set()
        if not release.wait(10):
            raise TimeoutError("test release not received")

    async with client(host_app) as browser:
        saved = await login(browser)
        rotate_during_read(monkeypatch, tmp_path, phase)
        monkeypatch.setattr(store_journal, "before_disk_commit", hold)
        request = asyncio.create_task(browser.get("/api/host/jobs/" + JOB))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            assert not request.done()
            with sqlite3.connect(store.path) as disk:
                assert disk.execute("SELECT COUNT(*) FROM records WHERE kind='host_session'").fetchone()[0] == 1
        finally:
            release.set()
            response = await asyncio.wait_for(request, 10)
        assert response.status_code == 403 and JOB not in response.text
        with sqlite3.connect(store.path) as disk:
            assert disk.execute("SELECT COUNT(*) FROM records WHERE kind='host_session'").fetchone()[0] == 0
    (tmp_path / "access-key").write_text(KEY)
    restored = Store(store.path)
    try:
        restarted = create_app(store=restored, ha=ha, host_mode=True)
        async with client(restarted) as browser:
            browser.headers["Cookie"] = "agentic_host=" + saved
            assert (await browser.get("/api/host/jobs/" + JOB)).status_code == 401
    finally:
        restored.close()


@pytest.mark.parametrize("phase", ["broker", "response"])
async def test_late_rotation_disk_failure_delivers_no_receipt(failing_disk_app, tmp_path, monkeypatch, phase):  # noqa: F811
    from agentic_controller import store_journal

    app, store = failing_disk_app

    def fail():
        raise OSError("synthetic disk failure")

    async with client(app) as browser:
        await login(browser)
        rotate_during_read(monkeypatch, tmp_path, phase)
        monkeypatch.setattr(store_journal, "before_disk_commit", fail)
        response = await browser.get("/api/host/jobs/" + JOB)
    assert response.status_code == 503 and store.failed
    assert JOB not in response.text and "set-cookie" not in response.headers
