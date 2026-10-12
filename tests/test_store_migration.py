"""Actual SQLite upgrade, private snapshot, and rejection of future data."""
import json
import os
import sqlite3
import stat
import time
import uuid

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
    assert current.db.execute("PRAGMA user_version").fetchone()[0] == 3
    assert current.setting("gateway")["provider"] == "grok"
    assert current.get("operation", "op1")["phase"] == "committed"
    current.close()
    backups = list(tmp_path.glob("*.before-v3-*.sqlite3"))
    assert len(backups) == 1 and stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    with sqlite3.connect(backups[0]) as old:
        assert old.execute("PRAGMA user_version").fetchone()[0] == 1
        assert old.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert json.loads(old.execute("SELECT value FROM settings WHERE key='gateway'").fetchone()[0])["primary"] == "web"
    Store(path).close()
    assert len(list(tmp_path.glob("*.before-v3-*.sqlite3"))) == 1


def test_future_database_is_refused_without_modifying_it(tmp_path):
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as future:
        future.execute("PRAGMA user_version=99")
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="資料庫版本較新"):
        Store(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize('pending,expired_age,retained_age', [
    (None, 31, 1), (7, 8, 1), (60, 31, 20),
])
def test_migration_snapshot_retention_matches_only_own_regular_files(tmp_path, pending, expired_age, retained_age):
    # Brackets ensure the database basename is matched literally, not as a glob.
    path = tmp_path / 'controller[owner].sqlite3'
    with sqlite3.connect(path) as legacy:
        legacy.execute('PRAGMA user_version=2')
    current = Store(path)
    try:
        current.set_setting('retention', {'audit_days': 30, 'context_days': 7})
        if pending:
            current.set_setting('retention_pending', {'audit_days': pending, 'context_days': 1})
        snapshots = [p for p in tmp_path.iterdir() if p.name.startswith(path.name + '.before-v3-')]
        assert len(snapshots) == 1
        expired = snapshots[0]
        def age(file, days):
            timestamp = time.time() - days * 86400
            os.utime(file, (timestamp, timestamp), follow_symlinks=False)
        def own_name():
            return tmp_path / (path.name + '.before-v3-' + uuid.uuid4().hex + '.sqlite3')
        age(expired, expired_age)
        recent = own_name()
        recent.write_bytes(expired.read_bytes())
        age(recent, retained_age)
        unrelated = [tmp_path / ('other.sqlite3.before-v3-' + uuid.uuid4().hex + '.sqlite3'),
                     tmp_path / (path.name + '.before-v3-not-a-uuid.sqlite3'),
                     tmp_path / (path.name + '.before-v3-' + uuid.uuid4().hex + '.sqlite3.extra')]
        for file in unrelated:
            file.write_bytes(b'unrelated private data')
            age(file, 90)
        target = tmp_path / 'unrelated.sqlite3'
        target.write_bytes(b'never follow this link')
        age(target, 90)
        link = own_name()
        link.symlink_to(target)
        age(link, 90)
        directory = own_name()
        directory.mkdir()
        age(directory, 90)
        fifo = own_name()
        os.mkfifo(fifo, 0o600)
        age(fifo, 90)
        current.prune()
        assert not expired.exists()
        assert recent.is_file() and current.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert all(file.read_bytes() == b'unrelated private data' for file in unrelated)
        assert link.is_symlink() and target.read_bytes() == b'never follow this link'
        assert directory.is_dir() and stat.S_ISFIFO(fifo.lstat().st_mode)
    finally:
        current.close()
