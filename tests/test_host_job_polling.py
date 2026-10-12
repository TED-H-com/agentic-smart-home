"""Exact lifecycle job observation survives transport loss without replaying effects."""
import http.client
import io
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from agentic_host import cli
from agentic_host.daemon import Jobs
from agentic_host.manager import HostError

JOB_ID = "8" * 32


def receipt(state="running", *, id=JOB_ID, action="snapshot"):
    result = {"id": id, "request": {"id": id, "action": action, "snapshot": None}, "state": state, "at": 1.0}
    if state == "succeeded":
        result.update(result={"id": "snapshot-fixture"}, finished_at=2.0)
    return result


def test_started_snapshot_survives_read_timeout_without_reposting(monkeypatch, capsys):
    calls, reads, accepted = [], [], []

    def broker(method, path, body=None):
        calls.append((method, path, body))
        if method == "POST":
            assert path == "/actions" and not accepted
            accepted.append({"id": body["id"], "request": dict(body), "state": "running", "at": 1.0})
            return dict(accepted[0])
        reads.append(path)
        if len(reads) == 1:
            # The broker already accepted the snapshot. Only observation failed.
            raise TimeoutError("private transport detail")
        return {**accepted[0], "state": "succeeded", "result": {"id": "snapshot-fixture"}, "finished_at": 2.0}

    monkeypatch.setattr(cli, "call", broker)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(sys, "argv", ["agentic-home", "snapshot"])
    cli.main()
    assert len([call for call in calls if call[0] == "POST"]) == 1
    assert reads == ["/jobs/" + accepted[0]["id"]] * 2
    assert '"state": "succeeded"' in capsys.readouterr().out


@pytest.mark.parametrize("error", [TimeoutError("private"), ConnectionRefusedError("private"),
                                  FileNotFoundError("private"), http.client.IncompleteRead(b"private", 20),
                                  cli.BrokerError(502, "private"), cli.BrokerError(503, "private"),
                                  cli.BrokerError(504, "private")])
def test_transient_job_observation_is_bounded_without_mutation(monkeypatch, error):
    calls, sleeps = [], []
    def broker(method, path, body=None):
        calls.append((method, path, body))
        raise error
    monkeypatch.setattr(cli, "call", broker)
    monkeypatch.setattr(cli.time, "sleep", sleeps.append)
    with pytest.raises(cli.JobObservationError, match="^host_job_observation_unavailable$"):
        cli.read_job(JOB_ID)
    assert calls == [("GET", "/jobs/" + JOB_ID, None)] * 3
    assert sleeps == [2, 2]


@pytest.mark.parametrize("error,code", [(cli.BrokerError(404, "private"), "host_job_not_found"),
                                      (cli.BrokerError(403, "private"), "host_job_read_refused"),
                                      (cli.BrokerError(409, "private"), "host_job_read_refused"),
                                      (PermissionError("private"), "host_job_read_refused"),
                                      (http.client.BadStatusLine("private"), "host_job_response_invalid"),
                                      (RecursionError("private"), "host_job_response_invalid"),
                                      (ValueError("private"), "host_job_response_invalid")])
def test_missing_refused_or_invalid_observation_is_never_retried(monkeypatch, error, code):
    calls = []
    def broker(method, path, body=None):
        calls.append((method, path, body))
        raise error
    monkeypatch.setattr(cli, "call", broker)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: pytest.fail("No retry for rejected observation"))
    with pytest.raises(cli.JobObservationError, match="^" + code + "$"):
        cli.read_job(JOB_ID)
    assert calls == [("GET", "/jobs/" + JOB_ID, None)]


@pytest.mark.parametrize("change", [
    {"id": "9" * 32}, {"state": "done"}, {"state": True}, {"state": "cancelled"},
    {"request": None}, {"request": {"id": JOB_ID, "action": "shell"}},
    {"request": {"id": "9" * 32, "action": "snapshot"}},
    {"request": {"id": JOB_ID, "action": "snapshot", "snapshot": True}},
    {"request": {"id": JOB_ID, "action": "snapshot", "command": "private"}},
    {"state": "succeeded"},
])
def test_job_identity_request_and_known_state_are_required(monkeypatch, change):
    monkeypatch.setattr(cli, "call", lambda *args: {**receipt(), **change})
    with pytest.raises(cli.JobObservationError, match="^host_job_response_invalid$"):
        cli.read_job(JOB_ID)


