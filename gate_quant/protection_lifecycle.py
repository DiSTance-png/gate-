from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from .safety import atomic_json
from .service import protection_coverage_status


SYSTEM_PROTECTION_PREFIXES = ("t-gate-tp-", "t-gate-sl-", "t-gate-rtp-", "t-gate-rsl-")


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (TypeError, ValueError):
        return Decimal("0")


def _order_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    nested = value.get("order")
    return nested if isinstance(nested, dict) else value


def _trigger_price(value: Any) -> str:
    trigger = _order_payload(value).get("trigger") or {}
    return str(trigger.get("price") or "")


def intent_from_history(row: dict[str, Any]) -> dict[str, Any] | None:
    trade = row.get("trade") or {}
    if str(trade.get("status") or "") not in {"submitted_testnet", "submitted_live"}:
        return None
    contract = str(trade.get("contract") or "").upper()
    size = _decimal(trade.get("size"))
    tp_price = _trigger_price(trade.get("take_profit"))
    sl_price = _trigger_price(trade.get("stop_loss"))
    if not contract or size == 0 or _decimal(tp_price) <= 0 or _decimal(sl_price) <= 0:
        return None
    return {
        "environment": str(row.get("environment") or trade.get("environment") or "").lower(),
        "contract": contract,
        "entry_client_id": str(trade.get("client_id") or ""),
        "position_side": "long" if size > 0 else "short",
        "entry_size": str(abs(size)),
        "entry_price": str(trade.get("price") or ""),
        "take_profit_price": tp_price,
        "stop_loss_price": sl_price,
        "created_at_ms": int(row.get("generated_at_ms") or 0),
        "policy_version": str(trade.get("policy_version") or row.get("policy_version") or "gate@unknown"),
        "policy_hash": str(trade.get("policy_hash") or row.get("policy_hash") or "unknown"),
    }


def _history_rows(audit_path: Path, history_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if audit_path.exists():
        try:
            for line in audit_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    value = json.loads(line)
                    if isinstance(value, dict):
                        rows.append(value)
        except (OSError, json.JSONDecodeError):
            pass
    if history_path.exists():
        try:
            values = json.loads(history_path.read_text(encoding="utf-8"))
            if isinstance(values, list):
                rows.extend(row for row in values if isinstance(row, dict))
        except (OSError, json.JSONDecodeError):
            pass
    return rows


def load_protection_intents(path: Path, audit_path: Path, history_path: Path, environment: str) -> list[dict[str, Any]]:
    try:
        saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        if not isinstance(saved, list):
            saved = []
    except (OSError, json.JSONDecodeError):
        saved = []
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in saved:
        if isinstance(item, dict):
            key = (str(item.get("environment") or ""), str(item.get("contract") or ""), str(item.get("entry_client_id") or ""))
            merged[key] = item
    for row in _history_rows(audit_path, history_path):
        item = intent_from_history(row)
        if item:
            key = (item["environment"], item["contract"], item["entry_client_id"])
            merged[key] = item
    values = sorted(merged.values(), key=lambda item: int(item.get("created_at_ms") or 0))[-2000:]
    if values != saved:
        atomic_json(path, values)
    return [item for item in values if str(item.get("environment") or "").lower() == environment.lower()]


def record_protection_intent(path: Path, intent: dict[str, Any]) -> None:
    try:
        values = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        if not isinstance(values, list):
            values = []
    except (OSError, json.JSONDecodeError):
        values = []
    key = (str(intent.get("environment") or ""), str(intent.get("contract") or ""), str(intent.get("entry_client_id") or ""))
    values = [item for item in values if not isinstance(item, dict) or (str(item.get("environment") or ""), str(item.get("contract") or ""), str(item.get("entry_client_id") or "")) != key]
    values.append(intent)
    atomic_json(path, values[-2000:])


def is_system_protection(order: dict[str, Any]) -> bool:
    text_value = str((order.get("initial") or {}).get("text") or "")
    return text_value.startswith(SYSTEM_PROTECTION_PREFIXES)


def recovery_plans(positions: list[dict], protections: list[dict], intents: list[dict]) -> list[dict[str, Any]]:
    plans: list[dict[str, Any]] = []
    for position in positions:
        size = _decimal(position.get("size"))
        if size == 0:
            continue
        contract = str(position.get("contract") or "").upper()
        side = "long" if size > 0 else "short"
        open_time_ms = int(float(position.get("open_time") or 0) * 1000)
        rows = [row for row in protections if str(((row.get("initial") or {}).get("contract") or row.get("contract") or "")).upper() == contract]
        coverage = protection_coverage_status(rows, size)
        candidates = [
            item for item in intents
            if str(item.get("contract") or "").upper() == contract
            and item.get("position_side") == side
            and open_time_ms > 0
            and int(item.get("created_at_ms") or 0) >= open_time_ms - 60 * 60 * 1000
        ]
        source = max(candidates, key=lambda item: int(item.get("created_at_ms") or 0), default=None)
        mark_price = _decimal(position.get("mark_price"))
        for kind, covered, rule in (
            ("take_profit", coverage["take_profit"], 1 if size > 0 else 2),
            ("stop_loss", coverage["stop_loss"], 2 if size > 0 else 1),
        ):
            missing = max(Decimal("0"), coverage["required"] - covered)
            if missing == 0:
                continue
            price = _decimal((source or {}).get(f"{kind}_price"))
            trigger_met = bool(source and mark_price > 0 and price > 0 and ((rule == 1 and mark_price >= price) or (rule == 2 and mark_price <= price)))
            valid_side = mark_price > 0 and price > 0 and not trigger_met
            plans.append({
                "contract": contract,
                "kind": kind,
                "missing_size": str(missing),
                "close_size": str(-missing if size > 0 else missing),
                "trigger_price": str(price) if price > 0 else "",
                "rule": rule,
                "recoverable": bool(source and valid_side),
                "close_required": trigger_met,
                "reason": "" if source and valid_side else ("no_matching_current_position_intent" if not source else ("saved_trigger_already_met" if trigger_met else "market_price_unavailable")),
                "source_entry_client_id": str((source or {}).get("entry_client_id") or ""),
            })
    return plans
