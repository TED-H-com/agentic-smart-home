"""Hermes-owned Codex device login using pinned upstream auth helpers."""

import json
import logging
import os
import re
import signal
import sys
from pathlib import Path


def main():
    import ctypes
    parent = int(os.environ["AGENTIC_PARENT_PID"])
    if ctypes.CDLL(None).prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
        raise SystemExit(1)
    output = sys.stdout
    sys.stdout = sys.stderr
    logging.disable(logging.CRITICAL)

    def emit(value):
        output.write(json.dumps(value) + "\n")
        output.flush()

    try:
        from hermes_cli.auth_codex import (
            CODEX_OAUTH_CLIENT_ID,
            _codex_exchange_authorization_code,
            _codex_poll_authorization_code,
            _codex_request_device_code,
            _save_codex_tokens,
        )
        # No endpoint overrides, borrowed CLI grants or interactive token import.
        home = Path(os.environ["HERMES_HOME"])
        (home / "config.yaml").write_text(json.dumps({"auth": {"adopt_external_logins": False}}))
        issuer = "https://auth.openai.com"
        device = _codex_request_device_code(issuer, CODEX_OAUTH_CLIENT_ID)
        user_code = device["user_code"]
        if not re.fullmatch(r"[A-Z0-9]{4,8}-[A-Z0-9]{4,8}", user_code):
            raise ValueError("invalid_user_code")
        emit({"state": "pending", "verification_uri": issuer + "/codex/device", "user_code": user_code})
        authorization = _codex_poll_authorization_code(
            issuer, device_auth_id=device["device_auth_id"], user_code=user_code,
            poll_interval=max(3, min(30, int(device["interval"]))),
        )
        tokens = _codex_exchange_authorization_code(issuer, CODEX_OAUTH_CLIENT_ID, authorization)
        if not all(isinstance(tokens.get(key), str) and tokens[key] for key in ("access_token", "refresh_token")):
            raise ValueError("incomplete_grant")
        _save_codex_tokens({key: tokens[key] for key in ("access_token", "refresh_token")})
        emit({"state": "configured"})
    except Exception as exc:
        code = str(getattr(exc, "code", ""))
        emit({"state": "expired" if code == "device_code_timeout" else "failed",
              "error": "auth_expired" if code == "device_code_timeout" else "oauth_failed"})


if __name__ == "__main__":
    main()
