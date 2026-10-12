from __future__ import annotations

import json
import re
import sqlite3
import stat
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import canonical, utcnow


class Conflict(Exception):
    pass


class Store:
    """One process owns this WAL database. Every state transition is committed first."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3):
            self.db.close()
            raise RuntimeError("資料庫版本較新，請使用相容 App 或完整還原")
        if version in (1, 2):
            # V3 records and Gateway preferences have new semantics despite
            # retaining the SQL tables. Old binaries must refuse the new data.
            # Keep a consistent, private pre-migration backup, never overwrite it.
            backup = path.with_name(path.name + ".before-v3-" + uuid.uuid4().hex + ".sqlite3")
            backup.touch(mode=0o600, exist_ok=False)
            with sqlite3.connect(backup) as target:
                self.db.backup(target)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS records (
                kind TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL,
                created TEXT NOT NULL, updated TEXT NOT NULL, PRIMARY KEY(kind,id));
            CREATE TABLE IF NOT EXISTS requests (
                scope TEXT NOT NULL, key TEXT NOT NULL, digest TEXT NOT NULL,
                result TEXT NOT NULL, PRIMARY KEY(scope,key));
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                at TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS conversation_scope ON records (
                json_extract(body, '$.scope'), created DESC) WHERE kind='conversation';
            -- V2 worker records were diagnostic placeholders and launched no writer.
            UPDATE records SET body=json_set(body,'$.stop_confirmed',json('true'))
              WHERE kind='work_job' AND json_extract(body,'$.state') IN ('blocked','cancelled')
              AND json_extract(body,'$.execution_started')=0 AND json_type(body,'$.lease_epoch') IS NULL;
            PRAGMA user_version=3;
            COMMIT;
        """)
        path.chmod(0o600)

    def setting(self, key, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        self.set_settings({key: value})

    def set_settings(self, values):
        """Commit related authorization settings atomically."""
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO settings VALUES (?,?)",
                                [(key, canonical(value)) for key, value in values.items()])

    def get(self, kind, id):
        row = self.db.execute("SELECT body FROM records WHERE kind=? AND id=?", (kind, id)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, kind, body):
        now = utcnow()
        with self.db:
            self.db.execute("""INSERT INTO records VALUES (?,?,?,?,?)
                ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body,updated=excluded.updated""",
                (kind, body["id"], canonical(body), now, now))
        return body

    def list(self, kind, limit=200):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT body FROM records WHERE kind=? ORDER BY created DESC LIMIT ?", (kind, limit))]

    def request(self, scope, key, request_digest, result=None):
        if not key or len(key) > 200:
            raise Conflict("缺少有效的 Idempotency-Key")
        row = self.db.execute("SELECT digest,result FROM requests WHERE scope=? AND key=?", (scope, key)).fetchone()
        if row:
            if row[0] != request_digest:
                raise Conflict("相同 Idempotency-Key 已用於不同內容")
            return row[1]
        if result is not None:
            with self.db:
                self.db.execute("INSERT INTO requests VALUES (?,?,?,?)", (scope, key, request_digest, result))
        return None

    def conversation(self, *, id=None, scope=None):
        """Respect context retention even before the periodic cleanup runs."""
        days = self.setting("retention", {"context_days": 7})["context_days"]
        pending = self.setting("retention_pending")
        if pending:
            days = min(days, pending["context_days"])
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        clause, value = ("id=?", id) if id else ("json_extract(body, '$.scope')=?", scope)
        row = self.db.execute(
            f"SELECT body FROM records WHERE kind='conversation' AND {clause} AND created>=? "
            "ORDER BY created DESC LIMIT 1", (value, cutoff)).fetchone()
        return json.loads(row[0]) if row else None

    def event(self, task_id, kind, body):
        with self.db:
            self.db.execute("INSERT INTO events(task_id,at,kind,body) VALUES (?,?,?,?)",
                            (task_id, utcnow(), kind, canonical(body)))

    def events(self, task_id):
        return [{**dict(r), "body": json.loads(r["body"])} for r in self.db.execute(
            "SELECT * FROM events WHERE task_id=? ORDER BY seq", (task_id,))]

    def transition(self, task, state, **fields):
        current = self.get("task", task["id"]) or task
        task = {**current, **fields, "state": state, "updated_at": utcnow()}
        with self.db:
            self.db.execute("UPDATE records SET body=?,updated=? WHERE kind='task' AND id=?",
                            (canonical(task), utcnow(), task["id"]))
            self.db.execute("INSERT INTO events(task_id,at,kind,body) VALUES (?,?,?,?)",
                            (task["id"], utcnow(), state, canonical(fields)))
        return task

    def recover(self):
        for task in self.list("host_task", 10000):
            if task["state"] == "planning":
                self.put("host_task", {**task, "state": "failed", "message": "主機查詢已中斷；沒有執行建議的操作。"})
        for task in self.list("task", 10000):
            if task["state"] in {"executing", "verifying"}:
                self.transition(task, "needs_reconcile", message="重啟後需要讀回確認；不會重送副作用")
            elif task["state"] in {"queued", "planning"}:
                self.transition(task, "failed", message="處理程序已重啟，請重新送出需求")

    def snapshot(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with sqlite3.connect(path) as target:
            self.db.backup(target)
        path.chmod(0o600)

    def prune(self):
        days = self.setting("retention", {"audit_days": 30, "context_days": 7})
        pending = self.setting("retention_pending")
        if pending:
            days = {key: min(days[key], pending[key]) for key in ("audit_days", "context_days")}
        cutoff = (datetime.now(UTC) - timedelta(days=days["audit_days"])).isoformat()
        context_cutoff = (datetime.now(UTC) - timedelta(days=days["context_days"])).isoformat()
        with self.db:
            self.db.execute("DELETE FROM records WHERE kind='conversation' AND created < ?", (context_cutoff,))
            # Capability hashes and context-bearing evidence expire independently
            # of audit handles. Idempotency tombstones deliberately remain.
            self.db.execute("DELETE FROM records WHERE kind='mcp_grant' AND json_extract(body,'$.expires') < ?",
                            (time.time(),))
            self.db.execute("DELETE FROM records WHERE kind IN ('hermes_case','case_evidence') AND created < ?",
                            (context_cutoff,))
            self.db.execute("DELETE FROM records WHERE kind='hermes_run' AND created < ? "
                            "AND json_extract(body,'$.state') IN ('succeeded','failed','cancelled','interrupted','blocked')",
                            (cutoff,))
        for task in self.list("host_task", 10000):
            if task["state"] != "planning" and task["created_at"] < context_cutoff:
                self.put("host_task", {"id": task["id"], "state": task["state"], "created_at": task["created_at"],
                                       "message": "主機查詢內容已依保存設定移除。"})
        protected = {o["proposal_id"] for o in self.list("owned", 10000)}
        for task in self.list("task", 100000):
            terminal = task["state"] in {"succeeded", "failed", "cancelled"}
            if task["updated_at"] < context_cutoff and task.get("sources"):
                task.pop("sources")
                task["sources_expired"] = True
                self.put("task", task)
            if terminal and task["updated_at"] < cutoff and task.get("proposal_id") not in protected:
                with self.db:
                    self.db.execute("DELETE FROM records WHERE kind='task' AND id=?", (task["id"],))
                    for kind in ("proposal", "approval", "operation"):
                        self.db.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, task.get("proposal_id")))
        with self.db:
            self.db.execute("DELETE FROM events WHERE at < ?", (cutoff,))
            # Context-bearing planning events are temporary working data, not permanent audit.
            self.db.execute("DELETE FROM events WHERE at < ? AND kind='planning'", (context_cutoff,))
        snapshots = self.path.parent / "snapshots"
        if snapshots.is_dir():
            for path in snapshots.glob("*.sqlite3"):
                if path.is_file() and not path.is_symlink() and path.stat().st_mtime < time.time() - days["audit_days"] * 86400:
                    path.unlink()
        # Migration copies contain the same private audit data. Match only
        # snapshots produced for this exact database; lstat never follows links.
        migration_name = re.compile(re.escape(self.path.name) + r"\.before-v3-[0-9a-f]{32}\.sqlite3")
        snapshot_cutoff = time.time() - days["audit_days"] * 86400
        for path in self.path.parent.iterdir():
            if not migration_name.fullmatch(path.name):
                continue
            try:
                metadata = path.lstat()
                if stat.S_ISREG(metadata.st_mode) and metadata.st_mtime < snapshot_cutoff:
                    path.unlink()
            except FileNotFoundError:
                pass  # Another cleanup already removed this exact entry.

    def close(self):
        self.db.close()
