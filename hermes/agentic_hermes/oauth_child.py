"""Hermes-owned Codex device login using pinned upstream auth helpers."""

import json
import logging
import os
import signal
import sys
from pathlib import Path

from .upstream import VERIFICATION_URI, HermesAdapter

# Provider bodies stay in the child. Only these codes cross the pipe.
LOGIN_ERRORS = {
    "access_denied": ("failed", "access_denied"),
    "slow_down": ("failed", "slow_down"),
    "expired_token": ("expired", "expired_token"),
    "device_code_timeout": ("expired", "expired_token"),
}


def classify_login(exc):
    """Map a device-login failure to a stable reason. Never return provider text."""
    code = str(getattr(exc, "code", "") or getattr(exc, "error", "") or "")
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "status", None)
    if code in LOGIN_ERRORS:
        return LOGIN_ERRORS[code]
    if status == 429 or code == "429":
        return LOGIN_ERRORS["slow_down"]
    text = code.lower()
    if "access_denied" in text:
        return LOGIN_ERRORS["access_denied"]
    if "slow_down" in text:
        return LOGIN_ERRORS["slow_down"]
    if "expired_token" in text or "device_code_timeout" in text:
        return LOGIN_ERRORS["expired_token"]
    return "failed", "oauth_failed"


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
        adapter = HermesAdapter()
        # No endpoint overrides, borrowed CLI grants or interactive token import.
        home = Path(os.environ["HERMES_HOME"])
        (home / "config.yaml").write_text(json.dumps({"auth": {"adopt_external_logins": False}}))
        device = adapter.request_device()
        emit({"state": "pending", "verification_uri": VERIFICATION_URI, "user_code": device["user_code"]})
        adapter.finish_device(device)
        emit({"state": "configured"})
    except Exception as exc:
        state, reason = classify_login(exc)
        emit({"state": state, "error": reason, "reason": reason})


if __name__ == "__main__":
    main()
