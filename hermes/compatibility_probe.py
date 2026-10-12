"""Offline compatibility gate for a candidate upstream tree, with synthetic data.

Run as UID 10003 with --network=none, read-only root and a disk-backed /work.
The real Hermes loop and official MCP client talk only to loopback fixtures.
No provider credential or household service is involved.
"""

import inspect
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def main():
    from hermes_cli import auth_codex
    from run_agent import AIAgent
    from tools.mcp_tool_discovery import discover_mcp_tools
    from tools.mcp_tool_schema import mcp_prefixed_tool_name

    inspect.signature(AIAgent).bind_partial(provider="openai-codex", api_mode="codex_responses",
        enabled_toolsets=["mcp-agentic"], skip_memory=True, skip_context_files=True,
        skip_background_review=True, load_soul_identity=False, fallback_model=None,
        checkpoints_enabled=False, run_budget_seconds=30)
    inspect.signature(discover_mcp_tools).bind_partial(allowed_mcp_names=["agentic"])
    for name in ("_codex_request_device_code", "_codex_poll_authorization_code",
                 "_codex_exchange_authorization_code", "_save_codex_tokens", "resolve_codex_runtime_credentials"):
        assert callable(getattr(auth_codex, name)), name
    assert mcp_prefixed_tool_name("agentic", "host.status") == "mcp__agentic__host_status"
    observed = {"model": 0, "tools": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if self.path == "/mcp":
                assert self.headers.get("Authorization") == "Bearer compatibility-mcp-fixture"
                method = body.get("method")
                if method == "notifications/initialized":
                    self.send_response(202)
                    self.end_headers()
                    return
                if method == "initialize":
                    result = {"protocolVersion": body["params"]["protocolVersion"], "capabilities": {"tools": {}}, "serverInfo": {"name": "offline-fixture", "version": "1"}}
                elif method == "tools/list":
                    result = {"tools": [{"name": "host.status", "description": "Offline host fixture", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}]}
                elif method == "tools/call":
                    assert body["params"]["name"] == "host.status"
                    observed["tools"] += 1
                    result = {"content": [{"type": "text", "text": "offline fixture evidence"}]}
                else:
                    raise AssertionError(method)
                answer = {"jsonrpc": "2.0", "id": body["id"], "result": result}
            else:
                assert self.path == "/v1/chat/completions"
                observed["model"] += 1
                message = {"role": "assistant", "content": "compatibility fixture complete"}
                finish = "stop"
                if observed["model"] == 1:
                    message.update(content=None, tool_calls=[{"id": "compat-call", "type": "function", "function": {"name": "mcp__agentic__host_status", "arguments": "{}"}}])
                    finish = "tool_calls"
                answer = {"id": "compat", "object": "chat.completion", "created": 1, "model": "fixture-model", "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
                if body.get("stream"):
                    delta = dict(message)
                    if "tool_calls" in delta:
                        delta["tool_calls"] = [{"index": 0, **delta["tool_calls"][0]}]
                    chunk = {**answer, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: " + json.dumps({**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}) + "\n\ndata: [DONE]\n\n").encode())
                    return
            data = json.dumps(answer).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="hermes-compat-", dir=os.environ["TMPDIR"]) as directory:
            scratch = Path(directory)
            (scratch / ".hermes").mkdir()
            env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": directory,
                   "HERMES_HOME": str(scratch / ".hermes"), "TMPDIR": directory,
                   "PYTHONPATH": "/opt/hermes:/app", "PYTHONDONTWRITEBYTECODE": "1",
                   "AGENTIC_PARENT_PID": str(os.getpid()),
                   "AGENTIC_MCP_URL": f"http://127.0.0.1:{server.server_port}/mcp",
                   "AGENTIC_PROVIDER_URL": f"http://127.0.0.1:{server.server_port}/v1"}
            payload = {"provider": "openrouter", "model": "fixture-model", "api_key": "compatibility-api-fixture",
                       "mcp_token": "compatibility-mcp-fixture", "message": "Call host.status once then report its fixture evidence.",
                       "session_id": "compatibility-session", "run_id": "compatibility-run", "history": []}
            result = subprocess.run([sys.executable, "-m", "agentic_hermes.child"], input=json.dumps(payload),
                                    capture_output=True, text=True, cwd=scratch, env=env, timeout=60)
            assert result.returncode == 0, "Hermes child did not complete"
            response = json.loads(result.stdout)
            assert response.get("error") is None, "Hermes inference or MCP adapter is incompatible"
            assert response["tools"] == ["mcp__agentic__host_status"], "Unexpected tool authority"
            assert observed == {"model": 2, "tools": 1}, "Expected fixture tool roundtrip missing"
            assert response["answer"] == "compatibility fixture complete"
        print(json.dumps({"compatible": True, "observed_from": "runtime_container", "model": "protocol_fixture", "mcp": "official_client_loopback_fixture", "cloud_account_verified": False}))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


if __name__ == "__main__":
    main()
