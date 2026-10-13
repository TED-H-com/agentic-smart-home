"""Offline behavioral gate for the product's private Hermes adapter contract v1.

Run as UID 10003, network=none, read-only root, with disk-backed HOME/TMPDIR.
This executes the real agent/MCP/OAuth implementation, using only synthetic
model responses, a loopback MCP server and an httpx mock OAuth transport. It
proves consumed integration behavior, never a cloud account or household action.
Failures print bounded, non-secret fields and exit nonzero before activation.
"""

import base64
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from agentic_hermes.models import TOOLS
from agentic_hermes.upstream import CONTRACT_VERSION, HermesAdapter, require

API_KEY = "compatibility-api-fixture"
MCP_TOKEN = "compatibility-mcp-fixture"
REFRESH_TOKEN = "compatibility-refresh-fixture"
JWT_HEADER = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
ACCESS_TOKEN = JWT_HEADER + "." + base64.urlsafe_b64encode(json.dumps({
    "exp": 4102444800, "https://api.openai.com/auth": {"chatgpt_account_id": "compatibility-fixture"},
}).encode()).rstrip(b"=").decode() + ".AA"
EXPECTED_TOOLS = {"mcp__agentic__" + name.replace(".", "_") for name in TOOLS}


def oauth_fixture():
    """Intercept httpx's public constructor, not a second private Hermes API."""
    import httpx
    from agentic_hermes.oauth_child import main as oauth_main

    observed = []
    original = httpx.Client.__init__

    def endpoint(request):
        require(request.url.host == "auth.openai.com" and request.url.scheme == "https")
        observed.append(request.url.path)
        if request.url.path == "/api/accounts/deviceauth/usercode":
            require(bool(json.loads(request.content)["client_id"]))
            value = {"device_auth_id": "fixture-device", "user_code": "ABCD-EFGH", "interval": 3}
        elif request.url.path == "/api/accounts/deviceauth/token":
            require(json.loads(request.content) == {"device_auth_id": "fixture-device", "user_code": "ABCD-EFGH"})
            value = {"authorization_code": "fixture-code", "code_verifier": "fixture-verifier"}
        elif request.url.path == "/oauth/token":
            from urllib.parse import parse_qs
            body = parse_qs(request.content.decode())
            require(body["code"] == ["fixture-code"] and body["code_verifier"] == ["fixture-verifier"])
            require(body["redirect_uri"] == ["https://auth.openai.com/deviceauth/callback"])
            value = {"access_token": ACCESS_TOKEN, "refresh_token": REFRESH_TOKEN}
        else:
            raise AssertionError("unexpected_oauth_endpoint")
        return httpx.Response(200, json=value)

    def client_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(endpoint)
        original(self, *args, **kwargs)

    public = io.StringIO()
    with patch.object(httpx.Client, "__init__", client_init), contextlib.redirect_stdout(public):
        oauth_main()
    events = [json.loads(line) for line in public.getvalue().splitlines()]
    require(events == [{"state": "pending", "verification_uri": "https://auth.openai.com/codex/device",
                        "user_code": "ABCD-EFGH"}, {"state": "configured"}])
    home = Path(os.environ["HERMES_HOME"])
    require((home / "auth.json").stat().st_mode & 0o777 == 0o600)
    require(json.loads((home / "config.yaml").read_text())["auth"]["adopt_external_logins"] is False)
    adapter = HermesAdapter()
    require(adapter.read_tokens() == {"access_token": ACCESS_TOKEN, "refresh_token": REFRESH_TOKEN})
    credential, base_url = adapter.credentials()
    require(credential == ACCESS_TOKEN and base_url.startswith("https://chatgpt.com/"))
    require(not (Path(os.environ["HOME"]) / ".codex").exists())
    require(observed == ["/api/accounts/deviceauth/usercode", "/api/accounts/deviceauth/token", "/oauth/token"])
    require(all(secret not in public.getvalue() for secret in
                (ACCESS_TOKEN, REFRESH_TOKEN, "fixture-device", "fixture-code", "fixture-verifier")))
    return {"oauth": "real_upstream_helpers", "transport": "mock_http", "account_verified": False}


