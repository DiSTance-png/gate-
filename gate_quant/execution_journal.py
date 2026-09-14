from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


ACTIVE_STATUSES = (
    "prepared",
    "submitted",
    "pending_fill",
    "position_pending",
    "partially_filled",
    "stop_protected_tp_pending",
    "protection_pending",
    "manual_review",
)


class ExecutionJournal:
    """Durable Gate entry lifecycle state, isolated from logs and AI history."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS entry_intents (
                  client_id TEXT PRIMARY KEY,
                  environment TEXT NOT NULL,
                  contract TEXT NOT NULL,
                  requested_size TEXT NOT NULL,
                  baseline_position_size TEXT NOT NULL DEFAULT '0',
                  entry_price TEXT NOT NULL,
                  take_profit_price TEXT NOT NULL,
                  stop_loss_price TEXT NOT NULL,
                  status TEXT NOT NULL,
                  order_id TEXT NOT NULL DEFAULT '',
                  order_status TEXT NOT NULL DEFAULT '',
                  filled_size TEXT NOT NULL DEFAULT '0',
                  protected_size TEXT NOT NULL DEFAULT '0',
                  stop_revision INTEGER NOT NULL DEFAULT 0,
                  take_profit_revision INTEGER NOT NULL DEFAULT 0,
                  policy_version TEXT NOT NULL DEFAULT '',
                  policy_hash TEXT NOT NULL DEFAULT '',
                  last_error TEXT NOT NULL DEFAULT '',
                  created_at_ms INTEGER NOT NULL,
                  updated_at_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entry_intents_active
                  ON entry_intents(environment, status, updated_at_ms);
                CREATE TABLE IF NOT EXISTS execution_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  client_id TEXT NOT NULL,
                  event_type TEXT NOT NULL,
                  detail_json TEXT NOT NULL DEFAULT '{}',
                  created_at_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_execution_events_client
                  ON execution_events(client_id, id);
                """
            )

    def prepare(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = int(payload.get("created_at_ms") or time.time() * 1000)
        values = {
            "client_id": str(payload["client_id"]),
            "environment": str(payload["environment"]).lower(),
            "contract": str(payload["contract"]).upper(),
            "requested_size": str(payload["requested_size"]),
            "baseline_position_size": str(payload.get("baseline_position_size") or "0"),
            "entry_price": str(payload["entry_price"]),
            "take_profit_price": str(payload["take_profit_price"]),
            "stop_loss_price": str(payload["stop_loss_price"]),
            "status": "prepared",
            "policy_version": str(payload.get("policy_version") or ""),
            "policy_hash": str(payload.get("policy_hash") or ""),
            "created_at_ms": now,
            "updated_at_ms": now,
        }
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO entry_intents(
                     client_id,environment,contract,requested_size,baseline_position_size,
                     entry_price,take_profit_price,stop_loss_price,status,policy_version,
                     policy_hash,created_at_ms,updated_at_ms
                   ) VALUES (
                     :client_id,:environment,:contract,:requested_size,:baseline_position_size,
                     :entry_price,:take_profit_price,:stop_loss_price,:status,:policy_version,
                     :policy_hash,:created_at_ms,:updated_at_ms
                   )""",
                values,
            )
            self._append_event(connection, values["client_id"], "prepared", values, now)
        return self.get(values["client_id"]) or values

    def update(self, client_id: str, status: str | None = None, **fields: Any) -> dict[str, Any]:
        allowed = {
            "order_id", "order_status", "filled_size", "protected_size",
            "stop_revision", "take_profit_revision", "last_error",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if status is not None:
            updates["status"] = status
        if not updates:
            return self.get(client_id) or {}
        updates["updated_at_ms"] = int(time.time() * 1000)
        assignments = ",".join(f"{key}=?" for key in updates)
        params = [str(value) if key not in {"stop_revision", "take_profit_revision", "updated_at_ms"} else value for key, value in updates.items()]
        params.append(client_id)
        with self._connect() as connection:
            connection.execute(f"UPDATE entry_intents SET {assignments} WHERE client_id=?", params)
            self._append_event(connection, client_id, status or "updated", updates, int(updates["updated_at_ms"]))
        return self.get(client_id) or {}

    def get(self, client_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM entry_intents WHERE client_id=?", (client_id,)).fetchone()
        return dict(row) if row else None

    def active(self, environment: str, limit: int = 100) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM entry_intents WHERE environment=? AND status IN ({placeholders}) ORDER BY created_at_ms LIMIT ?",
                (environment.lower(), *ACTIVE_STATUSES, max(1, min(limit, 1000))),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _append_event(connection: sqlite3.Connection, client_id: str, event_type: str, detail: Any, now: int) -> None:
        connection.execute(
            "INSERT INTO execution_events(client_id,event_type,detail_json,created_at_ms) VALUES (?,?,?,?)",
            (client_id, event_type, json.dumps(detail, ensure_ascii=False, default=str), now),
        )
