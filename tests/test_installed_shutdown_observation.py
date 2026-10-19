"""A lost shutdown reply may only resume its exact existing read-only job."""

import copy
import json
import subprocess

import pytest
from installed_update_probe import cli_job_unconfirmed
from test_linux_install import installed_shutdown

ID = "a" * 32
OTHER = "b" * 32
PRIVATE = "private-stderr-host-path-and-token"


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


def message(code="host_job_submit_unconfirmed", identifier=ID):
    return (
        f"Job {identifier}: completion is unconfirmed ({code}). No operation was resubmitted. "
        f"Inspect with 'agentic-home job --id {identifier}' or resume with "
        f"'agentic-home wait --id {identifier}'.\n"
    )


def receipt():
    return {
        "id": ID,
        "request": {"id": ID, "action": "shutdown", "snapshot": None},
        "state": "succeeded",
        "result": {"vm": "shut off"},
    }


def pending(code="host_job_submit_unconfirmed"):
    return subprocess.CompletedProcess([], 2, f"Operation {ID}\n", PRIVATE + "\n" + message(code))


def successful(*, initial=False):
    return subprocess.CompletedProcess(
        [], 0, (f"Operation {ID}\n" if initial else "") + json.dumps(receipt()), ""
    )


def sequence(responses, elapsed):
    clock, calls = Clock(), []

    def run(args, **kwargs):
        index = len(calls)
        calls.append((list(args), kwargs))
        assert kwargs["capture_output"] is True and kwargs["text"] is True
        clock.value += elapsed[index]
        value = responses[index]
        if isinstance(value, BaseException):
            raise value
        value.args = list(args)
        return value

    return clock, calls, run


def test_acknowledged_shutdown_requires_no_second_command():
    clock, calls, run = sequence([successful(initial=True)], [12])
    result = installed_shutdown(["ssh", "fixture"], run=run, clock=clock)
    assert [args[-1] for args, _ in calls] == ["agentic-home shutdown"]
    assert result["status"] == "passed" and result["vm_shut_off"] is True
    assert result["mutation_count"] == 1 and result["wait_count"] == 0


@pytest.mark.parametrize("code", ["host_job_submit_unconfirmed", "host_job_observation_unavailable"])
def test_same_id_wait_retains_uncertainty_and_uses_only_remaining_deadline(code):
    clock, calls, run = sequence([pending(code), successful()], [34, 15])
    observation, checkpoints = {}, []
    result = installed_shutdown(
        ["ssh", "fixture"],
        run=run,
        clock=clock,
        observation=observation,
        checkpoint=lambda: checkpoints.append(copy.deepcopy(observation)),
    )
    assert [args[-1] for args, _ in calls] == ["agentic-home shutdown", "agentic-home wait --id " + ID]
    assert [kwargs["timeout"] for _, kwargs in calls] == [240, 206]
    assert result["status"] == "passed" and result["vm_shut_off"] is True
    assert result["operation_id"] == ID and result["initial_cli_error"] == code
    assert result["initial_returncode"] == 2 and result["continuation_returncode"] == 0
    assert result["mutation_count"] == result["wait_count"] == 1 and result["elapsed_seconds"] == 49
    assert checkpoints[1]["status"] == "running" and checkpoints[1]["initial_cli_error"] == code
    assert checkpoints[-1] == result and PRIVATE not in json.dumps(checkpoints)


@pytest.mark.parametrize(
    "defect",
    [
        "not_exit_two",
        "no_handle",
        "two_handles",
        "wrong_handle",
        "bad_handle",
        "wrong_message_id",
        "wrong_recovery_id",
        "unknown_code",
        "duplicate_message",
        "invalid_response",
        "not_found",
        "refused",
        "extra_stdout",
        "truncated_message",
    ],
)
def test_unconfirmed_call_without_exact_recoverable_contract_never_resubmits_or_waits(defect):
    first = pending()
    if defect == "not_exit_two":
        first.returncode = 1
    elif defect == "no_handle":
        first.stdout = ""
    elif defect == "two_handles":
        first.stdout += first.stdout
    elif defect == "wrong_handle":
        first.stdout = f"Operation {OTHER}\n"
    elif defect == "bad_handle":
        first.stdout = "Operation a; shutdown\n"
    elif defect == "wrong_message_id":
        first.stderr = message(identifier=OTHER)
    elif defect == "wrong_recovery_id":
        first.stderr = message().replace("wait --id " + ID, "wait --id " + OTHER)
    elif defect == "unknown_code":
        first.stderr = message("host_job_" + PRIVATE)
    elif defect == "duplicate_message":
        first.stderr = message() * 2
    elif defect == "invalid_response":
        first.stderr = message("host_job_response_invalid")
    elif defect == "not_found":
        first.stderr = message("host_job_not_found")
    elif defect == "refused":
        first.stderr = message("host_job_read_refused")
    elif defect == "extra_stdout":
        first.stdout += PRIVATE
    else:
        first.stderr = message()[:-5]
    clock, calls, run = sequence([first], [30])
    observation = {}
    with pytest.raises(subprocess.CalledProcessError):
        installed_shutdown(["ssh"], run=run, clock=clock, observation=observation)
    assert len(calls) == 1 and observation["wait_count"] == 0
    assert observation["status"] == "failed" and observation["vm_shut_off"] is False
    assert PRIVATE not in json.dumps(observation)


