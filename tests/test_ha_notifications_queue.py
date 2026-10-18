"""Pure queue/storage behavior; real HA loading is tested separately."""
import asyncio
import copy
import importlib
import json
import os
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

PACKAGE = "native_notification_unit"
if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(Path(__file__).parents[1] / "custom_components/agentic_home_notifications")]
    sys.modules[PACKAGE] = package
queue_module = importlib.import_module(PACKAGE + ".queue")
storage = importlib.import_module(PACKAGE + ".storage_io")
NotificationQueue, QueueError = queue_module.NotificationQueue, queue_module.QueueError
OWNER, CONTROLLER, SOURCE, EPOCH = "a" * 32, "b" * 32, "c" * 32, "d" * 32
REVISION = "e" * 64
ALIASES = [{"alias": "household", "label": "Home", "channel": "line", "capabilities": ["text"]}]
CHANNELS = [{"channel": "line", "transport": "connected", "login": "ready"}]


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 17, tzinfo=UTC)
        self.monotonic = 0

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)
        self.monotonic += seconds


class Disk:
    def __init__(self):
        self.data = None
        self.fail = None
        self.gate = None

    async def save(self, data):
        if self.fail == "before":
            raise QueueError("storage_unavailable")
        self.data = copy.deepcopy(data)
        if self.gate:
            await self.gate.wait()
        if self.fail == "after":
            raise QueueError("storage_unavailable")


@pytest.fixture
async def ready():
    disk, clock = Disk(), Clock()
    queue = NotificationQueue(disk.save, now=lambda: clock.value, tick=lambda: clock.monotonic)
    await queue.restore(SOURCE, EPOCH, None, allow_create=True)
    await queue.update_status(OWNER, CONTROLLER, True, REVISION, ALIASES, CHANNELS)
    return queue, disk, clock


async def reload(queue, disk, clock):
    restored = NotificationQueue(disk.save, now=lambda: clock.value, tick=lambda: clock.monotonic)
    await restored.restore(SOURCE, EPOCH, copy.deepcopy(disk.data))
    return restored


async def request_and_claim(queue, claim_id="f" * 32):
    await queue.enqueue(OWNER, "household", "Door opened")
    request = (await queue.pull(OWNER, CONTROLLER))["requests"][0]
    claim = {"controller_id": CONTROLLER, "source_id": SOURCE, "epoch": EPOCH,
             "request_id": request["id"], "digest": request["digest"],
             "routing_revision": REVISION, "claim_id": claim_id}
    return request, claim, await queue.claim(OWNER, **claim)


def acknowledgement(claim, state="submitted", reason=None):
    return {k: v for k, v in claim.items() if k != "routing_revision"} | {"state": state, "reason": reason}


async def test_enqueue_is_durable_and_digest_matches_controller_canonicalization(ready):
    queue, disk, _ = ready
    result = await queue.enqueue(OWNER, "household", "洗衣機完成 🧺")
    persisted = disk.data["queue"][0]["request"]
    assert result == {"request_id": persisted["id"], "state": "queued"}
    from agentic_controller.models import digest
    assert persisted["digest"] == digest({k: v for k, v in persisted.items() if k != "digest"})
    assert persisted["recipient"] == "household" and persisted["kind"] == "text"
    assert "Door opened" not in str(queue.status(OWNER))


async def test_claim_granted_once_even_after_restart_and_identical_repetition(ready):
    queue, disk, clock = ready
    request, claim, result = await request_and_claim(queue)
    assert result == {"granted": True, "request": request}
    assert disk.data["queue"][0]["claim"]["id"] == claim["claim_id"]
    assert await queue.claim(OWNER, **claim) == {"granted": False, "request": None}
    claim["claim_id"] = "1" * 32
    assert await queue.claim(OWNER, **claim) == {"granted": False, "request": None}
    queue = await reload(queue, disk, clock)
    assert not queue.available
    assert queue.status(OWNER)["last_delivery"]["state"] == "unknown"
    await queue.update_status(OWNER, CONTROLLER, True, REVISION, ALIASES, CHANNELS)
    assert await queue.pull(OWNER, CONTROLLER) == {"requests": []}
    assert await queue.claim(OWNER, **claim) == {"granted": False, "request": None}


