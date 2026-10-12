"""Hermes-owned Codex device login using pinned upstream auth helpers."""

import json
import logging
import os
import signal
import sys
from pathlib import Path

from .upstream import VERIFICATION_URI, HermesAdapter


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
        code = str(getattr(exc, "code", ""))
        emit({"state": "expired" if code == "device_code_timeout" else "failed",
              "error": "auth_expired" if code == "device_code_timeout" else "oauth_failed"})


if __name__ == "__main__":
    main()