class FixtureServer(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.observed = {"chat": 0, "continuation": 0, "native": 0, "unauthorized": 0, "tools": 0}
        self.failures, self.egress = [], []

    def handle_error(self, request, client_address):
        self.failures.append("fixture_protocol_mismatch")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, value, status=200):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_CONNECT(self):
        # Turns use this server as their proxy: any HTTPS call beyond loopback is egress.
        self.server.egress.append(self.path[:120])
        self.send_response(403)
        self.end_headers()
        self.close_connection = True

    def do_GET(self):
        if self.path.startswith("http"):
            self.server.egress.append(self.path.split("/")[2][:120])
        self.reply({}, 405)

    def do_DELETE(self):
        self.reply({})

    def do_POST(self):
        if self.path.startswith("http"):
            self.server.egress.append(self.path.split("/")[2][:120])
            self.reply({}, 403)
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        if self.path == "/mcp":
            require(self.headers.get("Authorization") == "Bearer " + MCP_TOKEN)
            method = body.get("method")
            if method == "notifications/initialized":
                self.send_response(202)
                self.end_headers()
                return
            if method == "initialize":
                result = {"protocolVersion": body["params"]["protocolVersion"], "capabilities": {"tools": {}},
                          "serverInfo": {"name": "offline-fixture", "version": "1"}}
            elif method == "tools/list":
                # Undeclared MCP tools must be filtered even if the server offers them.
                result = {"tools": [{"name": name, "description": "Synthetic integration fixture",
                          "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}
                                    for name in (*TOOLS, "terminal")]}
            elif method == "tools/call":
                require(body["params"]["name"] == "host.status")
                self.server.observed["tools"] += 1
                result = {"content": [{"type": "text", "text": "offline fixture evidence"}]}
            else:
                raise AssertionError("unexpected_mcp_method")
            self.reply({"jsonrpc": "2.0", "id": body["id"], "result": result})
            return
        if self.path == "/api/show":
            self.reply({"model_info": {"general.architecture": "fixture", "fixture.context_length": 65536}})
            return
        native = self.path == "/v1/responses"
        require(MCP_TOKEN not in json.dumps(body) and REFRESH_TOKEN not in json.dumps(body))
        if self.path == "/v1/chat/completions" and self.headers.get("Authorization") == "Bearer " + API_KEY + "-invalid":
            self.server.observed["unauthorized"] += 1
            self.reply({"error": {"message": "Invalid API key", "type": "authentication_error"}}, 401)
            return
        require(self.headers.get("Authorization") == "Bearer " + (ACCESS_TOKEN if native else API_KEY))
        if native:
            require({tool["name"] for tool in body["tools"]} == EXPECTED_TOOLS)
            self.server.observed["native"] += 1
            if self.server.observed["native"] == 1:
                item = {"id": "fc_fixture", "type": "function_call", "status": "completed", "call_id": "call_fixture",
                        "name": "mcp__agentic__host_status", "arguments": "{}"}
            else:
                item = {"id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": "native fixture complete", "annotations": []}]}
            response = {"id": "resp_fixture", "object": "response", "created_at": 1, "model": "gpt-6-astra",
                        "status": "completed", "output": [item], "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
            events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                      {"type": "response.output_item.added", "output_index": 0, "item": item},
                      {"type": "response.output_item.done", "output_index": 0, "item": item},
                      {"type": "response.completed", "response": response}]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write("".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events).encode())
            return
        require(self.path == "/v1/chat/completions")
        require({tool["function"]["name"] for tool in body["tools"]} == EXPECTED_TOOLS)
        messages = body["messages"]
        continuation = any(item.get("content") == "fixture-continuation" for item in messages)
        if continuation:
            require(any(item.get("content") == "fixture-original-message" for item in messages))
            require(any(item.get("role") == "tool" for item in messages))
            self.server.observed["continuation"] += 1
        else:
            self.server.observed["chat"] += 1
        call = self.server.observed["chat"]
        message = {"role": "assistant", "content": "continuation fixture complete" if continuation else "chat fixture complete"}
        finish = "stop"
        if not continuation and call <= 2:
            # The model tries a builtin first. No terminal tool may execute.
            name = "terminal" if call == 1 else "mcp__agentic__host_status"
            message.update(content=None, tool_calls=[{"id": f"compat-call-{call}", "type": "function",
                "function": {"name": name, "arguments": "{}"}}])
            finish = "tool_calls"
        answer = {"id": "compat", "object": "chat.completion", "created": 1, "model": "fixture-model",
                  "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                  "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        if body.get("stream"):
            delta = dict(message)
            if "tool_calls" in delta:
                delta["tool_calls"] = [{"index": 0, **delta["tool_calls"][0]}]
            chunk = {**answer, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: " + json.dumps({**chunk, "choices": [
                {"index": 0, "delta": {}, "finish_reason": finish}]}) + "\n\ndata: [DONE]\n\n").encode())
        else:
            self.reply(answer)