@pytest.mark.parametrize(
    "defect",
    [
        "missing_id",
        "wrong_id",
        "request_id",
        "request_action",
        "request_snapshot",
        "missing_snapshot",
        "extra_request",
        "running",
        "failed",
        "needs_reconcile",
        "unknown",
        "running_vm",
        "missing_result",
        "extra_result",
        "malformed_json",
        "duplicate_json",
        "oversized_json",
    ],
)
def test_resumed_receipt_must_prove_exact_shutdown_and_terminal_vm_state(defect):
    value = receipt()
    if defect == "missing_id":
        value.pop("id")
    elif defect == "wrong_id":
        value["id"] = OTHER
    elif defect == "request_id":
        value["request"]["id"] = OTHER
    elif defect == "request_action":
        value["request"]["action"] = "start"
    elif defect == "request_snapshot":
        value["request"]["snapshot"] = "other"
    elif defect == "missing_snapshot":
        value["request"].pop("snapshot")
    elif defect == "extra_request":
        value["request"]["interface_id"] = None
    elif defect in ("running", "failed", "needs_reconcile", "unknown"):
        value["state"] = defect
    elif defect == "running_vm":
        value["result"]["vm"] = "running"
    elif defect == "missing_result":
        value.pop("result")
    elif defect == "extra_result":
        value["result"]["private"] = PRIVATE
    raw = json.dumps(value)
    if defect == "malformed_json":
        raw = PRIVATE
    elif defect == "duplicate_json":
        raw = raw[:-1] + ',"state":"succeeded"}'
    elif defect == "oversized_json":
        raw += " " * 65536
    clock, calls, run = sequence([pending(), subprocess.CompletedProcess([], 0, raw, PRIVATE)], [30, 1])
    observation = {}
    with pytest.raises(subprocess.CalledProcessError) as caught:
        installed_shutdown(["ssh"], run=run, clock=clock, observation=observation)
    assert caught.value.returncode == 2 and caught.value.cmd[-1] == "agentic-home shutdown"
    assert len(calls) == 2 and observation["error_code"] == "shutdown_receipt_invalid"
    assert observation["status"] == "failed" and observation["vm_shut_off"] is False
    assert PRIVATE not in json.dumps(observation)


def test_zero_exit_without_exact_initial_receipt_is_not_success():
    value = receipt()
    value["id"] = OTHER
    first = successful(initial=True)
    first.stdout = f"Operation {ID}\n" + json.dumps(value)
    clock, calls, run = sequence([first], [1])
    with pytest.raises(AssertionError, match="^shutdown_receipt_invalid$"):
        installed_shutdown(["ssh"], run=run, clock=clock)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "defect",
    [
        "initial_timeout",
        "initial_budget_spent",
        "wait_timeout",
        "wait_finishes_late",
        "wait_exit_two",
        "wait_exit_one",
        "wait_oserror",
    ],
)
def test_read_failure_or_deadline_never_extends_budget_or_replays_shutdown(defect):
    responses, elapsed = [pending(), successful()], [30, 1]
    if defect == "initial_timeout":
        responses[0] = subprocess.TimeoutExpired("private", 240)
        elapsed[0] = 240
    elif defect == "initial_budget_spent":
        elapsed[0] = 240
    elif defect == "wait_timeout":
        responses[1] = subprocess.TimeoutExpired("private", 210)
        elapsed[1] = 210
    elif defect == "wait_finishes_late":
        elapsed[1] = 211
    elif defect == "wait_exit_two":
        responses[1] = subprocess.CompletedProcess([], 2, "", message("host_job_observation_unavailable"))
    elif defect == "wait_exit_one":
        responses[1] = subprocess.CompletedProcess(
            [], 1, json.dumps({**receipt(), "state": "failed"}), PRIVATE
        )
    else:
        responses[1] = OSError(PRIVATE)
    clock, calls, run = sequence(responses, elapsed)
    observation = {}
    with pytest.raises((subprocess.CalledProcessError, subprocess.TimeoutExpired)):
        installed_shutdown(["ssh"], run=run, clock=clock, observation=observation)
    assert sum(args[-1] == "agentic-home shutdown" for args, _ in calls) == 1
    assert len(calls) == (1 if defect.startswith("initial") else 2)
    assert observation["status"] == "failed" and observation["vm_shut_off"] is False
    assert calls[0][1]["timeout"] == 240
    if len(calls) == 2:
        assert calls[1][1]["timeout"] == 210
    if defect == "wait_exit_two":
        assert observation["continuation_cli_error"] == "host_job_observation_unavailable"
    assert PRIVATE not in json.dumps(observation)


def test_checkpoint_failure_does_not_resubmit_or_hide_valid_shutdown_receipt():
    clock, calls, run = sequence([pending(), successful()], [30, 1])

    def broken():
        raise OSError(PRIVATE)

    result = installed_shutdown(["ssh"], run=run, clock=clock, checkpoint=broken)
    assert result["status"] == "passed" and result["checkpoint_failed"] is True and len(calls) == 2
    assert PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_cli_message_parser_rejects_unbounded_or_invalid_encoding(stream):
    values = {"stdout": f"Operation {ID}\n", "stderr": message()}
    for invalid in (b"\xff", "x" * 65537, None):
        assert cli_job_unconfirmed(**{**values, stream: invalid}) is None


def test_wait_classification_requires_empty_stdout_and_exact_expected_id():
    assert cli_job_unconfirmed("", message(), expected_id=ID) == {
        "id": ID,
        "error_code": "host_job_submit_unconfirmed",
    }
    assert cli_job_unconfirmed("", message(), expected_id=OTHER) is None
    assert cli_job_unconfirmed(f"Operation {ID}\n", message(), expected_id=ID) is None
