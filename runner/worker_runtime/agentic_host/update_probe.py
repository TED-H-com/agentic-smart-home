"""Trusted fixed probe mounted read-only into a signed Controller candidate.

The only writable input is a scrubbed structural copy of the stopped database.
No access key, conversation, provider, household or Runner bearer is copied.
"""
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

# This trusted script is mounted at /fixed-probe.py. The candidate package is
# copied to /app, not pip-installed; script execution does not add WORKDIR.
sys.path.insert(0, "/app")
from agentic_controller.store import Store


def require_contracts(store):
    required = store.setting("required_ingress_bridge_contract")
    if required is not None:
        from agentic_controller.bridge import BRIDGE_CONTRACT
        assert type(required) is int and type(BRIDGE_CONTRACT) is int and BRIDGE_CONTRACT == required


def main():
    path = Path("/probe/controller.sqlite3")
    with closing(sqlite3.connect(path)) as db:
        expected = db.execute("PRAGMA user_version").fetchone()[0]
    store = Store(path)
    try:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == expected
        assert store.setting("paused") is True
        assert store.setting("retention") == {"context_days": 7}
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        require_contracts(store)
    finally:
        store.close()


if __name__ == "__main__":
    main()
