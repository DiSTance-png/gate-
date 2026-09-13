"""Synchronize Gate Futures position-close records into the Gate ledger."""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from gate_quant.client import GateFuturesClient
from gate_quant.config import load_settings

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
LEDGER = DATA / "trading_ledger.json"


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def sync() -> int:
    settings = load_settings()
    if not settings.api_key or not settings.api_secret:
        print("Gate credentials unavailable; ledger sync skipped")
        return 0
    rows = GateFuturesClient(settings).position_close(limit=100) or []
    if not isinstance(rows, list):
        rows = []
    existing = []
    if LEDGER.exists():
        try:
            payload = json.loads(LEDGER.read_text(encoding="utf-8"))
            existing = payload if isinstance(payload, list) else []
        except (OSError, json.JSONDecodeError):
            existing = []
    by_id = {str(item.get("id")): item for item in existing if isinstance(item, dict) and item.get("id")}
    for row in rows:
        if not isinstance(row, dict):
            continue
        close_id = str(row.get("id") or row.get("order_id") or row.get("text") or "")
        contract = str(row.get("contract") or row.get("name") or "")
        if not close_id or not contract:
            continue
        timestamp = _number(row.get("time") or row.get("create_time") or row.get("finish_time"))
        close_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp)) if timestamp > 0 else "--"
        pnl = _number(row.get("pnl") or row.get("realised_pnl") or row.get("profit"))
        fee = _number(row.get("fee") or row.get("手续费"))
        size = abs(_number(row.get("size") or row.get("close_size") or row.get("qty")))
        entry = _number(row.get("entry_price") or row.get("open_price"))
        exit_price = _number(row.get("close_price") or row.get("fill_price") or row.get("price"))
        leverage = _number(row.get("leverage") or 1.0, 1.0)
        margin = round(abs(size * entry / leverage), 8) if size and entry and leverage else 0.0
        by_id[f"gate_closed_{close_id}"] = {
            "id": f"gate_closed_{close_id}", "venue": "gate", "inst": contract,
            "side": "多" if _number(row.get("size")) > 0 else "空",
            "strategy": "Gate Futures", "margin": margin, "sz": size,
            "open_px": entry, "close_px": exit_price, "close_time": close_time,
            "gross_pnl": round(pnl, 8), "fee": round(fee, 8),
            "pnl": round(pnl - abs(fee), 8), "net_pnl": round(pnl - abs(fee), 8),
            "status": "closed", "exit_reason": "Gate position_close",
        }
    DATA.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".gate-ledger-", suffix=".tmp", dir=DATA)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(list(by_id.values()), handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, LEDGER)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    print(f"Gate position_close ledger synchronized: {len(rows)} rows")
    return len(rows)


if __name__ == "__main__":
    sync()
