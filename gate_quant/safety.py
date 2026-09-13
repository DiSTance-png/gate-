from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .service import protection_coverage_status


def decimal_value(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def classify_error(exc: Any) -> str:
    text = str(exc).upper()
    if "401" in text or "INVALID_KEY" in text:
        return "authentication"
    if "403" in text or "FORBIDDEN" in text or "PERMISSION" in text:
        return "permission"
    if "429" in text or "RATE LIMIT" in text or "TOO MANY" in text:
        return "rate_limit"
    if "TIMEOUT" in text or "TIMED OUT" in text:
        return "timeout"
    if "PROXY" in text or "TUNNEL" in text:
        return "proxy"
    if "5" in text and "HTTP 5" in text:
        return "gate_service"
    return "unknown"


def epoch_seconds(row: dict, *keys: str) -> float | None:
    for key in keys:
        raw = decimal_value(row.get(key))
        if raw <= 0:
            continue
        value = float(raw)
        if value > 10_000_000_000:
            value /= 1000.0
        return value
    return None


def order_age_seconds(order: dict, now: float | None = None) -> float | None:
    created = epoch_seconds(order, "create_time_ms", "create_time", "ctime")
    return None if created is None else max(0.0, (now or time.time()) - created)


def position_age_seconds(position: dict, now: float | None = None) -> float | None:
    created = epoch_seconds(position, "create_time_ms", "create_time", "open_time_ms", "open_time")
    return None if created is None else max(0.0, (now or time.time()) - created)


def _is_entry_order(order: dict) -> bool:
    return not bool(order.get("reduce_only")) and not bool(order.get("close"))


def reconcile_exchange_state(
    positions: list[dict], orders: list[dict], protections: list[dict], *,
    max_pending_age_seconds: int, now: float | None = None,
) -> dict[str, Any]:
    now = now or time.time()
    active = {str(p.get("contract") or "").upper(): p for p in positions if decimal_value(p.get("size")) != 0}
    pending_contracts = {str(o.get("contract") or "").upper() for o in orders if _is_entry_order(o)}
    issues: list[dict[str, Any]] = []
    stale_orders: list[dict] = []
    orphan_protections: list[dict] = []
    for contract, position in active.items():
        contract_protections = [p for p in protections if str((p.get("initial") or {}).get("contract") or p.get("contract") or "").upper() == contract]
        coverage = protection_coverage_status(contract_protections, decimal_value(position.get("size")))
        if not coverage["fully_protected"]:
            issues.append({"code": "protection_gap", "contract": contract, "required": str(coverage["required"]), "tp": str(coverage["take_profit"]), "sl": str(coverage["stop_loss"])})
    for order in orders:
        if not _is_entry_order(order):
            continue
        age = order_age_seconds(order, now)
        if age is None:
            issues.append({"code": "order_age_unknown", "contract": order.get("contract"), "order_id": str(order.get("id") or "")})
        elif max_pending_age_seconds > 0 and age >= max_pending_age_seconds:
            stale_orders.append({**order, "age_seconds": int(age)})
    for protection in protections:
        initial = protection.get("initial") or {}
        contract = str(initial.get("contract") or protection.get("contract") or "").upper()
        if contract and contract not in active and contract not in pending_contracts:
            orphan_protections.append(protection)
    if stale_orders:
        issues.append({"code": "stale_entry_orders", "count": len(stale_orders)})
    if orphan_protections:
        issues.append({"code": "orphan_protections", "count": len(orphan_protections)})
    return {"safe_for_new_risk": not issues, "issues": issues, "stale_orders": stale_orders, "orphan_protections": orphan_protections, "checked_at_ms": int(now * 1000)}


def daily_loss_state(ledger_path: Path, *, max_loss_usd: float, max_loss_ratio: float, equity: float, now: float | None = None) -> dict[str, Any]:
    now = now or time.time()
    local_day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    try:
        rows = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else []
    except (OSError, json.JSONDecodeError):
        return {"tripped": True, "reason": "ledger_unreadable", "loss_usd": 0.0}
    pnl = Decimal("0")
    for row in rows if isinstance(rows, list) else []:
        if row.get("status") != "closed" or local_day not in str(row.get("close_time") or ""):
            continue
        pnl += decimal_value(row.get("net_pnl", row.get("pnl")))
    loss = max(Decimal("0"), -pnl)
    absolute_limit = decimal_value(max_loss_usd)
    ratio_limit = decimal_value(equity) * decimal_value(max_loss_ratio)
    limits = [value for value in (absolute_limit, ratio_limit) if value > 0]
    limit = min(limits) if limits else Decimal("0")
    return {"tripped": limit <= 0 or loss >= limit, "reason": "daily_loss_limit" if limit > 0 and loss >= limit else ("daily_loss_limit_unconfigured" if limit <= 0 else "ok"), "loss_usd": float(loss), "limit_usd": float(limit), "net_pnl_usd": float(pnl)}


def cooldown_state(ledger_path: Path, *, cooldown_seconds: int, contract: str | None = None, now: float | None = None) -> dict[str, Any]:
    now = now or time.time()
    try:
        rows = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else []
    except (OSError, json.JSONDecodeError):
        return {"active": True, "reason": "ledger_unreadable"}
    latest_loss = 0.0
    wanted_contract = str(contract or "").upper()
    for row in rows if isinstance(rows, list) else []:
        if row.get("status") != "closed" or decimal_value(row.get("net_pnl", row.get("pnl"))) >= 0:
            continue
        if wanted_contract and str(row.get("inst") or row.get("contract") or "").upper() != wanted_contract:
            continue
        try:
            latest_loss = max(latest_loss, datetime.strptime(str(row.get("close_time")), "%Y-%m-%d %H:%M:%S").timestamp())
        except (TypeError, ValueError):
            continue
    remaining = max(0, int(latest_loss + cooldown_seconds - now)) if latest_loss else 0
    return {"active": remaining > 0, "remaining_seconds": remaining, "reason": "post_stop_cooldown" if remaining else "ok", **({"contract": wanted_contract} if wanted_contract else {})}


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
