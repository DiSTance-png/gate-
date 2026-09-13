from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


class EventStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL, level TEXT NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL)")
            db.execute(
                """CREATE TABLE IF NOT EXISTS trade_records (
                    id INTEGER PRIMARY KEY,
                    environment TEXT NOT NULL,
                    record_type TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    occurred_at INTEGER NOT NULL,
                    contract TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL,
                    synced_at INTEGER NOT NULL,
                    UNIQUE(environment, record_type, external_id)
                )"""
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_trade_records_time ON trade_records(occurred_at DESC)")

    def _connect(self):
        return sqlite3.connect(self.path)

    def add(self, event: str, detail: dict, level: str = "INFO") -> None:
        with self._connect() as db:
            db.execute("INSERT INTO events(created_at,level,event,detail) VALUES(?,?,?,?)", (int(time.time()), level, event, json.dumps(detail, ensure_ascii=False)))

    def recent(self, limit: int = 30) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT created_at,level,event,detail FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"created_at": r[0], "level": r[1], "event": r[2], "detail": json.loads(r[3])} for r in rows]

    def upsert_trade_records(self, records: list[dict]) -> None:
        now = int(time.time() * 1000)
        with self._connect() as db:
            db.executemany(
                """INSERT INTO trade_records(environment,record_type,external_id,occurred_at,contract,status,detail,synced_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(environment,record_type,external_id) DO UPDATE SET
                     occurred_at=excluded.occurred_at, contract=excluded.contract,
                     status=excluded.status, detail=excluded.detail, synced_at=excluded.synced_at""",
                [
                    (
                        str(row.get("environment") or "testnet"),
                        str(row.get("record_type") or "order"),
                        str(row["external_id"]),
                        int(row.get("occurred_at") or now),
                        str(row.get("contract") or ""),
                        str(row.get("status") or ""),
                        json.dumps(row.get("detail") or {}, ensure_ascii=False),
                        now,
                    )
                    for row in records
                    if row.get("external_id")
                ],
            )

    def trade_history(self, *, environment: str, page: int = 1, page_size: int = 20, query: str = "") -> dict:
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 100))
        where = "environment=?"
        params: list[object] = [environment]
        if query.strip():
            where += " AND (contract LIKE ? OR status LIKE ? OR detail LIKE ?)"
            needle = f"%{query.strip()}%"
            params.extend([needle, needle, needle])
        with self._connect() as db:
            total = int(db.execute(f"SELECT COUNT(*) FROM trade_records WHERE {where}", params).fetchone()[0])
            rows = db.execute(
                f"SELECT record_type,external_id,occurred_at,contract,status,detail FROM trade_records WHERE {where} ORDER BY occurred_at DESC,id DESC LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
        return {
            "items": [
                {"record_type": row[0], "external_id": row[1], "occurred_at": row[2], "contract": row[3], "status": row[4], "detail": json.loads(row[5])}
                for row in rows
            ],
            "page": page,
            "page_size": page_size,
            "total": total,
            "pages": max(1, (total + page_size - 1) // page_size),
        }
