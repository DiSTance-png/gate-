"""Synchronize Gate Futures position-close records into the Gate ledger."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gate_quant.client import GateFuturesClient
from gate_quant.config import load_settings

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
LEDGER = DATA / "trading_ledger.json"
BJ_TZ = timezone(timedelta(hours=8))


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _time_text(value) -> str:
    timestamp = _number(value)
    return datetime.fromtimestamp(timestamp, BJ_TZ).strftime("%Y-%m-%d %H:%M:%S") if timestamp > 0 else "--"


def _duration_text(start, end) -> str:
    seconds = max(0, int(_number(end) - _number(start)))
    if not seconds:
        return "--"
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}小时{minutes}分" if hours else (f"{minutes}分{secs}秒" if minutes else f"{secs}秒")


def _ledger_row(row: dict, quanto_multiplier: float = 0.0) -> dict | None:
    close_id = str(row.get("id") or row.get("order_id") or row.get("text") or "")
    contract = str(row.get("contract") or row.get("name") or "")
    if not close_id or not contract:
        return None
    side_value = str(row.get("side") or "").lower()
    is_long = side_value == "long"
    entry = _number(row.get("long_price") if is_long else row.get("short_price"))
    exit_price = _number(row.get("short_price") if is_long else row.get("long_price"))
    leverage = _number(row.get("lever") or row.get("leverage"))
    size = abs(_number(row.get("max_size") or row.get("accum_size") or row.get("size")))
    margin = abs(size * quanto_multiplier * entry / leverage) if size and quanto_multiplier and entry and leverage else 0.0
    net_pnl = _number(row.get("pnl") or row.get("realised_pnl") or row.get("profit"))
    gross_pnl = _number(row.get("pnl_pnl"), net_pnl)
    fee = _number(row.get("pnl_fee") or row.get("fee"))
    funding = _number(row.get("pnl_fund"))
    roi = net_pnl / margin * 100 if margin > 0 else 0.0
    first_open_time = row.get("first_open_time")
    close_time = row.get("time") or row.get("create_time") or row.get("finish_time")
    return {
        "id": f"gate_closed_{close_id}", "venue": "gate", "inst": contract,
        "side": "多" if is_long else ("空" if side_value == "short" else "未知"),
        "strategy": "Gate Futures", "margin": round(margin, 8), "sz": size,
        "lever": f"{leverage:g}x" if leverage > 0 else "--",
        "open_px": entry, "open_time": _time_text(first_open_time),
        "close_px": exit_price, "close_time": _time_text(close_time),
        "gross_pnl": round(gross_pnl, 8), "fee": round(fee, 8),
        "funding_fee": round(funding, 8), "pnl": round(net_pnl, 8),
        "net_pnl": round(net_pnl, 8), "roi": round(roi, 2), "roi_pct": round(roi, 2),
        "hold_duration": _duration_text(first_open_time, close_time),
        "status": "closed", "exit_reason": "Gate position_close",
        "margin_mode": str(row.get("margin_mode") or ""),
        "source": "Gate Futures position_close",
    }


def sync() -> int:
    settings = load_settings()
    if not settings.api_key or not settings.api_secret:
        print("Gate credentials unavailable; ledger sync skipped")
        return 0
    client = GateFuturesClient(settings)
    rows = client.position_close(limit=100) or []
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
    multipliers = {}
    for contract in {str(row.get("contract") or "") for row in rows if isinstance(row, dict)}:
        if not contract:
            continue
        try:
            metadata = client.contracts(contract) or {}
            multipliers[contract] = _number(metadata.get("quanto_multiplier"))
        except Exception:
            multipliers[contract] = 0.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        mapped = _ledger_row(row, multipliers.get(str(row.get("contract") or ""), 0.0))
        if not mapped:
            continue
        by_id[mapped["id"]] = mapped
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
