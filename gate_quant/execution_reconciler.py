from __future__ import annotations

import hashlib
import json
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .client import GateFuturesClient
from .config import GateSettings, load_settings
from .execution_journal import ExecutionJournal
from .exchange_write_lock import gate_write_lock
from .risk import RiskLimits
from .service import GateTradingService, protection_coverage_status


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
JOURNAL_PATH = DATA / "gate_execution.db"
HEARTBEAT_PATH = DATA / "gate_execution_reconciler.json"
load_dotenv(ROOT / ".env", override=True)


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _order_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    nested = value.get("orders") or value.get("order")
    return nested if isinstance(nested, dict) else value


def _order_open(order: dict[str, Any]) -> bool:
    return str(order.get("status") or "").lower() == "open" and _decimal(order.get("left")) != 0


def _filled_size(order: dict[str, Any], requested_size: Decimal) -> Decimal:
    order_size = abs(_decimal(order.get("size"))) or abs(requested_size)
    remaining = abs(_decimal(order.get("left")))
    return max(Decimal("0"), min(abs(requested_size), order_size - remaining))


def _protection_id(intent: dict[str, Any], kind: str, revision: int) -> str:
    digest = hashlib.sha256(str(intent["client_id"]).encode("utf-8")).hexdigest()[:8]
    tag = "rsl" if kind == "stop_loss" else "rtp"
    return f"t-gate-{tag}-{digest}-{revision:02d}"


def _protection_matches(rows: list[dict], client_id: str, size: Decimal, rule: int) -> bool:
    expected_side = "long" if size < 0 else "short"
    for row in rows or []:
        initial = row.get("initial") or {}
        trigger = row.get("trigger") or {}
        full_close = (
            str(initial.get("auto_size") or "").lower() == f"close_{expected_side}"
            or str(row.get("order_type") or "").lower() == f"close-{expected_side}-position"
        )
        if (
            str(initial.get("text") or "") == client_id
            and (_decimal(initial.get("size")) == size or full_close)
            and int(trigger.get("rule") or 0) == rule
            and str(row.get("status") or "open").lower() == "open"
        ):
            return True
    return False


def _lookup_entry(client: GateFuturesClient, intent: dict[str, Any]) -> dict[str, Any] | None:
    order_id = str(intent.get("order_id") or "")
    if order_id:
        try:
            return _order_payload(client.order(order_id, str(intent["contract"])))
        except RuntimeError:
            pass
    found = client.find_by_client_id(str(intent["client_id"]), str(intent["contract"]))
    return _order_payload(found) if found else None


def _lookup_trigger_entry(client: GateFuturesClient, intent: dict[str, Any]) -> dict[str, Any] | None:
    order_id = str(intent.get("order_id") or "")
    if order_id:
        try:
            value = client.price_order(order_id)
            if isinstance(value, dict):
                return value
        except RuntimeError:
            pass
    found = client.find_trigger_entry_by_client_id(str(intent["client_id"]), str(intent["contract"]))
    return found if isinstance(found, dict) else None


def _rounded_decimal(value: Decimal, tick: Any) -> Decimal:
    quantum = _decimal(tick)
    if quantum <= 0:
        return value
    return (value / quantum).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * quantum


def _cancel_remainder(service: GateTradingService, intent: dict[str, Any], order: dict[str, Any]) -> dict[str, Any] | None:
    if not _order_open(order):
        return None
    order_id = str(order.get("id") or intent.get("order_id") or "")
    if not order_id:
        raise RuntimeError("open Gate entry has no exchange order id")
    return service.cancel_order_confirmed(contract=str(intent["contract"]), order_id=order_id)


def _position_for_contract(client: GateFuturesClient, contract: str, requested_size: Decimal) -> dict[str, Any]:
    try:
        value = client.positions(contract) or {}
    except RuntimeError as exc:
        if "POSITION_NOT_FOUND" in str(exc):
            return {}
        raise
    if isinstance(value, list):
        rows = [
            row for row in value
            if isinstance(row, dict) and str(row.get("contract") or "").upper() == contract
        ]
        expected_mode = "dual_long" if requested_size > 0 else "dual_short"
        directional = next(
            (row for row in rows if str(row.get("mode") or "").lower() == expected_mode),
            None,
        )
        if directional is not None:
            return directional
        same_sign = next(
            (row for row in rows if _decimal(row.get("size")) * requested_size > 0),
            None,
        )
        if same_sign is not None:
            return same_sign
        return next((row for row in rows if _decimal(row.get("size")) != 0), rows[0] if rows else {})
    return value if isinstance(value, dict) else {}