async def test_concurrent_claims_have_only_one_durable_winner(ready):
    queue, _, _ = ready
    await queue.enqueue(OWNER, "household", "Once")
    item = (await queue.pull(OWNER, CONTROLLER))["requests"][0]
    fields = {"controller_id": CONTROLLER, "source_id": SOURCE, "epoch": EPOCH,
              "request_id": item["id"], "digest": item["digest"], "routing_revision": REVISION}
    results = await asyncio.gather(*[queue.claim(OWNER, **fields, claim_id=f"{i:032x}") for i in range(1, 9)])
    assert sum(result["granted"] for result in results) == 1


@pytest.mark.parametrize("offline", [
    {"transport": "disconnected", "login": "ready"},
    {"transport": "connected", "login": "needs_login"},
    {"transport": "connected", "login": "unknown"},
])
async def test_offline_first_eight_do_not_starve_later_ready_channel(ready, offline):
    queue, _, _ = ready
    aliases = ALIASES + [{"alias": "alerts", "label": "Alerts", "channel": "telegram", "capabilities": ["text"]}]
    channels = [{"channel": "line", **offline},
                {"channel": "telegram", "transport": "connected", "login": "not_required"}]
    revision = "9" * 64
    await queue.update_status(OWNER, CONTROLLER, True, revision, aliases, channels)
    for index in range(8):
        await queue.enqueue(OWNER, "household", f"Offline {index}")
    ready_item = await queue.enqueue(OWNER, "alerts", "Ready ninth request")
    for _ in range(3):
        requests = (await queue.pull(OWNER, CONTROLLER))["requests"]
        assert [item["id"] for item in requests] == [ready_item["request_id"]]
    assert queue.status(OWNER)["pending"] == 9
    await queue.update_status(OWNER, CONTROLLER, True, revision, aliases, CHANNELS + channels[1:])
    assert [item["message"] for item in (await queue.pull(OWNER, CONTROLLER))["requests"]] == [
        f"Offline {index}" for index in range(8)]


async def test_terminal_ack_exact_repeat_and_reconciliation_from_uncertain(ready):
    queue, disk, clock = ready
    _, claim, _ = await request_and_claim(queue)
    queue = await reload(queue, disk, clock)
    ack = acknowledgement(claim)
    assert await queue.ack(OWNER, **ack) == {"acknowledged": True}
    assert await queue.ack(OWNER, **ack) == {"acknowledged": True}
    assert queue.status(OWNER)["last_delivery"]["state"] == "submitted"
    assert queue.status(OWNER)["pending"] == 0
    with pytest.raises(QueueError, match="receipt_conflict"):
        await queue.ack(OWNER, **(ack | {"state": "unknown"}))
    assert "message" not in str(queue.status(OWNER))


@pytest.mark.parametrize("claimed", [False, True])
async def test_expiry_across_restart_retires_without_any_replay(ready, claimed):
    queue, disk, clock = ready
    if claimed:
        _, claim, _ = await request_and_claim(queue)
    else:
        await queue.enqueue(OWNER, "household", "Old")
    clock.advance(3601)
    queue = await reload(queue, disk, clock)
    await queue.update_status(OWNER, CONTROLLER, True, REVISION, ALIASES, CHANNELS)
    assert await queue.pull(OWNER, CONTROLLER) == {"requests": []}
    assert queue.status(OWNER)["pending"] == 0
    assert queue.status(OWNER)["last_delivery"]["state"] == ("unknown" if claimed else "not_sent")
    if claimed:
        assert await queue.claim(OWNER, **claim) == {"granted": False, "request": None}
        assert await queue.ack(OWNER, **acknowledgement(claim)) == {"acknowledged": False}


async def test_ack_after_request_expiry_false_even_for_earlier_confirmed_receipt(ready):
    queue, _, clock = ready
    _, claim, _ = await request_and_claim(queue)
    ack = acknowledgement(claim)
    assert (await queue.ack(OWNER, **ack))["acknowledged"]
    clock.advance(3600)
    assert await queue.ack(OWNER, **ack) == {"acknowledged": False}


async def test_routing_change_invalidates_pending_but_preserves_immutable_claim(ready):
    queue, _, _ = ready
    request, claim, _ = await request_and_claim(queue)
    await queue.enqueue(OWNER, "household", "Unclaimed")
    await queue.update_status(OWNER, CONTROLLER, True, "2" * 64,
                              [ALIASES[0] | {"channel": "telegram"}], CHANNELS)
    assert queue.status(OWNER)["pending"] == 1
    assert queue.data["queue"][0]["request"] == request
    assert (await queue.pull(OWNER, CONTROLLER))["requests"] == []
    assert (await queue.ack(OWNER, **acknowledgement(claim, "not_sent", "scope_changed")))["acknowledged"]
    assert queue.status(OWNER)["pending"] == 0


