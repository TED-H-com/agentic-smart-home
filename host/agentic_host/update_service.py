"""Fixed owner transport from the immutable updater, independent of Host rollback."""
import json
import os
import re
import signal
import socket
import socketserver
import stat
import struct
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from .host_update import HostBundle, secure
from .manager import HostError, Manager

SOCKET = Path("/run/agentic-home/host-updates.sock")
MAX_BODY = 131072
READ_TIMEOUT = 10
MAX_CONNECTIONS = 8


def peer_uid(connection):
    return struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]


def allowed(method, path):
    return ((method == "GET" and (path == "/host-updates" or re.fullmatch(r"/host-updates/[a-f0-9]{64}", path)))
            or (method == "POST" and path in {"/host-updates/check", "/host-updates/stage", "/host-updates/apply",
                                             "/host-updates/rollback-offer", "/host-updates/rollback", "/host-updates/cancel-offer"}))


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = MAX_CONNECTIONS

    def __init__(self, path, bundle):
        self.bundle = bundle
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(str(path), Handler)

    def process_request(self, request, address):
        # Acquire before creating a thread; stalled peers cannot create an
        # unbounded number of root threads. Busy requests have no side effects.
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, address):
        pass  # No request bodies, signatures or owner approvals enter logs.


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(READ_TIMEOUT)
        super().setup()

    def log_message(self, *args):
        pass

    def respond(self, status, body):
        data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def dispatch(self, method):
        try:
            if peer_uid(self.connection) != 1000:
                return self.respond(403, {"detail": "broker_role_forbidden"})
            if not allowed(method, self.path):
                return self.respond(404, {"detail": "host_update_route_not_found"})
            body = None
            if method == "POST":
                lengths = self.headers.get_all("Content-Length", [])
                if (len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,6}", lengths[0])
                        or self.headers.get("Transfer-Encoding") is not None):
                    return self.respond(400, {"detail": "host_update_request_invalid"})
                size = int(lengths[0])
                if not 0 < size <= MAX_BODY:
                    return self.respond(413, {"detail": "host_update_request_too_large"})
                data = self.rfile.read(size)
                if len(data) != size:
                    return self.respond(400, {"detail": "host_update_request_invalid"})
                body = json.loads(data)
                if not isinstance(body, dict):
                    return self.respond(400, {"detail": "host_update_request_invalid"})
            self.respond(*self.server.bundle.handle(method, self.path, body))
        except (ValueError, TypeError, UnicodeError):
            self.respond(400, {"detail": "host_update_request_invalid"})
        except HostError as exc:
            code = str(exc)
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,95}", code):
                code = "host_update_operation_failed"
            self.respond(409, {"detail": code})
        except (TimeoutError, OSError):
            pass  # Closed/stalled connection: never replay a mutation.
        except Exception:
            self.respond(503, {"detail": "host_update_service_unavailable"})

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")


def create_server(path, bundle):
    """Inject a disk socket and engine in isolated tests; production paths fixed."""
    path = Path(path)
    secure(path.parent)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0:
            raise HostError("host_update_socket_untrusted")
        # Never unlink another running instance's listener.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            try:
                probe.connect(str(path))
            except (ConnectionRefusedError, FileNotFoundError):
                path.unlink(missing_ok=True)
            else:
                raise HostError("host_update_service_already_running")
    server = Server(path, bundle)
    os.chown(path, 0, 1000)
    os.chmod(path, 0o660)
    return server


def main():
    if os.geteuid() != 0:
        raise SystemExit("host_update_local_root_required")
    os.umask(0o077)
    SOCKET.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    bundle = HostBundle(Manager())
    with create_server(SOCKET, bundle) as server:
        try:
            bundle.recover_preparations()
        except HostError:
            pass  # Approved unresolved attempts stay fenced; GET/rescue remains.
        def stop(*args):
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        server.serve_forever()


if __name__ == "__main__":
    main()
