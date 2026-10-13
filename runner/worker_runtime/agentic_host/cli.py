import argparse
import http.client
import json
import re
import socket
import sys
import time
import uuid
from pathlib import Path

JOB_ACTIONS = {"provision", "start", "shutdown", "snapshot", "restore", "bootstrap_hermes"}
JOB_STATES = {"running", "succeeded", "failed", "needs_reconcile"}
JOB_READ_ATTEMPTS = 3
JOB_POLL_SECONDS = 2
MAX_JOB_RESPONSE_BYTES = 262144


class BrokerError(ValueError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class JobObservationError(ValueError):
    pass


class Connection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(30)
        self.sock.connect("/run/agentic-home/host.sock")


def call(method, path, body=None):
    connection = Connection("localhost")
    try:
        connection.request(method, path, body=json.dumps(body) if body is not None else None,
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        if path.startswith("/jobs/"):
            raw = response.read(MAX_JOB_RESPONSE_BYTES + 1)
            if len(raw) > MAX_JOB_RESPONSE_BYTES:
                raise JobObservationError("host_job_response_invalid")
            if response.status >= 300:
                # Status determines retryability; never relay private broker text.
                raise BrokerError(response.status, "host_job_read_failed")
            data = json.loads(raw)
        else:
            data = json.load(response)
        if response.status >= 300:
            raise BrokerError(response.status, data.get("detail", "Host operation failed") if isinstance(data, dict) else "Host operation failed")
        return data
    finally:
        connection.close()


def valid_job_id(id):
    return isinstance(id, str) and re.fullmatch(r"[a-zA-Z0-9_-]{8,100}", id) is not None


def validate_job(job, id, expected_request=None):
    if not valid_job_id(id) or not isinstance(job, dict) or job.get("id") != id:
        raise JobObservationError("host_job_response_invalid")
    request = job.get("request")
    if (not isinstance(request, dict) or set(request) - {"id", "action", "snapshot"} or
            request.get("id") != id or not isinstance(request.get("action"), str) or request["action"] not in JOB_ACTIONS or
            not isinstance(job.get("state"), str) or job["state"] not in JOB_STATES or
            (request.get("snapshot") is not None and not isinstance(request["snapshot"], str)) or
            (request["action"] != "restore" and request.get("snapshot") is not None) or
            (expected_request is not None and request != expected_request) or
            (job["state"] == "succeeded" and not isinstance(job.get("result"), dict))):
        raise JobObservationError("host_job_response_invalid")
    return job


def read_job(id, expected_request=None, *, retry=True):
    if not valid_job_id(id):
        raise JobObservationError("host_job_id_invalid")
    for attempt in range(JOB_READ_ATTEMPTS if retry else 1):
        try:
            return validate_job(call("GET", "/jobs/" + id), id, expected_request)
        except BrokerError as exc:
            if exc.status not in {502, 503, 504}:
                raise JobObservationError("host_job_not_found" if exc.status == 404 else "host_job_read_refused") from None
        except (TimeoutError, ConnectionError, FileNotFoundError, http.client.IncompleteRead):
            pass
        except (ValueError, TypeError, RecursionError, http.client.HTTPException):
            raise JobObservationError("host_job_response_invalid") from None
        except OSError:
            raise JobObservationError("host_job_read_refused") from None
        if not retry or attempt + 1 == JOB_READ_ATTEMPTS:
            raise JobObservationError("host_job_observation_unavailable") from None
        time.sleep(JOB_POLL_SECONDS)


def wait_job(id, job):
    request = dict(job["request"])
    while job["state"] == "running":
        time.sleep(JOB_POLL_SECONDS)
        job = read_job(id, request)
    return job


def unconfirmed(id, error):
    print(f"Job {id}: completion is unconfirmed ({error}). No operation was resubmitted. "
          f"Inspect with 'agentic-home job --id {id}' or resume with 'agentic-home wait --id {id}'.", file=sys.stderr)
    raise SystemExit(2)


def main():
    parser = argparse.ArgumentParser(description="Agentic Home outer Linux lifecycle")
    parser.add_argument("command", choices=["status", "provision", "start", "shutdown", "snapshot", "restore",
                                             "bootstrap_hermes", "open", "access-key", "diagnostics", "support-bundle",
                                             "network", "network-select", "network-login", "network-revoke", "updates",
                                             "update-stage", "update-apply", "update-rollback-offer", "update-rollback",
                                             "update-publisher-inspect", "update-publisher-add", "update-publisher-revoke", "job", "wait"])
    parser.add_argument("--id", help="Existing lifecycle job ID for read-only job/wait")
    parser.add_argument("--snapshot")
    parser.add_argument("--output", type=Path, help="New JSON file for support-bundle")
    parser.add_argument("--lan", choices=["dhcp", "reservation"])
    parser.add_argument("--remote", choices=["disabled", "tailscale"])
    parser.add_argument("--manifest", type=Path, help="Signed update envelope JSON (publisher public key is never accepted here)")
    parser.add_argument("--candidate", help="Exact staged candidate digest")
    parser.add_argument("--approval", help="Short-lived approval from the inspected candidate or rollback offer")
    parser.add_argument("--publisher", help="Local-root release publisher identifier")
    parser.add_argument("--repository", help="Exact trusted image repository, without tag/digest")
    parser.add_argument("--public-key", type=Path, help="Ed25519 PUBLIC key PEM; never a private signing key")
    args = parser.parse_args()
    if args.command in {"job", "wait"}:
        if not valid_job_id(args.id):
            parser.error("job/wait requires a valid existing --id")
        try:
            job = read_job(args.id, retry=args.command == "wait")
            if args.command == "wait":
                job = wait_job(args.id, job)
        except JobObservationError as exc:
            unconfirmed(args.id, exc)
        print(json.dumps(job, ensure_ascii=False, indent=2))
        if args.command == "wait" and job["state"] != "succeeded":
            raise SystemExit(1)
        return
    if args.id is not None:
        parser.error("--id is only accepted by read-only job/wait")
    if args.command.startswith("update-publisher-"):
        from .publisher import enroll, inspect
        if not args.publisher or not args.repository or not args.public_key or args.public_key.is_symlink() or args.public_key.stat().st_size > 4096:
            parser.error("publisher commands require --publisher, --repository and --public-key (up to 4 KiB)")
        pem = args.public_key.read_text()
        if args.command == "update-publisher-inspect":
            result = inspect(args.publisher, args.repository, pem)
        else:
            if not args.approval:
                parser.error("publisher trust changes require --approval with the inspected review_digest and local root")
            result = enroll(args.publisher, args.repository, pem, args.approval, revoke=args.command.endswith("revoke"))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command in {"network", "updates"}:
        print(json.dumps(call("GET", "/" + args.command), ensure_ascii=False, indent=2))
        return
    if args.command.startswith("network-"):
        if args.command == "network-select":
            if not args.lan or not args.remote:
                parser.error("network-select requires --lan and --remote")
            path, body = "/network/preferences", {"lan": args.lan, "remote": args.remote}
        else:
            path, body = "/network/" + args.command.removeprefix("network-"), {}
        print(json.dumps(call("POST", path, body), ensure_ascii=False, indent=2))
        return
    if args.command.startswith("update-"):
        action = args.command.removeprefix("update-")
        if action == "stage":
            if not args.manifest or args.manifest.stat().st_size > 131072:
                parser.error("update-stage requires --manifest (up to 128 KiB)")
            body = json.loads(args.manifest.read_text())
        else:
            if not args.candidate or (action != "rollback-offer" and not args.approval):
                parser.error("update requires --candidate and an exact --approval (except rollback-offer)")
            body = {"candidate_digest": args.candidate}
            if action != "rollback-offer":
                body["approval_id"] = args.approval
        print(json.dumps(call("POST", "/updates/" + action, body), ensure_ascii=False, indent=2))
        return
    if args.command == "open":
        from .desktop import open_desktop
        open_desktop()
        return
    if args.command in {"diagnostics", "support-bundle"}:
        from .support import main as support_main
        if args.command == "support-bundle" and args.output is None:
            parser.error("support-bundle requires --output to a new file")
        support_main(["status"] if args.command == "diagnostics" else ["bundle", str(args.output)])
        return
    if args.command == "access-key":
        print(Path("/var/lib/agentic-home/controller/access-key").read_text().strip())
        return
    if args.command == "status":
        print(json.dumps(call("GET", "/status"), ensure_ascii=False, indent=2))
        return
    id = uuid.uuid4().hex
    body = {"id": id, "action": args.command, "snapshot": args.snapshot}
    # A lost POST reply must still leave the Owner an exact read-only recovery
    # handle. Never automatically resend this mutation, even with the same ID.
    print("Operation " + id, flush=True)
    try:
        job = validate_job(call("POST", "/actions", body), id, body)
    except (OSError, ValueError, TypeError, RecursionError, http.client.HTTPException):
        unconfirmed(id, "host_job_submit_unconfirmed")
    try:
        job = wait_job(id, job)
    except JobObservationError as exc:
        unconfirmed(id, exc)
    print(json.dumps(job, ensure_ascii=False, indent=2))
    if job["state"] != "succeeded":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
