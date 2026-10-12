"""Trusted fixed probe mounted read-only into a signed Controller candidate.

The only writable input is a scrubbed structural copy of the stopped database.
No access key, conversation, provider, household or Runner bearer is copied.
"""
import sqlite3
from pathlib import Path

from agentic_controller.store import Store

path = Path("/probe/controller.sqlite3")
with sqlite3.connect(path) as db:
    expected = db.execute("PRAGMA user_version").fetchone()[0]
store = Store(path)
assert store.db.execute("PRAGMA user_version").fetchone()[0] == expected
assert store.setting("paused") is True
assert store.setting("retention") == {"context_days": 7}
assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
store.db.close()
