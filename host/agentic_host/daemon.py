import json
import os
import re
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from .manager import HostError, Manager, atomic


class Jobs:
    def __init__(self, manager):
        self.manager = manager
        self.path = manager.root / "jobs.json"
        self.items = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.lock = threading.Lock()
        for job in self.items.values():
            if job["state"] == "running":
                job.update(state="needs_reconcile", message="主機重啟；檢查實際狀態後重新操作")
                if job.get("request", {}).get("action") == "bootstrap_hermes":
                    path = manager.root / "bootstrap-hermes.json"
                    if path.exists():
                        record = json.loads(path.read_text())
                        if record.get("state") == "installing":
                            atomic(path, {**record, "state": "needs_reconcile", "error_code": "hermes_install_interrupted"})
        atomic(self.path, self.items)

    def start(self, body):
        if set(body) - {"id", "action", "snapshot"}:
            raise HostError("Unknown job fields")
        id, action = body.get("id", ""), body.get("action")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{8,100}", id):
            raise HostError("Invalid operation id")
        if action not in {"provision", "start", "shutdown", "snapshot", "restore", "bootstrap_hermes"}:
            raise HostError("Unknown lifecycle action")
        if action != "restore" and body.get("snapshot"):
            raise HostError("Unexpected snapshot")
        with self.lock:
            if id in self.items:
                if self.items[id]["request"] != body:
                    raise HostError("Idempotency key reused with different operation")
                return self.items[id]
            if any(j["state"] == "running" for j in self.items.values()):
                raise HostError("Another host operation is running")
            job = {"id": id, "request": body, "state": "running", "at": time.time()}
            self.items[id] = job
            atomic(self.path, self.items)
        threading.Thread(target=self.execute, args=(job,), daemon=True).start()
        return job

    def execute(self, job):
        try:
            result = self.manager.action(job["request"]["action"], job["request"].get("snapshot"))
            update = {"state": "succeeded", "result": result}
        except Exception as exc:
            update = {"state": "failed", "message": str(exc)[:500]}
        with self.lock:
            job.update(update, finished_at=time.time())
            atomic(self.path, self.items)


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve(path="/run/agentic-home/host.sock"):
    os.umask(0o077)
    manager = Manager()
    jobs = Jobs(manager)

    def forwarding():
        while True:
            try:
                manager.refresh_forwarding()
            except Exception as exc:
                print("Product network reconciliation: " + str(exc)[:300], file=sys.stderr, flush=True)
            time.sleep(15)

    threading.Thread(target=forwarding, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, code, body):
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            try:
                if self.path != "/status":
                    return self.respond(404, {"detail": "Unknown route"})
                return self.respond(200, {**manager.status(), "jobs": list(jobs.items.values())[-50:]})
            except Exception as exc:
                return self.respond(503, {"detail": str(exc)[:500]})

        def do_POST(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if self.path != "/actions" or not 0 < size < 4096:
                    return self.respond(400, {"detail": "Invalid request"})
                body = json.loads(self.rfile.read(size))
                return self.respond(202, jobs.start(body))
            except (ValueError, HostError, TypeError) as exc:
                return self.respond(409, {"detail": str(exc)[:500]})

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).unlink(missing_ok=True)
    with Server(path, Handler) as server:
        os.chown(path, 0, 1000)
        os.chmod(path, 0o660)
        server.serve_forever()


if __name__ == "__main__":
    serve()
