"""Durable identity for Gate price orders whose custom text is not echoed."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from decimal import Decimal, InvalidOperation
from pathlib import Path

STORE_PATH = Path(__file__).resolve().parents[1] / 'data' / 'gate_protection_orders.db'


def order_id(row: dict) -> str:
    return str(row.get('id_string') or row.get('id') or '')


def matches(row: dict, *, client_id: str, contract: str, size, trigger_price, rule: int,
            exchange_id: str = '') -> bool:
    if not isinstance(row, dict):
        return False
    initial, trigger = row.get('initial') or {}, row.get('trigger') or {}
    if not isinstance(initial, dict) or not isinstance(trigger, dict):
        return False
    identity = (order_id(row) == exchange_id if exchange_id else
                str(row.get('_gate_verified_client_id') or initial.get('text') or '') == client_id)
    try:
        wanted, actual = Decimal(str(size)), Decimal(str(initial.get('size') or 0))
        price = Decimal(str(trigger.get('price') or 0))
        side = 'long' if wanted < 0 else 'short'
        auto = str(initial.get('auto_size') or '').lower()
        kind = str(row.get('order_type') or '').lower()
        full_close = actual == 0 and auto == f'close_{side}' and kind in ('', f'close-{side}-position')
        sized = actual == wanted and wanted != 0 and not auto and not kind.startswith('close-')
        safe = bool(initial.get('reduce_only') or initial.get('is_reduce_only')
                    or initial.get('close') or initial.get('is_close'))
        return bool(identity and safe and (sized or full_close)
                    and str(initial.get('contract') or '').upper() == contract.upper()
                    and str(row.get('status') or '').lower() == 'open'
                    and int(trigger.get('rule') or 0) == int(rule)
                    and price == Decimal(str(trigger_price)) and price > 0
                    and int(trigger.get('strategy_type') or 0) == 0
                    and int(trigger.get('price_type') or 0) == 0
                    and Decimal(str(initial.get('price') or 0)) == 0
                    and str(initial.get('tif') or 'ioc').lower() == 'ioc')
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return False


class ProtectionIdentityStore:
    def __init__(self, scope: str, path: Path | None = None):
        self.scope, self.path = scope, Path(path or STORE_PATH)

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        db.execute('''CREATE TABLE IF NOT EXISTS protections (
            scope TEXT NOT NULL, client_id TEXT NOT NULL, exchange_id TEXT NOT NULL DEFAULT '',
            request_json TEXT NOT NULL, submitted_at_ms INTEGER NOT NULL,
            PRIMARY KEY(scope,client_id))''')
        return db

    def get(self, client_id: str):
        with closing(self._connect()) as db:
            row = db.execute('SELECT * FROM protections WHERE scope=? AND client_id=?',
                             (self.scope, client_id)).fetchone()
        return dict(row) if row else None

    def records(self):
        with closing(self._connect()) as db:
            return [dict(row) for row in db.execute('SELECT * FROM protections WHERE scope=?', (self.scope,))]

    def prepare(self, client_id: str, request: dict):
        with closing(self._connect()) as db, db:
            db.execute('INSERT INTO protections(scope,client_id,request_json,submitted_at_ms) VALUES (?,?,?,?)',
                       (self.scope, client_id, json.dumps(request), int(time.time() * 1000)))

    def bind(self, client_id: str, exchange_id: str):
        if not exchange_id:
            return
        with closing(self._connect()) as db, db:
            db.execute('UPDATE protections SET exchange_id=? WHERE scope=? AND client_id=?',
                       (exchange_id, self.scope, client_id))

    def annotate(self, rows: list[dict]) -> list[dict]:
        known = {r['exchange_id']: r for r in self.records() if r['exchange_id']}
        result = []
        for row in rows:
            record = known.get(order_id(row))
            if record and matches(row, client_id=record['client_id'], exchange_id=record['exchange_id'],
                                  **json.loads(record['request_json'])):
                row = {**row, '_gate_verified_client_id': record['client_id']}
            result.append(row)
        return result


def confirm_protection(client, created: dict, *, client_id: str, contract: str, size,
                       trigger_price: str, rule: int) -> dict:
    if not isinstance(created, dict):
        raise RuntimeError('Gate protection creation response is missing')
    payload = created.get('order') if isinstance(created.get('order'), dict) else created
    expected_id = order_id(payload)
    last_error = ''
    for attempt in range(3):
        if expected_id and hasattr(client, 'price_order'):
            try:
                row = client.price_order(expected_id)
                if isinstance(row, dict) and matches(row, client_id=client_id, contract=contract,
                        size=size, trigger_price=trigger_price, rule=rule, exchange_id=expected_id):
                    return row
            except RuntimeError as exc:
                last_error = str(exc)
        try:
            rows = client.protection_orders(contract) or []
        except RuntimeError as exc:
            last_error = str(exc)
            rows = []
        for row in rows:
            if isinstance(row, dict) and matches(row, client_id=client_id, contract=contract,
                    size=size, trigger_price=trigger_price, rule=rule, exchange_id=expected_id):
                return row
        if attempt < 2:
            time.sleep(0.1 * (attempt + 1))
    raise RuntimeError('Gate protection creation was not confirmed' + (f': {last_error}' if last_error else ''))