def reconcile_intent(
    client: GateFuturesClient,
    journal: ExecutionJournal,
    intent: dict[str, Any],
    settings: GateSettings,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    with gate_write_lock():
        return _reconcile_intent_unlocked(client, journal, intent, settings, now_ms=now_ms)


def _reconcile_intent_unlocked(
    client: GateFuturesClient,
    journal: ExecutionJournal,
    intent: dict[str, Any],
    settings: GateSettings,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    now_ms = now_ms or int(time.time() * 1000)
    client_id = str(intent["client_id"])
    contract = str(intent["contract"]).upper()
    requested = _decimal(intent["requested_size"])
    if str(intent.get("environment") or "").lower() != settings.environment:
        return journal.update(client_id, "manual_review", last_error="execution environment changed")

    if str(intent.get("order_type") or "") == "breakout":
        trigger_order = _lookup_trigger_entry(client, intent)
        if not trigger_order:
            age_ms = now_ms - int(intent.get("created_at_ms") or now_ms)
            if age_ms >= 60_000:
                return journal.update(client_id, "manual_review", last_error="Gate breakout order is not visible; it was not retried")
            return journal.update(client_id, "awaiting_trigger", last_error="Gate breakout order is not visible yet")
        trigger_id = str(trigger_order.get("id_string") or trigger_order.get("id") or intent.get("order_id") or "")
        trigger_status = str(trigger_order.get("status") or "unknown").lower()
        finish_as = str(trigger_order.get("finish_as") or "").lower()
        if trigger_status == "open":
            age_ms = now_ms - int(intent.get("created_at_ms") or now_ms)
            local_expiration_ms = int(_decimal(intent.get("expiration_seconds")) or settings.breakout_expiration_seconds) * 1000
            if age_ms >= local_expiration_ms:
                service = GateTradingService(
                    client,
                    RiskLimits(settings.max_position_notional_usd, settings.max_total_margin_usd, settings.max_order_margin_usd),
                )
                cancelled = service.cancel_trigger_entry_confirmed(contract=contract, order_id=trigger_id)
                return journal.update(client_id, "trigger_expired", order_id=trigger_id,
                                      order_status="locally_expired_cancelled", last_error="") | {"cancel": cancelled}
            return journal.update(client_id, "awaiting_trigger", order_id=trigger_id,
                                  order_status="waiting_trigger", last_error="")
        trade_id = str(trigger_order.get("trade_id") or "")
        if finish_as != "succeeded" or not trade_id:
            terminal = {
                "expired": "trigger_expired",
                "cancelled": "trigger_cancelled",
                "failed": "trigger_failed",
            }.get(finish_as)
            if terminal:
                return journal.update(client_id, terminal, order_id=trigger_id,
                                      order_status=finish_as, last_error=str(trigger_order.get("reason") or ""))
            return journal.update(client_id, "triggered_order_pending", order_id=trigger_id,
                                  order_status=finish_as or trigger_status,
                                  last_error="Gate breakout trigger finished without a visible child trade id")
        try:
            order = _order_payload(client.order(trade_id, contract))
        except RuntimeError as exc:
            return journal.update(client_id, "triggered_order_pending", order_id=trigger_id,
                                  order_status="triggered", last_error=f"triggered Gate entry is not visible yet: {exc}")
    else:
        order = _lookup_entry(client, intent)
    if not order:
        age_ms = now_ms - int(intent.get("created_at_ms") or now_ms)
        if str(intent.get("status")) == "prepared" and age_ms >= 60_000:
            return journal.update(client_id, "abandoned_unconfirmed", last_error="no Gate order found; entry was not retried")
        return journal.update(client_id, last_error="Gate entry not visible yet")

    order_id = str(order.get("id") or intent.get("order_id") or "")
    order_status = str(order.get("status") or "unknown")
    filled = _filled_size(order, requested)
    position = _position_for_contract(client, contract, requested)
    position_size = _decimal(position.get("size"))
    baseline = _decimal(intent.get("baseline_position_size"))
    signed_delta = position_size - baseline
    if signed_delta * requested > 0:
        filled = max(filled, min(abs(requested), abs(signed_delta)))

    base_update = {
        "order_id": order_id,
        "order_status": order_status,
        "filled_size": str(filled),
    }
    if filled == 0:
        terminal = not _order_open(order)
        return journal.update(client_id, "unfilled" if terminal else "pending_fill", last_error="", **base_update)

    service = GateTradingService(
        client,
        RiskLimits(settings.max_position_notional_usd, settings.max_total_margin_usd, settings.max_order_margin_usd),
    )
    if position_size == 0 or position_size * requested <= 0:
        _cancel_remainder(service, intent, order)
        age_ms = now_ms - int(intent.get("created_at_ms") or now_ms)
        status = "position_pending" if age_ms < 60_000 else "manual_review"
        return journal.update(
            client_id,
            status,
            last_error="filled Gate entry is waiting for a matching position" if status == "position_pending" else "filled Gate entry has no matching position after 60 seconds",
            **base_update,
        )

    protections = client.protection_orders(contract) or []
    coverage = protection_coverage_status(protections, position_size)
    close_sign = Decimal("-1") if position_size > 0 else Decimal("1")
    mark_price = _decimal(position.get("mark_price"))
    stop_price = _decimal(intent["stop_loss_price"])
    take_profit_price = _decimal(intent["take_profit_price"])
    fill_price = _decimal(order.get("fill_price") or order.get("avg_deal_price"))
    if fill_price <= 0:
        fill_price = _decimal(position.get("entry_price"))
    tp_distance = _decimal(intent.get("take_profit_distance_pct"))
    sl_distance = _decimal(intent.get("stop_loss_distance_pct"))
    if fill_price > 0 and tp_distance and sl_distance:
        take_profit_price = _rounded_decimal(fill_price * (Decimal("1") + tp_distance), intent.get("price_tick"))
        stop_price = _rounded_decimal(fill_price * (Decimal("1") + sl_distance), intent.get("price_tick"))
        intent = journal.update(
            client_id,
            entry_price=str(fill_price),
            fill_price=str(fill_price),
            take_profit_price=str(take_profit_price),
            stop_loss_price=str(stop_price),
            **base_update,
        )
        base_update["fill_price"] = str(fill_price)
    if tp_distance and sl_distance:
        valid_geometry = (
            mark_price > 0 and fill_price > 0 and stop_price > 0 and take_profit_price > 0
            and ((position_size > 0 and stop_price < min(mark_price, fill_price) and take_profit_price > max(mark_price, fill_price))
                 or (position_size < 0 and take_profit_price < min(mark_price, fill_price) and stop_price > max(mark_price, fill_price)))
        )
    else:
        # Backward-compatible recovery for intents created before fill-relative
        # distances were introduced. New intents always take the strict branch.
        valid_geometry = (
            mark_price > 0 and stop_price > 0 and take_profit_price > 0
            and ((position_size > 0 and stop_price < mark_price < take_profit_price)
                 or (position_size < 0 and take_profit_price < mark_price < stop_price))
        )
    if not valid_geometry:
        _cancel_remainder(service, intent, order)
        reduce_size = close_sign * min(filled, abs(signed_delta))
        if reduce_size == 0:
            return journal.update(client_id, "flattened_invalid_protection", last_error="saved protection geometry is no longer valid; new fill was already absent", **base_update)
        flattened = service.reduce_position_safely(
            contract=contract,
            size=reduce_size,
            client_id=f"t-gate-rclose-{hashlib.sha256(client_id.encode()).hexdigest()[:8]}",
        )
        return journal.update(client_id, "flattened_invalid_protection", last_error="saved protection geometry is no longer valid", **base_update) | {"flatten": flattened}

    stop_missing = max(Decimal("0"), coverage["required"] - coverage["stop_loss"])
    if stop_missing:
        revision = int(intent.get("stop_revision") or 0)
        protection_client_id = _protection_id(intent, "stop_loss", revision)
        stop_size = close_sign * stop_missing
        try:
            existing = client.find_protection_by_client_id(protection_client_id, contract)
            if not existing:
                client.create_protection_order(
                    contract=contract,
                    size=stop_size,
                    trigger_price=str(stop_price),
                    rule=2 if position_size > 0 else 1,
                    client_id=protection_client_id,
                )
            protections = client.protection_orders(contract) or []
            if not _protection_matches(protections, protection_client_id, stop_size, 2 if position_size > 0 else 1):
                raise RuntimeError("Gate stop-loss creation was not confirmed")
            intent = journal.update(client_id, stop_revision=revision + 1)
        except Exception as exc:
            cleanup: dict[str, Any] = {}
            try:
                cleanup["cancel_entry"] = _cancel_remainder(service, intent, order)
            except Exception as cleanup_exc:
                cleanup["cancel_entry_error"] = str(cleanup_exc)
            try:
                reduce_size = close_sign * min(filled, abs(signed_delta))
                if reduce_size:
                    cleanup["reduce_fill"] = service.reduce_position_safely(
                        contract=contract,
                        size=reduce_size,
                        client_id=f"t-gate-rclose-{hashlib.sha256(client_id.encode()).hexdigest()[:8]}",
                    )
            except Exception as cleanup_exc:
                cleanup["reduce_fill_error"] = str(cleanup_exc)
            updated = journal.update(client_id, "stop_failed_flatten_attempted", last_error=str(exc), **base_update)
            return {**updated, "cleanup": cleanup}

    protections = client.protection_orders(contract) or []
    coverage = protection_coverage_status(protections, position_size)
    take_profit_missing = max(Decimal("0"), coverage["required"] - coverage["take_profit"])
    if take_profit_missing:
        revision = int(intent.get("take_profit_revision") or 0)
        protection_client_id = _protection_id(intent, "take_profit", revision)
        take_profit_size = close_sign * take_profit_missing
        try:
            existing = client.find_protection_by_client_id(protection_client_id, contract)
            if not existing:
                client.create_protection_order(
                    contract=contract,
                    size=take_profit_size,
                    trigger_price=str(take_profit_price),
                    rule=1 if position_size > 0 else 2,
                    client_id=protection_client_id,
                )
            protections = client.protection_orders(contract) or []
            if not _protection_matches(protections, protection_client_id, take_profit_size, 1 if position_size > 0 else 2):
                raise RuntimeError("Gate take-profit creation was not confirmed")
            intent = journal.update(client_id, take_profit_revision=revision + 1)
        except Exception as exc:
            cleanup: dict[str, Any] = {}
            try:
                cleanup["cancel_entry"] = _cancel_remainder(service, intent, order)
            except Exception as cleanup_exc:
                cleanup["cancel_entry_error"] = str(cleanup_exc)
            stop_revision = int(intent.get("stop_revision") or 0)
            stop_client_id = _protection_id(intent, "stop_loss", max(0, stop_revision - 1))
            try:
                created_stop = client.find_protection_by_client_id(stop_client_id, contract)
                if created_stop:
                    stop_order_id = str(created_stop.get("id_string") or created_stop.get("id") or "")
                    if not stop_order_id:
                        raise RuntimeError("new stop-loss has no Gate order id")
                    cleanup["cancel_new_stop"] = service.cancel_protection_confirmed(order_id=stop_order_id)
            except Exception as cleanup_exc:
                cleanup["cancel_new_stop_error"] = str(cleanup_exc)
                updated = journal.update(
                    client_id,
                    "stop_protected_tp_pending",
                    last_error=f"take-profit failed and new stop cancellation is unconfirmed: {exc}; {cleanup_exc}",
                    **base_update,
                )
                return {**updated, "cleanup": cleanup}
            try:
                reduce_size = close_sign * min(filled, abs(signed_delta))
                if reduce_size:
                    cleanup["reduce_fill"] = service.reduce_position_safely(
                        contract=contract,
                        size=reduce_size,
                        client_id=f"t-gate-rclose-{hashlib.sha256(client_id.encode()).hexdigest()[:8]}",
                    )
            except Exception as cleanup_exc:
                cleanup["reduce_fill_error"] = str(cleanup_exc)
            updated = journal.update(client_id, "take_profit_failed_flatten_attempted", last_error=str(exc), **base_update)
            return {**updated, "cleanup": cleanup}

    protections = client.protection_orders(contract) or []
    coverage = protection_coverage_status(protections, position_size)
    protected = min(coverage["take_profit"], coverage["stop_loss"])
    fully_protected = bool(coverage["fully_protected"])
    if not fully_protected:
        return journal.update(client_id, "protection_pending", protected_size=str(protected), last_error="", **base_update)
    status = "partially_filled" if _order_open(order) else "filled_protected"
    return journal.update(client_id, status, protected_size=str(protected), last_error="", **base_update)


def run_once() -> dict[str, Any]:
    settings = load_settings()
    journal = ExecutionJournal(JOURNAL_PATH)
    intents = journal.active(settings.environment)
    now_ms = int(time.time() * 1000)
    intents = [
        intent for intent in intents
        if str(intent.get("status") or "") != "manual_review"
        or now_ms - int(intent.get("updated_at_ms") or 0) >= 300_000
    ]
    if not intents:
        return {"status": "idle", "environment": settings.environment, "processed": 0}
    if not settings.api_key or not settings.api_secret:
        return {"status": "blocked", "environment": settings.environment, "processed": 0, "error": "credentials unavailable"}
    client = GateFuturesClient(settings)
    results = []
    for intent in intents:
        try:
            results.append(reconcile_intent(client, journal, intent, settings))
        except Exception as exc:
            journal.update(str(intent["client_id"]), last_error=f"{type(exc).__name__}: {exc}")
            results.append({"client_id": intent["client_id"], "status": "error", "error": str(exc)})
    return {"status": "ok", "environment": settings.environment, "processed": len(results), "results": results}


def main() -> None:
    result = run_once()
    HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    HEARTBEAT_PATH.write_text(
        json.dumps({**result, "timestamp_ms": int(time.time() * 1000)}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(json.dumps({"status": result["status"], "processed": result["processed"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
