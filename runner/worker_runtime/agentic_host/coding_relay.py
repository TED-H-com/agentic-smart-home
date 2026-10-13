"""Fixed provider/source egress across a Unix socket, never a general network proxy."""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import socket
import socketserver
import ssl
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .coding_sandbox import SandboxError

SOURCE_HOSTS = frozenset({"github.com", "raw.githubusercontent.com", "api.github.com",
    "docs.python.org", "developers.home-assistant.io", "www.home-assistant.io",
    "hermes-agent.nousresearch.com", "developers.openai.com", "platform.openai.com"})
MAX_REQUEST = 2 * 1024**2
MAX_RESPONSE = 8 * 1024**2


def credentials(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid not in {0, 1010}
            or info.st_mode & 0o077 or info.st_size > 32768):
            raise SandboxError("Unsafe coding credential file")
        value = json.loads(stream.read(32769))
    if isinstance(value.get("OPENAI_API_KEY"), str) and value["OPENAI_API_KEY"]:
        return "api.openai.com", "/v1/responses", {"Authorization": "Bearer " + value["OPENAI_API_KEY"]}
    tokens = value.get("tokens") or {}
    if isinstance(tokens.get("access_token"), str) and tokens["access_token"]:
        headers = {"Authorization": "Bearer " + tokens["access_token"], "OpenAI-Beta": "responses=experimental"}
        if isinstance(tokens.get("account_id"), str):
            headers["ChatGPT-Account-Id"] = tokens["account_id"]
        return "chatgpt.com", "/backend-api/codex/responses", headers
    raise SandboxError("Coding login required")


class PublicTLS(http.client.HTTPSConnection):
    def connect(self):
        # Resolve once, reject every non-public result, then connect to the checked IP.
        results = socket.getaddrinfo(self.host, 443, type=socket.SOCK_STREAM)
        if not results or any(not ipaddress.ip_address(item[4][0]).is_global for item in results):
            raise SandboxError("Private network destination rejected")
        address = results[0][4][0]
        self.sock = ssl.create_default_context().wrap_socket(
            socket.create_connection((address, 443), timeout=self.timeout), server_hostname=self.host)


def upstream(host, path, method, body, headers, *, maximum=MAX_RESPONSE):
    connection = PublicTLS(host, timeout=45)
    try:
        connection.request(method, path, body=body, headers={"User-Agent": "agentic-home-coding/1", **headers})
        response = connection.getresponse()
        data = response.read(maximum + 1)
        if len(data) > maximum:
            raise SandboxError("Relay response limit")
        return response.status, response.getheader("Content-Type", "application/octet-stream"), data
    finally:
        connection.close()


class Relay:
    def __init__(self, path: Path, credential_path: Path, deadline: float, *, transport=upstream,
                 model="gpt-6-astra"):
        self.path, self.credential_path, self.deadline = path, credential_path, deadline
        self.transport, self.calls, self.lock = transport, 0, threading.Lock()
        self.server = self.thread = None
        self.model = model
        self.connections = threading.BoundedSemaphore(2)
        self.auth_failed = False

    def request(self, method, path, body):
        with self.lock:
            if time.time() >= self.deadline or self.calls >= 80:
                raise SandboxError("Relay authorization expired or exhausted")
            self.calls += 1
        if method == "POST" and path == "/v1/responses":
            if self.auth_failed:
                raise SandboxError("Coding provider requires reauthentication")
            if len(body) > MAX_REQUEST:
                raise SandboxError("Relay request limit")
            value = json.loads(body)
            if not isinstance(value, dict) or value.get("model") != self.model:
                raise SandboxError("Unsupported model")
            host, target, headers = credentials(self.credential_path)
            # Remote MCP and hosted computer/network tools are not this worker's authority.
            allowed_tools = {"function", "custom", "local_shell"}
            if any(tool.get("type") not in allowed_tools for tool in value.get("tools", [])):
                raise SandboxError("Remote tool not allowed")
            value["store"] = False
            response = self.transport(host, target, "POST", json.dumps(value).encode(),
                                      {**headers, "Content-Type": "application/json", "Accept": "text/event-stream"})
            if response[0] in {401, 403}:
                self.auth_failed = True
            return response
        query = urlsplit(path)
        if method == "GET" and query.path == "/fetch":
            params = parse_qs(query.query, strict_parsing=True)
            if set(params) != {"url"} or len(params["url"]) != 1:
                raise SandboxError("Invalid source request")
            url = urlsplit(params["url"][0])
            if (url.scheme != "https" or url.hostname not in SOURCE_HOSTS or url.port not in {None, 443}
                or url.username or url.password or url.fragment or len(url.geturl()) > 2048):
                raise SandboxError("Source not allowed")
            return self.transport(url.hostname, url.path + ("?" + url.query if url.query else ""), "GET", None,
                                  {"Accept": "text/plain,application/json,text/html"}, maximum=512 * 1024)
        raise SandboxError("Relay route not allowed")

    def start(self):
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                self.request.settimeout(50)
                super().setup()

            def log_message(self, *args):
                pass

            def handle_request(self):
                self.connection.settimeout(50)
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 <= length <= MAX_REQUEST or self.headers.get("Transfer-Encoding"):
                        raise ValueError("Invalid framing")
                    result = relay.request(self.command, self.path, self.rfile.read(length))
                    status, content_type, data = result
                except (OSError, ValueError, SandboxError, http.client.HTTPException):
                    status, content_type, data = 502, "application/json", b'{"error":"coding_egress_rejected"}'
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = handle_request

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
            block_on_close = True

            def process_request(self, request, client_address):
                if not relay.connections.acquire(blocking=False):
                    self.shutdown_request(request)
                    return
                try:
                    super().process_request(request, client_address)
                except BaseException:
                    relay.connections.release()
                    raise

            def process_request_thread(self, request, client_address):
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    relay.connections.release()

        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.server = Server(str(self.path), Handler)
        self.path.chmod(0o600)
        os.chown(self.path, 10000, 10000)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.deadline = 0
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread:
            self.thread.join(timeout=3)
        self.path.unlink(missing_ok=True)