@pytest.mark.parametrize("change", [{"aliases": []}, {"enabled": False}])
async def test_alias_changes_under_same_revision_rejected(ready, change):
    queue, _, _ = ready
    args = {"controller_id": CONTROLLER, "enabled": True, "routing_revision": REVISION,
            "aliases": ALIASES, "channels": CHANNELS} | change
    with pytest.raises(QueueError, match="routing_conflict"):
        await queue.update_status(OWNER, **args)


@pytest.mark.parametrize("change", ["owner", "controller"])
async def test_binding_cannot_be_replaced(ready, change):
    queue, disk, _ = ready
    before = copy.deepcopy(disk.data)
    with pytest.raises(QueueError, match="needs_rebind"):
        await queue.update_status("1" * 32 if change == "owner" else OWNER,
                                  "2" * 32 if change == "controller" else CONTROLLER,
                                  True, "3" * 64, ALIASES, CHANNELS)
    assert disk.data == before


async def test_heartbeat_is_runtime_only_and_expiration_blocks_enqueue(ready):
    queue, disk, clock = ready
    before = copy.deepcopy(disk.data)
    clock.advance(59)
    await queue.update_status(OWNER, CONTROLLER, True, REVISION, ALIASES, CHANNELS)
    assert disk.data == before and queue.available
    clock.advance(60)
    assert not queue.available
    with pytest.raises(QueueError, match="stale_heartbeat"):
        await queue.enqueue(OWNER, "household", "No send")


async def test_queue_bound_includes_claims_and_recovers_capacity_after_expiry(ready):
    queue, _, clock = ready
    await request_and_claim(queue)
    for _ in range(31):
        await queue.enqueue(OWNER, "household", "Pending")
    assert len((await queue.pull(OWNER, CONTROLLER))["requests"]) == 8
    with pytest.raises(QueueError, match="queue_full"):
        await queue.enqueue(OWNER, "household", "Overflow")
    clock.advance(3601)
    await queue.update_status(OWNER, CONTROLLER, True, REVISION, ALIASES, CHANNELS)
    await queue.enqueue(OWNER, "household", "New")
    assert queue.status(OWNER)["pending"] == 1


async def test_recent_receipts_are_bounded_without_keeping_message_contents(ready):
    queue, _, _ = ready
    for index in range(70):
        _, claim, _ = await request_and_claim(queue, f"{index:032x}")
        await queue.ack(OWNER, **acknowledgement(claim))
    assert len(queue.data["recent"]) == 64
    assert "Door opened" not in json.dumps(queue.data["recent"])


@pytest.mark.parametrize("defect", ["before", "after"])
async def test_failed_claim_storage_never_grants_and_fences_process(ready, defect):
    queue, disk, clock = ready
    await queue.enqueue(OWNER, "household", "Once")
    item = (await queue.pull(OWNER, CONTROLLER))["requests"][0]
    fields = {"controller_id": CONTROLLER, "source_id": SOURCE, "epoch": EPOCH,
              "request_id": item["id"], "digest": item["digest"], "routing_revision": REVISION,
              "claim_id": "f" * 32}
    disk.fail = defect
    with pytest.raises(QueueError, match="storage_unavailable"):
        await queue.claim(OWNER, **fields)
    with pytest.raises(QueueError, match="storage_unavailable"):
        await queue.claim(OWNER, **fields)
    disk.fail = None
    restored = await reload(queue, disk, clock)
    await restored.update_status(OWNER, CONTROLLER, True, REVISION, ALIASES, CHANNELS)
    if defect == "after":
        assert not (await restored.claim(OWNER, **fields))["granted"]


