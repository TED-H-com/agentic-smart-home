"""One disk owner replays immutable memory transactions in ordered FULL batches."""
import asyncio
import logging
import queue
import sqlite3
import threading
from pathlib import Path


def before_disk_commit():
    """Test hook on the writer thread, before a FULL commit."""


def _resolve(future, error=None):
    if not future.done():
        if error:
            future.set_exception(error)
        else:
            future.set_result(None)


def _notify(loop, future, error=None):
    # Cancelled callers can close their loop while a disk operation still drains.
    # Delivery failure must never terminate the writer or discard queued writes.
    try:
        loop.call_soon_threadsafe(_resolve, future, error)
    except RuntimeError:
        if not loop.is_closed():
            raise


class Journal:
    def __init__(self, disk):
        self.disk = disk
        self.queue = queue.Queue()
        self.lock = threading.Lock()
        self.seq = self.durable_seq = 0
        self.error = None
        self.waiters = []
        self.thread = threading.Thread(target=self.run, name="controller-store-writer", daemon=True)
        self.thread.start()

    def check(self):
        if self.error:
            raise RuntimeError("資料庫持久化失敗") from self.error

    def publish(self, seq):
        with self.lock:
            self.durable_seq = seq
            remaining = []
            for target, loop, future in self.waiters:
                if self.error or target <= seq:
                    _notify(loop, future, RuntimeError("資料庫持久化失敗") if self.error else None)
                else:
                    remaining.append((target, loop, future))
            self.waiters = remaining

    async def durable(self):
        with self.lock:
            self.check()
            if self.seq <= self.durable_seq:
                return
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            self.waiters.append((self.seq, loop, future))
        await future

    def run(self):
        pending = None
        try:
            while True:
                item = pending if pending is not None else self.queue.get()
                pending = None
                if item is None:
                    break
                if isinstance(item[0], Path):
                    path, loop, future = item
                    try:
                        self.check()
                        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        path.touch(mode=0o600, exist_ok=True)
                        path.chmod(0o600)
                        target = sqlite3.connect(path)
                        try:
                            self.disk.backup(target)
                        finally:
                            target.close()
                    except Exception:
                        _notify(loop, future, RuntimeError("資料庫快照失敗"))
                    else:
                        _notify(loop, future)
                    continue
                batch = [item]
                stop = False
                while True:
                    try:
                        next_item = self.queue.get_nowait()
                    except queue.Empty:
                        break
                    if next_item is None:
                        stop = True
                        break
                    if isinstance(next_item[0], Path):
                        pending = next_item
                        break
                    batch.append(next_item)
                if not self.error:
                    try:
                        self.disk.execute("BEGIN IMMEDIATE")
                        for _, statements in batch:
                            for sql, params in statements:
                                self.disk.execute(sql, params)
                        before_disk_commit()
                        self.disk.commit()
                    except Exception as error:
                        try:
                            self.disk.rollback()
                        except Exception:
                            pass
                        with self.lock:
                            self.error = error
                        logging.getLogger(__name__).error("Controller database journal failed")
                self.publish(batch[-1][0])
                if stop:
                    break
        finally:
            self.disk.close()

    def close(self):
        self.queue.put(None)
        self.thread.join()


def close_store(journal, memory):
    journal.close()
    memory.close()


def _parameter(value):
    if value is None or isinstance(value, (str, bytes, int, float)):
        return value
    try:
        # SQLite also accepts other buffer providers, such as array.array.
        return bytes(memoryview(value))
    except TypeError:
        # Materialize an adapter once on the loop, rather than on both owners.
        return _parameter(sqlite3.adapt(value))


class Transaction:
    def __init__(self, db):
        self.db = db
        self.statements = []

    def execute(self, sql, params=()):
        params = tuple(_parameter(value) for value in params)
        cursor = self.db.execute(sql, params)
        self.statements.append((sql, params))
        return cursor

    def executemany(self, sql, params):
        for values in params:
            self.execute(sql, values)
