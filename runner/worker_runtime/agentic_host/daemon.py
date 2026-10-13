import copy
import json
import os
import re
import signal
import socket
import socketserver
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from .manager import HostError, Manager, atomic
from .network import Network
from .preview import Previews, preview_server
from .updater import Updater


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

    def get(self, id):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{8,100}", id):
            raise HostError("Invalid operation id")
        # Read the persisted-job cache only. Never hold this lock while calling
        # libvirt, Docker, HA or any other external status observer.
        with self.lock:
            return copy.deepcopy(self.items.get(id))

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


def install_broker_stops(server, coding):
    """SIGTERM and SIGINT unblock serve_forever. close() collects on the caller thread.

    shutdown() must run off the main thread: serve_forever waits for it there.
    """
    def handler(signum, frame):
        coding.closing = True
        coding.closed.set()
        # shutdown() waits until serve_forever returns, so a second signal must not call it.
        if getattr(handler, "started", False):
            return
        handler.started = True
        threading.Thread(target=server.shutdown, name="broker-shutdown", daemon=True).start()

    previous = {name: signal.signal(name, handler) for name in (signal.SIGTERM, signal.SIGINT)}
    return previous


def serve(path="/run/agentic-home/host.sock"):
    os.umask(0o077)
    manager = Manager()
    jobs = Jobs(manager)
    from .coding_service import CodingService
    coding = CodingService(manager)
    network = Network(manager)
    previews = Previews(manager, bundles=coding.root / "web-previews")
    updater = Updater(manager)
    try:
        viewer = preview_server(previews)
        threading.Thread(target=viewer.serve_forever, daemon=True).start()
    except OSError:
        viewer = None
        previews.transport_ready = False
        print("Preview listener unavailable; local rescue broker remains active", file=sys.stderr, flush=True)

    def service(method, path, body=None):
        for prefix, handler in (("/coding", coding), ("/network", network), ("/previews", previews), ("/updates", updater)):
            if path == prefix or path.startswith(prefix + "/"):
                return handler.handle(method, path, body)
        return None

    def forwarding():
        while True:
            try:
                manager.refresh_forwarding()
            except Exception as exc:
                print("Product network reconciliation: " + str(exc)[:300], file=sys.stderr, flush=True)
            try:
                previews.expire()
            except Exception:
                print("Preview retention reconciliation unavailable", file=sys.stderr, flush=True)
            try:
                updater.expire_snapshots()
            except Exception:
                print("Updater snapshot retention reconciliation unavailable", file=sys.stderr, flush=True)
            time.sleep(15)

    threading.Thread(target=forwarding, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def authorized(self):
            _, uid, _ = struct.unpack("3i", self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            # Root Runner adapter only receives the coding namespace. Owner
            # Controller/CLI UID1000 receives the existing management surface.
            return uid == (0 if self.path == "/coding" or self.path.startswith("/coding/") else 1000)

        def respond(self, code, body):
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            try:
                if not self.authorized():
                    return self.respond(403, {"detail": "broker_role_forbidden"})
                response = service("GET", self.path)
                if response is not None:
                    return self.respond(*response)
                if self.path.startswith("/jobs/"):
                    job = jobs.get(self.path.removeprefix("/jobs/"))
                    return self.respond(200, job) if job is not None else self.respond(404, {"detail": "host_job_not_found"})
                if self.path != "/status":
                    return self.respond(404, {"detail": "Unknown route"})
                return self.respond(200, {**manager.status(), "jobs": list(jobs.items.values())[-50:]})
            except HostError as exc:
                return self.respond(409, {"detail": str(exc)[:100]})
            except Exception:
                return self.respond(503, {"detail": "host_service_unavailable"})

        def do_POST(self):
            try:
                if not self.authorized():
                    return self.respond(403, {"detail": "broker_role_forbidden"})
                size = int(self.headers.get("Content-Length", "0"))
                maximum = 6 * 1024 * 1024 if self.path == "/previews" else 131072
                if not 0 < size <= maximum:
                    return self.respond(400, {"detail": "Invalid request"})
                body = json.loads(self.rfile.read(size))
                response = service("POST", self.path, body)
                if response is not None:
                    return self.respond(*response)
                if self.path != "/actions" or size >= 4096:
                    return self.respond(400, {"detail": "Invalid request"})
                return self.respond(202, jobs.start(body))
            except (ValueError, HostError, TypeError) as exc:
                return self.respond(409, {"detail": str(exc)[:500]})
            except Exception:
                return self.respond(503, {"detail": "host_service_unavailable"})

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # Match systemd RuntimeDirectoryMode when launched by a rescue shell/test.
    # Socket permissions still restrict all callers to root and Owner GID1000.
    os.chmod(Path(path).parent, 0o755)
    Path(path).unlink(missing_ok=True)
    with Server(path, Handler) as server:
        os.chown(path, 0, 1000)
        os.chmod(path, 0o660)
        previous_stops = install_broker_stops(server, coding)
        try:
            server.serve_forever()
        finally:
            for name in (signal.SIGTERM, signal.SIGINT):
                signal.signal(name, previous_stops.get(name, signal.SIG_DFL))
            if viewer:
                viewer.shutdown()
                viewer.server_close()
            coding.close()


if __name__ == "__main__":
    serve()
