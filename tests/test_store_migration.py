"""Actual SQLite upgrade, private snapshot, and rejection of future data."""
import json
import sqlite3
import stat

import pytest
from agentic_controller.store import Store


def test_v1_settings_and_audit_survive_with_private_pre_upgrade_snapshot(tmp_path):
    path = tmp_path / "controller.sqlite3"
    with sqlite3.connect(path) as legacy:
        legacy.executescript("""
          CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE records(kind TEXT NOT NULL,id TEXT NOT NULL,body TEXT NOT NULL,
            created TEXT NOT NULL,updated TEXT NOT NULL,PRIMARY KEY(kind,id));
          PRAGMA user_version=1;
        """)
        legacy.execute("INSERT INTO settings VALUES (?,?)", ("gateway", json.dumps({"primary": "web", "provider": "grok"})))
        legacy.execute("INSERT INTO records VALUES (?,?,?,?,?)", ("operation", "op1", '{"id":"op1","phase":"committed"}', "2026", "2026"))
    current = Store(path)
    assert current.db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert current.setting("gateway")["provider"] == "grok"
    assert current.get("operation", "op1")["phase"] == "committed"
    current.close()
    backups = list(tmp_path.glob("*.before-v2-*.sqlite3"))
    assert len(backups) == 1 and stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    with sqlite3.connect(backups[0]) as old:
        assert old.execute("PRAGMA user_version").fetchone()[0] == 1
        assert old.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert json.loads(old.execute("SELECT value FROM settings WHERE key='gateway'").fetchone()[0])["primary"] == "web"
    Store(path).close()
    assert len(list(tmp_path.glob("*.before-v2-*.sqlite3"))) == 1


def test_future_database_is_refused_without_modifying_it(tmp_path):
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as future:
        future.execute("PRAGMA user_version=99")
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="資料庫版本較新"):
        Store(path)
    assert path.read_bytes() == before
