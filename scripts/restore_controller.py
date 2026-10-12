#!/usr/bin/env python3
"""Restore an encrypted Controller snapshot into an offline App data directory."""
import argparse
import getpass
import hashlib
import os
import sqlite3
import uuid
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def restore(archive: Path, destination: Path, password: str):
    raw = archive.read_bytes()
    if len(raw) < 52 or raw[:7] != b"AGHOME1":
        raise ValueError("不是 Agentic Home 加密備份")
    salt, nonce = raw[7:23], raw[23:35]
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 390000, 32)
    plain = AESGCM(key).decrypt(nonce, raw[35:], b"AGHOME1")
    if not plain.startswith(b"SQLite format 3\x00"):
        raise ValueError("備份不是 SQLite 資料庫")
    if destination.exists() or any(Path(str(destination) + ext).exists() for ext in ("-wal", "-shm")):
        raise ValueError("目標已存在，請停止 App 並把既有 DB／WAL／SHM 移至保留目錄，再還原")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / (".restore-" + uuid.uuid4().hex + ".sqlite3")
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(plain)
            file.flush()
            os.fsync(file.fileno())
        with sqlite3.connect(temporary.resolve().as_uri() + "?mode=ro&immutable=1", uri=True) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("資料庫完整性檢查失敗")
            if db.execute("PRAGMA user_version").fetchone()[0] not in (1, 2):
                raise ValueError("不相容的資料庫版本")
        # Atomic and exclusive: a concurrently created destination is never overwritten.
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    print("Controller 資料庫已還原。重新啟動 App；未完成副作用將讀回核對，不自動重送。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    restore(args.archive, args.destination, getpass.getpass("備份密碼："))
