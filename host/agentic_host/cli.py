import argparse
import http.client
import json
import re
import socket
import sys
import time
import uuid
from pathlib import Path

JOB_ACTIONS = {"provision", "start", "shutdown", "snapshot", "restore", "bootstrap_hermes", "configure_lan"}
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
    if (not isinstance(request, dict) or set(request) - {"id", "action", "snapshot", "interface_id"} or
            request.get("id") != id or not isinstance(request.get("action"), str) or request["action"] not in JOB_ACTIONS or
            not isinstance(job.get("state"), str) or job["state"] not in JOB_STATES or
            (request.get("snapshot") is not None and not isinstance(request["snapshot"], str)) or
            (request["action"] != "restore" and request.get("snapshot") is not None) or
            (expected_request is not None and request != expected_request) or
            (job["state"] == "succeeded" and not isinstance(job.get("result"), dict))):
        raise JobObservationError("host_job_response_invalid")
    if request["action"] == "configure_lan":
        value = request.get("interface_id")
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)):
            raise JobObservationError("host_job_response_invalid")
    elif "interface_id" in request:
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


def argument_parser():
    parser = argparse.ArgumentParser(description="Agentic Home outer Linux lifecycle")
    # Shared defaults keep dispatch simple; options belong only to the command
    # that actually accepts them, so unrelated flags fail before any request.
    parser.set_defaults(id=None, snapshot=None, interface_id=None, disable_lan=False,
                        output=None, lan=None, remote=None, manifest=None, candidate=None,
                        approval=None, publisher=None, repository=None, public_key=None)
    commands = parser.add_subparsers(dest="command", required=True)
    descriptions = {
        "status": "Read product, home network and restore point status",
        "provision": "Prepare the product-owned Home Assistant VM",
        "start": "Start Home Assistant", "shutdown": "Cleanly shut down Home Assistant",
        "snapshot": "Create a Home Assistant restore point",
        "restore": "Restore an existing Home Assistant restore point",
        "bootstrap_hermes": "Prepare the shipped assistant runtime",
        "configure_lan": "Connect to home network automatically, with a restore point and NAT fallback",
        "ha-lan": "Read Home Assistant home network address and diagnostics",
        "open": "Open the product on this desktop", "access-key": "Read the local rescue access key",
        "diagnostics": "Read safe product diagnostics", "support-bundle": "Save a new diagnostic bundle",
        "network": "Read host network preferences", "network-select": "Choose the host address and remote access policy",
        "network-login": "Start the selected remote network login", "network-revoke": "Revoke remote network access",
        "updates": "Read trusted update candidates",
        "update-stage": "Verify and stage a signed update envelope",
        "update-apply": "Apply an exact approved staged Controller update",
        "update-rollback-offer": "Inspect an exact Controller rollback offer",
        "update-rollback": "Apply an exact approved Controller rollback",
        "update-publisher-inspect": "Inspect a publisher public key without granting trust",
        "update-publisher-add": "Approve a publisher trust pin as local root",
        "update-publisher-revoke": "Revoke an exact publisher trust pin as local root",
        "job": "Read an existing lifecycle job without resubmitting",
        "wait": "Wait for an existing lifecycle job without resubmitting",
    }
    sub = {name: commands.add_parser(name, help=description, description=description)
           for name, description in descriptions.items()}
    for name in ("job", "wait"):
        sub[name].add_argument("--id", required=True, help="Existing lifecycle job ID")
    sub["restore"].add_argument("--snapshot", required=True, help="Existing restore point ID")
    selection = sub["configure_lan"].add_mutually_exclusive_group()
    selection.add_argument("--interface-id", help="Current opaque Ethernet ID from ha-lan; omit for the only wired NIC")
    selection.add_argument("--disable-lan", action="store_true", help="Return Home Assistant to its basic NAT network")
    sub["support-bundle"].add_argument("--output", required=True, type=Path, help="New JSON file")
    sub["network-select"].add_argument("--lan", required=True, choices=["dhcp", "reservation"], help="Host address policy")
    sub["network-select"].add_argument("--remote", required=True, choices=["disabled", "tailscale"])
    sub["update-stage"].add_argument("--manifest", required=True, type=Path, help="Signed update envelope JSON")
    for name in ("update-apply", "update-rollback-offer", "update-rollback"):
        sub[name].add_argument("--candidate", required=True, help="Exact staged candidate digest")
        if name != "update-rollback-offer":
            sub[name].add_argument("--approval", required=True, help="Short-lived approval from the inspected offer")
    for name in ("update-publisher-inspect", "update-publisher-add", "update-publisher-revoke"):
        sub[name].add_argument("--publisher", required=True, help="Release publisher identifier")
        sub[name].add_argument("--repository", required=True, help="Exact trusted image repository, without tag/digest")
        sub[name].add_argument("--public-key", required=True, type=Path, help="Ed25519 PUBLIC key PEM")
        if name != "update-publisher-inspect":
            sub[name].add_argument("--approval", required=True, help="Exact inspected review_digest; requires local root")
    return parser


def main():
    parser = argument_parser()
    args = parser.parse_args()
    if args.interface_id is not None and not re.fullmatch(r"[a-f0-9]{64}", args.interface_id):
        parser.error("--interface-id must be a current opaque Ethernet ID from ha-lan")
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
    if args.command == "ha-lan":
        print(json.dumps(call("GET", "/network/ha-lan"), ensure_ascii=False, indent=2))
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
    if args.command == "configure_lan" and (args.disable_lan or args.interface_id is not None):
        body["interface_id"] = None if args.disable_lan else args.interface_id
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
