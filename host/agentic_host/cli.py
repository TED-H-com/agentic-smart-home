import argparse
import http.client
import json
import socket
import time
import uuid
from pathlib import Path


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
        data = json.load(response)
        if response.status >= 300:
            raise ValueError(data.get("detail", "Host operation failed"))
        return data
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description="Agentic Home outer Linux lifecycle")
    parser.add_argument("command", choices=["status", "provision", "start", "shutdown", "snapshot", "restore",
                                             "bootstrap_hermes", "open", "access-key", "diagnostics", "support-bundle",
                                             "network", "network-select", "network-login", "network-revoke", "updates",
                                             "update-stage", "update-apply", "update-rollback-offer", "update-rollback",
                                             "update-publisher-inspect", "update-publisher-add", "update-publisher-revoke"])
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
    job = call("POST", "/actions", {"id": uuid.uuid4().hex, "action": args.command, "snapshot": args.snapshot})
    print("Operation " + job["id"], flush=True)
    while job["state"] == "running":
        time.sleep(2)
        job = next(j for j in call("GET", "/status")["jobs"] if j["id"] == job["id"])
    print(json.dumps(job, ensure_ascii=False, indent=2))
    if job["state"] != "succeeded":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
