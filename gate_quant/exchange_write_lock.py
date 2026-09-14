from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "data" / "gate_exchange_write.lock.db"
_LOCAL = threading.local()


class GateWriteBusy(RuntimeError):
    """Another process currently owns the Gate exchange mutation lane."""


@contextmanager
def gate_write_lock(*, timeout_seconds: float = 30.0, path: Path = LOCK_PATH) -> Iterator[None]:
    """Cross-platform, crash-safe and thread-reentrant Gate write lock."""
    depth = int(getattr(_LOCAL, "depth", 0))
    if depth:
        _LOCAL.depth = depth + 1
        try:
            yield
        finally:
            _LOCAL.depth -= 1
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=max(0.1, timeout_seconds), isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=%d" % int(max(0.1, timeout_seconds) * 1000))
        connection.execute("CREATE TABLE IF NOT EXISTS gate_write_lock (id INTEGER PRIMARY KEY CHECK(id=1))")
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise GateWriteBusy("Gate 写操作繁忙，请稍后重试；当前请求未发送到交易所") from exc
        _LOCAL.depth = 1
        try:
            yield
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            _LOCAL.depth = 0
    finally:
        connection.close()