def test_wait_never_adopts_a_different_request_for_same_id(monkeypatch):
    monkeypatch.setattr(cli, "call", lambda *args: receipt("succeeded", action="shutdown"))
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    with pytest.raises(cli.JobObservationError, match="^host_job_response_invalid$"):
        cli.wait_job(JOB_ID, receipt())


def test_submit_ack_loss_preserves_handle_and_never_reposts(monkeypatch, capsys):
    effects = []
    def broker(method, path, body=None):
        assert method == "POST" and path == "/actions"
        effects.append(dict(body))
        raise TimeoutError("private transport detail")
    monkeypatch.setattr(cli, "call", broker)
    monkeypatch.setattr(sys, "argv", ["agentic-home", "snapshot"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2 and len(effects) == 1
    output = capsys.readouterr()
    assert output.out == "Operation " + effects[0]["id"] + "\n"
    assert "completion is unconfirmed" in output.err and "wait --id " + effects[0]["id"] in output.err
    assert "private" not in output.err


def test_read_exhaustion_does_not_mark_accepted_job_failed(monkeypatch, capsys):
    accepted, calls = [], []
    def broker(method, path, body=None):
        calls.append((method, path))
        if method == "POST":
            accepted.append(receipt(id=body["id"]))
            return accepted[0]
        raise TimeoutError("private")
    monkeypatch.setattr(cli, "call", broker)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(sys, "argv", ["agentic-home", "snapshot"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2 and accepted[0]["state"] == "running"
    assert calls == [("POST", "/actions")] + [("GET", "/jobs/" + accepted[0]["id"])] * 3
    assert "No operation was resubmitted" in capsys.readouterr().err


@pytest.mark.parametrize("error", [http.client.BadStatusLine("private"), RecursionError("private")])
def test_resume_protocol_errors_have_safe_recovery_instruction(monkeypatch, capsys, error):
    def broker(*args):
        raise error
    monkeypatch.setattr(cli, "call", broker)
    monkeypatch.setattr(sys, "argv", ["agentic-home", "wait", "--id", JOB_ID])
    with pytest.raises(SystemExit) as failure:
        cli.main()
    assert failure.value.code == 2
    output = capsys.readouterr()
    assert "host_job_response_invalid" in output.err and "wait --id " + JOB_ID in output.err
    assert "private" not in output.err and not output.out


def test_malformed_submit_error_receipt_still_preserves_readonly_recovery(monkeypatch, capsys):
    response = io.BytesIO(b'["private"]')
    response.status = 503
    requests = []
    connection = SimpleNamespace(request=lambda *args, **kwargs: requests.append(args),
                                 getresponse=lambda: response, close=lambda: None)
    monkeypatch.setattr(cli, "Connection", lambda host: connection)
    monkeypatch.setattr(sys, "argv", ["agentic-home", "snapshot"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2 and len(requests) == 1 and requests[0][0] == "POST"
    output = capsys.readouterr()
    id = output.out.removeprefix("Operation ").strip()
    assert cli.valid_job_id(id) and "wait --id " + id in output.err and "private" not in output.err


@pytest.mark.parametrize("state,exit_code", [("succeeded", None), ("failed", 1), ("needs_reconcile", 1)])
def test_wait_resumes_exact_persisted_job_and_preserves_terminal_state(monkeypatch, capsys, state, exit_code):
    calls = []
    def broker(method, path, body=None):
        calls.append((method, path, body))
        return receipt(state)
    monkeypatch.setattr(cli, "call", broker)
    monkeypatch.setattr(sys, "argv", ["agentic-home", "wait", "--id", JOB_ID])
    if exit_code is None:
        cli.main()
    else:
        with pytest.raises(SystemExit) as error:
            cli.main()
        assert error.value.code == exit_code
    assert calls == [("GET", "/jobs/" + JOB_ID, None)]
    assert json.loads(capsys.readouterr().out)["state"] == state


def test_job_once_reports_running_without_waiting_or_dispatching(monkeypatch, capsys):
    calls = []
    def broker(method, path, body=None):
        calls.append((method, path, body))
        return receipt()
    monkeypatch.setattr(cli, "call", broker)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: pytest.fail("job is a single read"))
    monkeypatch.setattr(sys, "argv", ["agentic-home", "job", "--id", JOB_ID])
    cli.main()
    assert calls == [("GET", "/jobs/" + JOB_ID, None)]
    assert json.loads(capsys.readouterr().out)["state"] == "running"


@pytest.mark.parametrize("command", ["job", "wait"])
@pytest.mark.parametrize("id", [None, "short", "../private", "x" * 101, "valid_id?query"])
def test_resume_rejects_invalid_id_before_io(monkeypatch, command, id):
    monkeypatch.setattr(cli, "call", lambda *args: pytest.fail("No broker request for invalid ID"))
    monkeypatch.setattr(sys, "argv", ["agentic-home", command] + (["--id", id] if id else []))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


def test_resume_never_recreates_missing_job(monkeypatch, capsys):
    calls = []
    def broker(method, path, body=None):
        calls.append((method, path, body))
        raise cli.BrokerError(404, "private")
    monkeypatch.setattr(cli, "call", broker)
    monkeypatch.setattr(sys, "argv", ["agentic-home", "wait", "--id", JOB_ID])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2 and calls == [("GET", "/jobs/" + JOB_ID, None)]
    assert "host_job_not_found" in capsys.readouterr().err


def test_job_cache_returns_detached_receipt_while_real_job_is_running(tmp_path):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def action(name, snapshot):
        assert name == "snapshot" and snapshot is None
        entered.set()
        try:
            assert release.wait(3)
            return {"id": "snapshot-fixture"}
        finally:
            finished.set()
    manager = SimpleNamespace(root=tmp_path, action=action)
    jobs = Jobs(manager)
    jobs.start(receipt()["request"])
    try:
        assert entered.wait(2)
        value = jobs.get(JOB_ID)
        assert value["state"] == "running"
        value["request"]["action"] = "shutdown"
        value["state"] = "succeeded"
        assert jobs.get(JOB_ID)["request"]["action"] == "snapshot"
        assert jobs.get(JOB_ID)["state"] == "running"
        assert jobs.get("missing_id") is None
    finally:
        release.set()
        assert finished.wait(3)
        # Wait for this owned background thread's durable receipt, not external IO.
        import time
        deadline = time.monotonic() + 3
        while jobs.get(JOB_ID)["state"] == "running" and time.monotonic() < deadline:
            time.sleep(.01)
    assert jobs.get(JOB_ID)["state"] == "succeeded"
    assert json.loads(jobs.path.read_text())[JOB_ID]["state"] == "succeeded"


def test_restart_receipt_is_needs_reconcile_without_live_status_or_action(tmp_path):
    (tmp_path / "jobs.json").write_text(json.dumps({JOB_ID: receipt()}))
    manager = SimpleNamespace(root=tmp_path)
    jobs = Jobs(manager)
    assert jobs.get(JOB_ID)["state"] == "needs_reconcile"
    with pytest.raises(HostError, match="Invalid operation id"):
        jobs.get("../private")


@pytest.mark.parametrize("raw", [b"", b"{partial", b"[" * 3000 + b"]" * 3000,
                                 b"x" * (cli.MAX_JOB_RESPONSE_BYTES + 1)])
def test_http_job_response_is_bounded_and_malformed_never_success(monkeypatch, raw):
    response = io.BytesIO(raw)
    response.status = 200
    closed = []
    connection = SimpleNamespace(request=lambda *args, **kwargs: None, getresponse=lambda: response, close=lambda: closed.append(True))
    monkeypatch.setattr(cli, "Connection", lambda host: connection)
    with pytest.raises(cli.JobObservationError, match="host_job_response_invalid"):
        cli.read_job(JOB_ID)
    assert closed == [True]


def test_socket_per_request_timeout_remains_thirty_seconds(monkeypatch):
    observed = []
    socket = SimpleNamespace(settimeout=lambda value: observed.append(("timeout", value)),
                             connect=lambda path: observed.append(("connect", path)))
    monkeypatch.setattr(cli.socket, "socket", lambda *args: socket)
    cli.Connection("localhost").connect()
    assert observed == [("timeout", 30), ("connect", "/run/agentic-home/host.sock")]
