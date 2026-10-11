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
        connection.request(method, path, body=json.dumps(body) if body else None,
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
    parser.add_argument("command", choices=["status", "provision", "start", "shutdown", "snapshot", "restore", "access-key"])
    parser.add_argument("--snapshot")
    args = parser.parse_args()
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
