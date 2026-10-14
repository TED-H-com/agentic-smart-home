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
        self.lock = threading.RLock()
        self.seq = self.durable_seq = 0
        self.error = None
        self.closed = False
        self.waiters = []
        self.thread = threading.Thread(target=self.run, name="controller-store-writer", daemon=True)
        self.thread.start()

    def fail(self, error):
        with self.lock:
            if self.error is None:
                self.error = error
                logging.getLogger(__name__).error("Controller database journal failed")
            waiters, self.waiters = self.waiters, []
        for _, loop, future in waiters:
            try:
                _notify(loop, future, RuntimeError("資料庫持久化失敗"))
            except Exception:
                pass

    def check(self):
        if not self.closed and not self.thread.is_alive():
            self.fail(RuntimeError("Database writer stopped"))
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
        item = None
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
                        self.fail(error)
                self.publish(batch[-1][0])
                if stop:
                    break
        except BaseException as error:
            self.fail(error)
            items = [pending] if pending is not None else []
            if item is not None and isinstance(item[0], Path):
                items.append(item)
            while True:
                try:
                    items.append(self.queue.get_nowait())
                except queue.Empty:
                    break
            for item in items:
                if item is not None and isinstance(item[0], Path):
                    _, loop, future = item
                    try:
                        _notify(loop, future, RuntimeError("資料庫持久化失敗"))
                    except Exception:
                        pass
        finally:
            try:
                self.disk.close()
            except BaseException as error:
                self.fail(error)

    def close(self):
        self.closed = True
        self.queue.put(None)
        self.thread.join()


def close_store(journal, memory):
    journal.close()
    memory.close()


def _parameter(value):
    if value is None or type(value) in (str, bytes, int, float):
        return value
    if isinstance(value, (str, bytes, int, float)):
        try:
            return _parameter(sqlite3.adapt(value))
        except sqlite3.ProgrammingError:
            for native in (str, bytes, int, float):
                if isinstance(value, native):
                    return native(value)
    try:
        # SQLite also accepts other buffer providers, such as array.array.
        return bytes(memoryview(value))
    except TypeError:
        # Materialize an adapter once on the loop, rather than on both owners.
        return _parameter(sqlite3.adapt(value))


class Transaction:
    def __init__(self, store):
        self.store = store
        self.db = store.db
        self.statements = []
        self.active = True

    def execute(self, sql, params=()):
        if not self.active:
            raise RuntimeError("Store transaction is closed")
        if not isinstance(params, (list, tuple)):
            raise TypeError("SQL parameters must be a list or tuple")
        params = tuple(_parameter(value) for value in params)
        self.store._recording = True
        try:
            cursor = self.db.execute(sql, params)
        finally:
            self.store._recording = False
        self.statements.append((sql, params))
        return cursor

    def executemany(self, sql, params):
        for values in params:
            self.execute(sql, values)