async def test_cancelled_claim_after_disk_write_is_never_regranted(ready):
    queue, disk, clock = ready
    await queue.enqueue(OWNER, "household", "Once")
    item = (await queue.pull(OWNER, CONTROLLER))["requests"][0]
    fields = {"controller_id": CONTROLLER, "source_id": SOURCE, "epoch": EPOCH,
              "request_id": item["id"], "digest": item["digest"], "routing_revision": REVISION,
              "claim_id": "f" * 32}
    disk.gate = asyncio.Event()
    task = asyncio.create_task(queue.claim(OWNER, **fields))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not queue.healthy
    disk.gate = None
    restored = await reload(queue, disk, clock)
    await restored.update_status(OWNER, CONTROLLER, True, REVISION, ALIASES, CHANNELS)
    assert await restored.claim(OWNER, **fields) == {"granted": False, "request": None}


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(schema=2),
    lambda d: d.update(source_id="0" * 32),
    lambda d: d.update(owner_id=None),
    lambda d: d["queue"][0]["request"].update(message="changed"),
    lambda d: d["queue"][0].update(claim={"id": "bad", "at": "bad"}),
    lambda d: d.update(extra="unexpected"),
])
async def test_corrupted_state_is_rejected_without_reset(ready, mutate):
    queue, disk, clock = ready
    await queue.enqueue(OWNER, "household", "Original")
    mutate(disk.data)
    before = copy.deepcopy(disk.data)
    with pytest.raises(QueueError, match="storage_unavailable"):
        await reload(queue, disk, clock)
    assert disk.data == before


async def test_missing_previously_initialized_store_is_not_a_new_enrollment(ready):
    queue, disk, clock = ready
    disk.data = None
    with pytest.raises(QueueError, match="storage_unavailable"):
        await reload(queue, disk, clock)


@pytest.mark.parametrize("recipient,message", [("raw-chat-id", "hello"), ("unlisted", "hello"),
                                               ("household", ""), ("household", "x" * 1901),
                                               ("household", "\ud800")])
async def test_bad_recipient_or_text_cannot_enter_queue(ready, recipient, message):
    queue, _, _ = ready
    with pytest.raises(QueueError):
        await queue.enqueue(OWNER, recipient, message)
    assert queue.status(OWNER)["pending"] == 0


@pytest.mark.parametrize("field,value", [("digest", "0" * 64), ("source_id", "1" * 32),
                                       ("epoch", "2" * 32), ("routing_revision", "3" * 64)])
async def test_claim_cannot_change_request_identity(ready, field, value):
    queue, _, _ = ready
    _, claim, _ = await request_and_claim(queue)
    with pytest.raises(QueueError):
        await queue.claim(OWNER, **(claim | {field: value}))


def stored_file(tmp_path, data=None):
    path = tmp_path / "queue"
    envelope = {"version": 1, "minor_version": 1, "key": "test", "data": data or {"revision": 1}}
    path.write_text(json.dumps(envelope))
    path.chmod(0o600)
    return path, envelope["data"]


def test_commit_readback_verifies_payload_and_syncs_file_and_directory(tmp_path, monkeypatch):
    path, expected = stored_file(tmp_path)
    sync, calls = os.fsync, []
    def tracked(fd):
        calls.append(os.fstat(fd).st_mode)
        sync(fd)
    monkeypatch.setattr(storage.os, "fsync", tracked)
    assert storage.read_owned(path, "test", expected=expected) == expected
    assert len(calls) == 2
    with pytest.raises(QueueError, match="storage_unavailable"):
        storage.read_owned(path, "test", expected={"revision": 2})


@pytest.mark.parametrize("defect", ["symlink", "public", "oversize", "malformed", "duplicate", "wrong_key"])
def test_storage_validation_rejects_unsafe_or_corrupt_file(tmp_path, defect):
    path, _ = stored_file(tmp_path)
    if defect == "symlink":
        target = tmp_path / "target"
        path.rename(target)
        path.symlink_to(target)
    elif defect == "public":
        path.chmod(0o644)
    elif defect == "oversize":
        with path.open("r+b") as stream:
            stream.truncate(storage.MAX_STORAGE_BYTES + 1)
    elif defect == "malformed":
        path.write_text("{")
    elif defect == "duplicate":
        path.write_text('{"version":1,"version":1}')
    else:
        value = json.loads(path.read_text())
        value["key"] = "another_integration"
        path.write_text(json.dumps(value))
    with pytest.raises((OSError, ValueError, QueueError)):
        storage.read_owned(path, "test")


def test_directory_sync_failure_is_not_acknowledged(tmp_path, monkeypatch):
    path, expected = stored_file(tmp_path)
    calls = []
    def failing(fd):
        calls.append(fd)
        if len(calls) == 2:
            raise OSError("synthetic directory fsync failure")
    monkeypatch.setattr(storage.os, "fsync", failing)
    with pytest.raises(OSError, match="directory fsync"):
        storage.read_owned(path, "test", expected=expected)