def run_child(arguments, env, payload=None):
    result = subprocess.run([sys.executable, *arguments], input=json.dumps(payload) if payload else None,
                            capture_output=True, text=True, cwd=env["HOME"], env=env, timeout=30)
    require(result.returncode == 0)
    require(all(secret not in result.stdout for secret in (API_KEY, MCP_TOKEN, ACCESS_TOKEN, REFRESH_TOKEN)))
    return json.loads(result.stdout)


def behavioral_probe():
    # Imported upstream may print banners. Only the final bounded receipt is stdout.
    with contextlib.redirect_stdout(sys.stderr):
        HermesAdapter().validate_contract()
    server = FixtureServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="hermes-compat-", dir=os.environ["TMPDIR"]) as directory:
            scratch = Path(directory)
            for name in ("chat", "native"):
                (scratch / name).mkdir(mode=0o700)
            url = f"http://127.0.0.1:{server.server_port}"
            env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": directory,
                   "HERMES_HOME": str(scratch / "chat"), "TMPDIR": directory,
                   "PYTHONPATH": "/app:/opt/hermes", "PYTHONDONTWRITEBYTECODE": "1",
                   "AGENTIC_PARENT_PID": str(os.getpid()), "AGENTIC_MCP_URL": url + "/mcp",
                   "AGENTIC_PROVIDER_URL": url + "/v1"}
            # A turn may reach only the provider and the Controller, both loopback here.
            turn_env = {**env, "HTTPS_PROXY": url, "HTTP_PROXY": url, "NO_PROXY": "127.0.0.1,localhost"}
            payload = {"provider": "openrouter", "model": "fixture-model", "api_key": API_KEY,
                       "mcp_token": MCP_TOKEN, "message": "fixture-original-message",
                       "session_id": "compatibility-session", "run_id": "compatibility-run", "history": []}
            response = run_child(["-m", "agentic_hermes.child"], turn_env, payload)
            require(response.get("error") is None and response["answer"] == "chat fixture complete")
            require(set(response["tools"]) == EXPECTED_TOOLS)
            denied = [item for item in response["history"] if item.get("role") == "tool"
                      and item.get("tool_call_id") == "compat-call-1"]
            require(bool(denied) and any(marker in str(denied).lower()
                                        for marker in ("error", "does not exist", "not authorized")))
            payload.update(message="fixture-continuation", history=response["history"])
            continued = run_child(["-m", "agentic_hermes.child"], turn_env, payload)
            require(continued.get("error") is None and continued["answer"] == "continuation fixture complete")
            rejected = run_child(["-m", "agentic_hermes.child"], turn_env,
                                 {**payload, "history": [], "api_key": API_KEY + "-invalid"})
            require(rejected.get("error") == "needs_reauth" and rejected["answer"] == "" and rejected["history"] == [])
            env.update(HERMES_HOME=str(scratch / "native"), AGENTIC_PROVIDER_URL=url + "/v1")
            oauth = run_child([str(Path(__file__).resolve()), "--oauth-fixture"], env)
            require(oauth["oauth"] == "real_upstream_helpers")
            native = run_child(["-m", "agentic_hermes.child"], {**turn_env, "HERMES_HOME": env["HERMES_HOME"]},
                              {**payload, "provider": "openai-codex", "model": "gpt-6-astra", "history": []})
            require(native.get("error") is None and native["answer"] == "native fixture complete")
            require(set(native["tools"]) == EXPECTED_TOOLS)
            require(not server.failures and not server.egress)
            require(server.observed == {"chat": 3, "continuation": 1, "native": 2, "unauthorized": 1, "tools": 2})
        return {"compatible": True, "adapter_contract": CONTRACT_VERSION, "observed_from": "runtime_container",
                "model": "protocol_fixture", "mcp": "official_client_loopback_fixture", "cloud_account_verified": False,
                "checks": ["consumed_signatures", "chat_tool_roundtrip", "continued_history", "tool_allowlist",
                           "builtin_tool_refused", "provider_401", "native_device_oauth", "private_token_store",
                           "native_responses_tool_roundtrip", "secret_redaction", "no_egress_beyond_provider_and_mcp"]}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def main():
    try:
        result = oauth_fixture() if sys.argv[1:] == ["--oauth-fixture"] else behavioral_probe()
    except Exception:
        # Do not expose provider responses, raw stderr, filesystem paths or tokens.
        print(json.dumps({"compatible": False, "adapter_contract": CONTRACT_VERSION,
                          "error": "upstream_contract_incompatible", "cloud_account_verified": False}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
