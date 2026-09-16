from __future__ import annotations

import json
import os
import time
import re
import requests
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path
from dataclasses import replace
from typing import Any
from dotenv import load_dotenv

from .client import AmbiguousOrderError, GateFuturesClient, PRICE_ORDER_EXPIRATION_QUANTUM_SECONDS
from .config import load_settings
from .execution_journal import ExecutionJournal
from .execution_reconciler import JOURNAL_PATH as EXECUTION_JOURNAL, reconcile_intent
from .risk import RiskLimits
from .service import GateTradingService, protection_coverage_status
from .risk_profiles import get_risk_profile
from .safety import atomic_json, classify_error, cooldown_state, daily_loss_state, position_age_seconds, reconcile_exchange_state
from .protection_lifecycle import is_system_protection, load_protection_intents, record_protection_intent, recovery_plans

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=True)
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
DECISIONS = DATA / "ai_brain_decisions.json"
DECISION_HISTORY = DATA / "ai_decision_history.json"
DECISION_AUDIT = DATA / "ai_decision_audit.jsonl"
SAFETY_STATUS = DATA / "gate_safety_status.json"
RUNTIME_HEARTBEAT = DATA / "gate_trader_heartbeat.json"
LEDGER = DATA / "trading_ledger.json"
PROTECTION_INTENTS = DATA / "protection_intents.json"
PENDING_REQUOTES = DATA / "pending_requotes.json"
PENDING_REQUOTE_TTL_SECONDS = 20 * 60

TRADE_STATUS_LABELS = {
    "blocked_pending_reconfirmation": "等待下一轮确认",
    "blocked_price_deviation": "报价偏差过大，未下单",
    "blocked_extreme_deviation": "价格波动过大，未下单",
    "blocked_by_strategy_interceptor": "被策略风控拦截",
    "blocked_safety_fail_closed": "安全门关闭，未下单",
    "blocked_cycle_entry_limit": "达到单轮开仓上限，本轮未提交",
    "blocked_entry_intent_mismatch": "入场意图与盘口矛盾，未下单",
    "blocked_invalid_trigger_size": "突破计划张数无效，未下单",
    "blocked_invalid_protection_geometry": "止盈止损方向无效，未下单",
    "existing_breakout_plan_kept": "保留现有突破计划单",
    "breakout_cancelled_by_new_signal": "新信号已撤销突破计划单",
    "breakout_triggered_reconciliation_pending": "突破已触发，等待成交保护对账",
    "breakout_cancel_ambiguous": "突破计划撤单状态不明，安全暂停",
}


def _save_decision_payload(payload: dict, decisions_payload: dict, trade: dict, now: int, environment: str, risk_snapshot: dict | None = None) -> None:
    risk_snapshot = risk_snapshot or {}
    trade.setdefault("policy_version", risk_snapshot.get("policy_version", "gate@unknown"))
    trade.setdefault("policy_hash", risk_snapshot.get("policy_hash", "unknown"))
    trade.setdefault("status_label", TRADE_STATUS_LABELS.get(str(trade.get("status") or ""), str(trade.get("status") or "未下单")))
    DECISIONS.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    history_item = {
        "generated_at_ms": now,
        "environment": environment,
        "trade": trade,
        "trades": payload.get("trades") or [trade],
        "policy_version": risk_snapshot.get("policy_version", "gate@unknown"),
        "policy_hash": risk_snapshot.get("policy_hash", "unknown"),
        "risk_snapshot": risk_snapshot,
        "decisions": {
            symbol: {
                "action": envelope["decision"].get("action", "WAIT"),
                "raw_action": envelope["decision"].get("raw_action", envelope["decision"].get("action", "WAIT")),
                "confidence": envelope["decision"].get("confidence", 0),
                "entry_intent": envelope["decision"].get("entry_intent", ""),
                "entry_price": envelope["decision"].get("entry_price", 0),
                "take_profit_price": envelope["decision"].get("take_profit_price", 0),
                "stop_loss_price": envelope["decision"].get("stop_loss_price", 0),
                "summary_reason": envelope["decision"].get("summary_reason", ""),
                "rejection_reason": envelope["decision"].get("rejection_reason", ""),
            }
            for symbol, envelope in decisions_payload.items()
        },
    }
    try:
        history = json.loads(DECISION_HISTORY.read_text(encoding="utf-8")) if DECISION_HISTORY.exists() else []
        if not isinstance(history, list):
            history = []
    except (OSError, json.JSONDecodeError):
        history = []
    history.append(history_item)
    DECISION_HISTORY.write_text(json.dumps(history[-200:], ensure_ascii=False, indent=2), encoding="utf-8")
    with DECISION_AUDIT.open("a", encoding="utf-8") as audit:
        audit.write(json.dumps(history_item, ensure_ascii=False, separators=(",", ":")) + "\n")


def _console_summary(result: dict) -> str:
    try:
        payload = json.loads(DECISIONS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    positions = (payload.get("private_context") or {}).get("positions") or []
    active = [position for position in positions if float(position.get("size") or 0) != 0]
    long_count = sum(float(position.get("size") or 0) > 0 for position in active)
    short_count = sum(float(position.get("size") or 0) < 0 for position in active)
    trade = payload.get("trade") or result.get("trade") or {}
    status = str(trade.get("status") or "not_submitted")
    if status in {"submitted_testnet", "submitted_live"}:
        venue = "测试网" if status == "submitted_testnet" else "实盘"
        action_text = f"[{trade.get('contract', '--')}] 已提交{venue}订单"
    elif status == "blocked_by_strategy_interceptor":
        action_text = f"[{trade.get('contract', '--')}] 信号被策略拦截"
    elif status == "protection_failed_flatten_attempted":
        action_text = f"[{trade.get('contract', '--')}] 保护单失败，已尝试平仓"
    elif status == "blocked_pending_reconfirmation":
        action_text = f"[{trade.get('contract', '--')}] 等待下一轮确认"
    elif status == "blocked_price_deviation":
        action_text = f"[{trade.get('contract', '--')}] 报价偏差过大，未下单"
    else:
        action_text = "无开平仓操作"
    action_labels = {"BUY_LONG": "做多", "SELL_SHORT": "做空", "WAIT": "观望"}
    decision_parts = []
    for symbol, envelope in (result.get("decisions") or {}).items():
        decision = envelope.get("decision") or {}
        short_symbol = str(symbol).replace("_USDT", "")
        action = action_labels.get(str(decision.get("action") or "WAIT"), "未知")
        confidence = float(decision.get("confidence") or 0)
        decision_parts.append(f"{short_symbol}:{action}({confidence:.0f}%)")
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    headline = f"[{stamp}] Gate Quantum Trader v0.2.0 巡检完成 | 持仓 {len(active)} (多{long_count}/空{short_count}) | 动作: {action_text}"
    trades = result.get("trades") or payload.get("trades") or []
    if len(trades) > 1:
        status_parts = [f"{row.get('contract', '--')}:{TRADE_STATUS_LABELS.get(str(row.get('status') or ''), row.get('status') or '未下单')}" for row in trades]
        headline += "\n候选执行 | " + " | ".join(status_parts)
    return headline + "\nAI决策 | " + " | ".join(decision_parts)
SYMBOLS = ("BTC_USDT", "ETH_USDT", "SOL_USDT", "DOGE_USDT", "SUI_USDT", "XRP_USDT")

GATE_LIMIT_PRICE_DEVIATION = 0.02
# A stale AI quote may be repaired, but an extreme move is still rejected
# rather than chased.  This is deliberately wider than the normal execution
# band and can be tightened in a future risk profile.
GATE_MAX_REQUOTE_DEVIATION = 0.06
ENTRY_INTENTS = {"immediate", "retracement", "breakout"}


def _quote_deviation(entry_price: float, reference_price: float) -> float:
    if entry_price <= 0 or reference_price <= 0:
        return 0.0
    return abs(entry_price - reference_price) / reference_price


def _entry_execution_plan(*, action: str, entry_intent: str, entry_price: float,
                          quote: dict, max_slippage_pct: float, price_tick: str | float,
                          expiration_seconds: int) -> dict:
    """Validate explicit AI intent against the live book and map it to Gate semantics."""
    intent = str(entry_intent or "").lower()
    entry = float(entry_price or 0)
    bid = float(quote.get("highest_bid") or quote.get("bid") or 0)
    ask = float(quote.get("lowest_ask") or quote.get("ask") or 0)
    if action not in {"BUY_LONG", "SELL_SHORT"} or intent not in ENTRY_INTENTS:
        return {"valid": False, "reason": "入场意图缺失或不受支持"}
    if entry <= 0 or bid <= 0 or ask <= 0 or bid > ask:
        return {"valid": False, "reason": "Gate 买一/卖一盘口不可用，无法验证入场意图"}
    slip = max(0.0, float(max_slippage_pct))
    if intent == "immediate":
        reference = ask if action == "BUY_LONG" else bid
        if abs(entry - reference) / reference > slip:
            return {"valid": False, "reason": "立即入场报价与当前盘口偏差超过滑点上限"}
        capped = reference * (1 + slip if action == "BUY_LONG" else 1 - slip)
        return {"valid": True, "order_type": "immediate", "tif": "ioc",
                "price": _round_price(capped, price_tick, ROUND_FLOOR if action == "BUY_LONG" else ROUND_CEILING),
                "reference_price": reference,
                "max_slippage_pct": slip}
    if intent == "retracement":
        valid = entry < ask if action == "BUY_LONG" else entry > bid
        if not valid:
            return {"valid": False, "reason": "回调/反弹入场价与当前盘口方向矛盾"}
        return {"valid": True, "order_type": "retracement", "tif": "gtc",
                "price": _round_price(entry, price_tick, ROUND_FLOOR if action == "BUY_LONG" else ROUND_CEILING),
                "reference_price": ask if action == "BUY_LONG" else bid,
                "max_slippage_pct": 0.0}
    valid = entry > ask if action == "BUY_LONG" else entry < bid
    if not valid:
        return {"valid": False, "reason": "突破/跌破触发价与当前盘口方向矛盾"}
    capped = entry * (1 + slip if action == "BUY_LONG" else 1 - slip)
    return {"valid": True, "order_type": "breakout", "tif": "ioc",
            "price": _round_price(capped, price_tick, ROUND_FLOOR if action == "BUY_LONG" else ROUND_CEILING),
            "trigger_price": _round_price(entry, price_tick, ROUND_CEILING if action == "BUY_LONG" else ROUND_FLOOR),
            "trigger_rule": 1 if action == "BUY_LONG" else 2,
            "reference_price": ask if action == "BUY_LONG" else bid,
            "max_slippage_pct": slip, "expiration_seconds": int(expiration_seconds)}


def _breakout_compatible_size(size: Decimal, minimum: Any = 1) -> Decimal:
    """Gate price_orders documents an integer initial size; never round exposure up."""
    sign = Decimal("1") if size > 0 else Decimal("-1")
    integer_size = abs(size).to_integral_value(rounding=ROUND_DOWN)
    if integer_size < max(Decimal("1"), Decimal(str(minimum or 1))):
        raise ValueError("突破计划单按 Gate 整数张约束取整后低于最小下单量")
    return sign * integer_size


def _requote_decision(decision: dict, reference_price: float, *,
                      limit: float = GATE_LIMIT_PRICE_DEVIATION,
                      max_requote: float = GATE_MAX_REQUOTE_DEVIATION) -> tuple[dict, dict]:
    """Reprice a stale limit decision while preserving its planned distances.

    This is intentionally a limit-price repair, never a conversion to market.
    TP/SL offsets are carried from the AI's original entry when they are
    directionally valid; the caller must run the normal strategy/risk gates
    again after applying the returned decision.
    """
    original = dict(decision)
    entry = float(original.get("entry_price") or 0)
    reference = float(reference_price or 0)
    deviation = _quote_deviation(entry, reference)
    meta = {
        "original_entry_price": entry,
        "reference_price": reference,
        "original_deviation_pct": deviation * 100,
        "requote_limit_pct": limit * 100,
        "requote_max_pct": max_requote * 100,
        "requote_status": "unchanged",
    }
    if not entry or not reference or deviation <= limit:
        return original, meta
    if deviation > max_requote:
        meta["requote_status"] = "blocked_extreme_deviation"
        return original, meta

    action = str(original.get("action") or "WAIT")
    sign = 1 if action == "BUY_LONG" else -1 if action == "SELL_SHORT" else 0
    updated = dict(original)
    updated["entry_price"] = reference
    for field, favorable in (("take_profit_price", 1), ("stop_loss_price", -1)):
        planned = float(original.get(field) or 0)
        delta = planned - entry if planned and entry else 0.0
        # Long TP / short SL should be above entry; the inverse is below.
        # Invalid or missing AI levels are left at zero for the caller's
        # existing ATR fallback logic.
        valid = bool(sign and delta and ((delta * sign * favorable) > 0))
        updated[field] = reference + delta if valid else 0.0
    meta.update({
        "requote_status": "requoted",
        "repriced_entry_price": reference,
        "repriced_take_profit_price": updated["take_profit_price"],
        "repriced_stop_loss_price": updated["stop_loss_price"],
    })
    return updated, meta


def _load_pending_requotes(now_ms: int) -> dict[str, dict]:
    try:
        raw = json.loads(PENDING_REQUOTES.read_text(encoding="utf-8")) if PENDING_REQUOTES.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    cutoff = now_ms - PENDING_REQUOTE_TTL_SECONDS * 1000
    return {symbol: row for symbol, row in raw.items()
            if isinstance(row, dict) and int(row.get("created_at_ms") or 0) >= cutoff}


def _save_pending_requotes(rows: dict[str, dict]) -> None:
    atomic_json(PENDING_REQUOTES, rows)


def _confirm_pending_requotes(rows: dict[str, dict], decisions_payload: dict,
                              *, min_confidence: float, now_ms: int) -> set[str]:
    """Confirm deferred signals only when the next AI cycle agrees in direction."""
    confirmed: set[str] = set()
    for symbol, row in list(rows.items()):
        current = (decisions_payload.get(symbol) or {}).get("decision") or {}
        action = str(current.get("action") or "WAIT")
        same_direction = action == str(row.get("action") or "")
        confidence_ok = float(current.get("confidence") or 0) >= min_confidence
        if same_direction and confidence_ok:
            row["confirmed_at_ms"] = now_ms
            row["second_decision"] = dict(current)
            confirmed.add(symbol)
        else:
            # A contrary/WAIT/low-confidence second decision invalidates the
            # deferred entry signal instead of carrying it into later cycles.
            rows.pop(symbol, None)
    _save_pending_requotes(rows)
    return confirmed


def _preflight_candidate_quotes(client, candidates: list[dict], pending_requotes: dict[str, dict],
                                confirmed_requotes: set[str], *, now_ms: int,
                                entry_intent_enabled: bool = False) -> tuple[list[dict], list[dict]]:
    """Screen every candidate so one deferred quote cannot hide later signals."""
    eligible: list[dict] = []
    blocked: list[dict] = []
    ordered = sorted(candidates, key=lambda row: float(row["decision"].get("confidence") or 0), reverse=True)
    for envelope in ordered:
        symbol = str(envelope["instId"])
        item = envelope["decision"]
        quote_rows = client.tickers(symbol) or []
        quote = quote_rows[0] if isinstance(quote_rows, list) and quote_rows else (quote_rows if isinstance(quote_rows, dict) else {})
        reference = float(quote.get("mark_price") or quote.get("last") or envelope["indicators"].get("last") or 0)
        original = dict(item)
        if entry_intent_enabled and str(item.get("entry_intent") or "").lower() in ENTRY_INTENTS:
            envelope["quote_preflight"] = {
                "requote_status": "explicit_entry_intent",
                "original_entry_price": float(item.get("entry_price") or 0),
                "reference_price": reference,
            }
            eligible.append(envelope)
            continue
        _, requote = _requote_decision(item, reference)
        if symbol in confirmed_requotes or requote["requote_status"] == "unchanged":
            envelope["quote_preflight"] = requote
            eligible.append(envelope)
            continue
        if requote["requote_status"] == "requoted":
            pending_requotes[symbol] = {
                "contract": symbol, "action": original.get("action"),
                "confidence": float(original.get("confidence") or 0),
                "entry_price": requote["original_entry_price"],
                "take_profit_price": original.get("take_profit_price"),
                "stop_loss_price": original.get("stop_loss_price"),
                "created_at_ms": now_ms,
                "reason": "price_deviation_requires_next_cycle_confirmation",
            }
            reason = f"首次信号价格偏差 {requote['original_deviation_pct']:.3f}%；已保存，等待下一轮 AI 同方向确认"
            status = "blocked_pending_reconfirmation"
        else:
            reason = (f"Gate limit price deviation {requote['original_deviation_pct']:.3f}% exceeds "
                      f"safe re-quote ceiling {requote['requote_max_pct']:.1f}%; safe WAIT")
            status = "blocked_price_deviation"
        item.update({"action": "WAIT", "entry_price": 0.0, "take_profit_price": 0.0,
                     "stop_loss_price": 0.0, "rejection_reason": reason})
        blocked.append({"status": status, "contract": symbol, "reason": reason,
                        "requested_entry": requote["original_entry_price"],
                        "reference_price": requote["reference_price"],
                        "deviation_pct": requote["original_deviation_pct"], "requote": requote})
    _save_pending_requotes(pending_requotes)
    return eligible, blocked


def _account_committed_margin(account: dict) -> float:
    """Return Gate margin already committed by positions and open orders."""
    detailed_fields = (
        "cross_initial_margin",
        "cross_order_margin",
        "isolated_position_margin",
        "isolated_order_margin",
    )
    if any(field in account for field in detailed_fields):
        return sum(float(account.get(field) or 0) for field in detailed_fields)
    return float(account.get("position_margin") or 0) + float(account.get("order_margin") or 0)


def _portfolio_position_notional(client: GateFuturesClient, positions: list[dict]) -> float:
    """Calculate total open-position notional with each Gate contract multiplier."""
    total = Decimal("0")
    multiplier_cache: dict[str, Decimal] = {}
    for position in positions or []:
        size = Decimal(str(position.get("size") or 0))
        if size == 0:
            continue
        contract_name = str(position.get("contract") or "").upper()
        mark_price = Decimal(str(position.get("mark_price") or 0))
        raw_multiplier = position.get("quanto_multiplier")
        if raw_multiplier:
            multiplier = Decimal(str(raw_multiplier))
        else:
            if not contract_name:
                raise RuntimeError("Cannot enforce total notional limit: position contract is missing")
            if contract_name not in multiplier_cache:
                metadata = client.contracts(contract_name)
                multiplier_cache[contract_name] = Decimal(str(metadata.get("quanto_multiplier") or 0))
            multiplier = multiplier_cache[contract_name]
        if mark_price <= 0 or multiplier <= 0:
            raise RuntimeError(f"Cannot enforce total notional limit for {contract_name or 'unknown position'}")
        total += abs(size) * multiplier * mark_price
    return float(total)


def _untracked_trigger_entries(trigger_entries: list[dict], execution_intents: list[dict]) -> list[dict]:
    """Return non-reduce price orders that have no matching durable execution intent."""
    known_client_ids = {str(row.get("client_id") or "") for row in execution_intents}
    known_order_ids = {str(row.get("order_id") or "") for row in execution_intents if row.get("order_id")}
    untracked = []
    for row in trigger_entries or []:
        if not isinstance(row, dict):
            continue
        initial = row.get("initial") or {}
        client_id = str(initial.get("text") or "")
        order_id = str(row.get("id_string") or row.get("id") or "")
        if client_id not in known_client_ids and order_id not in known_order_ids:
            untracked.append({"order_id": order_id, "client_id": client_id,
                              "contract": str(initial.get("contract") or "").upper()})
    return untracked


def _execute_entry_candidate(client: GateFuturesClient, envelope: dict, *, settings, risk_profile,
                             risk_snapshot: dict, policy_snapshot: dict, confirmed_requotes: set[str],
                             now_ms: int, sequence: int, risk_state: dict[str, float]) -> dict:
    """Execute one candidate serially and return an isolated outcome.

    Candidate-level rejections do not stop later symbols. An ambiguous order or
    unresolved reconciliation does, because the true exchange exposure is then
    unknown and the cycle must fail closed.
    """
    trade_symbol = str(envelope["instId"])
    decision = envelope["decision"]
    trade_feature = envelope["indicators"]
    action = str(decision.get("action") or "WAIT")
    confidence = float(decision.get("confidence") or 0)
    cooldown = cooldown_state(LEDGER, cooldown_seconds=settings.stop_cooldown_seconds, contract=trade_symbol)
    if cooldown["active"]:
        return {"trade": {"status": "blocked_symbol_cooldown", "contract": trade_symbol,
                          "reason": f"{trade_symbol} is in post-stop cooldown; other contracts remain eligible",
                          "cooldown": cooldown}, "submitted": False, "stop_cycle": False}

    limits = RiskLimits(settings.max_position_notional_usd, settings.max_total_margin_usd, settings.max_order_margin_usd)
    contract = client.contracts(trade_symbol)
    multiplier = float(contract.get("quanto_multiplier") or 0)
    quote_rows = client.tickers(trade_symbol) or []
    quote = quote_rows[0] if isinstance(quote_rows, list) and quote_rows else (quote_rows if isinstance(quote_rows, dict) else {})
    gate_reference = float(quote.get("mark_price") or quote.get("last") or trade_feature.get("last") or 0)
    tick = contract.get("order_price_round") or contract.get("mark_price_round") or "0"
    if settings.entry_intent_enabled:
        entry_plan = _entry_execution_plan(
            action=action,
            entry_intent=str(decision.get("entry_intent") or ""),
            entry_price=float(decision.get("entry_price") or 0),
            quote=quote,
            max_slippage_pct=settings.max_entry_slippage_pct,
            price_tick=tick,
            expiration_seconds=settings.breakout_expiration_seconds,
        )
        requote = {"requote_status": "explicit_entry_intent", "reference_price": gate_reference}
        if not entry_plan.get("valid"):
            reason = str(entry_plan.get("reason") or "入场意图与盘口关系不一致")
            decision.update({"action": "WAIT", "entry_price": 0.0, "take_profit_price": 0.0,
                             "stop_loss_price": 0.0, "rejection_reason": reason})
            return {"trade": {"status": "blocked_entry_intent_mismatch", "contract": trade_symbol,
                              "reason": reason, "entry_intent": decision.get("entry_intent"),
                              "quote": {"bid": quote.get("highest_bid"), "ask": quote.get("lowest_ask")}},
                    "submitted": False, "stop_cycle": False}
    else:
        repriced_decision, requote = _requote_decision(decision, gate_reference)
        decision.update(repriced_decision)
        entry_plan = {"valid": True, "order_type": "limit", "tif": "gtc",
                      "price": _round_price(float(decision.get("entry_price") or 0), tick),
                      "max_slippage_pct": 0.0}
        if trade_symbol in confirmed_requotes:
            requote["requote_status"] = "confirmed_after_deferred_signal"
            requote["confirmed_at_ms"] = now_ms
        if requote["requote_status"] in {"blocked_extreme_deviation", "requoted"} and trade_symbol not in confirmed_requotes:
            status = "blocked_price_deviation" if requote["requote_status"] == "blocked_extreme_deviation" else "blocked_pending_reconfirmation"
            reason = (f"Gate limit price deviation {requote['original_deviation_pct']:.3f}% exceeds safe re-quote ceiling; safe WAIT"
                      if status == "blocked_price_deviation" else
                      f"首次信号价格偏差 {requote['original_deviation_pct']:.3f}%；等待下一轮 AI 同方向确认")
            decision.update({"action": "WAIT", "entry_price": 0.0, "take_profit_price": 0.0,
                             "stop_loss_price": 0.0, "rejection_reason": reason})
            return {"trade": {"status": status, "contract": trade_symbol, "reason": reason,
                              "requested_entry": requote["original_entry_price"], "reference_price": requote["reference_price"],
                              "deviation_pct": requote["original_deviation_pct"], "requote": requote},
                    "submitted": False, "stop_cycle": False}

    entry_price = float(decision.get("entry_price") or 0)
    planned_margin = float(decision.get("margin_usdt") or decision.get("margin_usd") or 0)
    leverage = settings.leverage
    if planned_margin <= 0:
        return {"trade": {"status": "blocked_invalid_margin", "contract": trade_symbol,
                          "reason": "AI did not provide a positive margin_usdt; no order was submitted"},
                "submitted": False, "stop_cycle": False}
    leverage_min = float(contract.get("leverage_min") or 1)
    leverage_max = float(contract.get("leverage_max") or 0)
    if leverage < leverage_min or (leverage_max > 0 and leverage > leverage_max):
        return {"trade": {"status": "blocked_invalid_leverage", "contract": trade_symbol,
                          "reason": f"Configured Gate leverage {leverage:g}x is outside contract range {leverage_min:g}x-{leverage_max:g}x"},
                "submitted": False, "stop_cycle": False}
    try:
        contracts = _order_size_for_margin(margin_usdt=planned_margin, leverage=leverage, entry_price=entry_price,
                                           multiplier=multiplier, minimum=contract.get("order_size_min") or 1,
                                           maximum=contract.get("order_size_max") or 0,
                                           enable_decimal=bool(contract.get("enable_decimal")))
    except ValueError as exc:
        return {"trade": {"status": "blocked_invalid_size", "contract": trade_symbol, "reason": str(exc),
                          "planned_margin_usdt": planned_margin, "configured_leverage": leverage},
                "submitted": False, "stop_cycle": False}
    size = contracts if action == "BUY_LONG" else -contracts
    if entry_plan["order_type"] == "breakout":
        try:
            size = _breakout_compatible_size(size, contract.get("order_size_min") or 1)
        except ValueError as exc:
            return {"trade": {"status": "blocked_invalid_trigger_size", "contract": trade_symbol,
                              "reason": str(exc), "requested_size": str(size)},
                    "submitted": False, "stop_cycle": False}
    risk_price = max(entry_price, float(entry_plan.get("price") or entry_price))
    order_notional = float(abs(size) * Decimal(str(multiplier)) * Decimal(str(risk_price)))
    estimated_margin = order_notional / leverage

    # Refresh exchange state for every candidate. The local reservation ensures
    # the previous order is counted even if Gate account fields lag briefly.
    account = client.account() or {}
    positions = client.positions() or []
    if isinstance(positions, dict):
        positions = [positions] if positions else []
    existing_position = next((p for p in positions if str(p.get("contract") or "").upper() == trade_symbol and float(p.get("size") or 0) != 0), None)
    existing_size = Decimal(str((existing_position or {}).get("size") or 0))
    is_add_on = existing_size != 0
    if is_add_on and ((existing_size > 0) != (size > 0)):
        return {"trade": {"status": "blocked_opposite_position", "contract": trade_symbol,
                          "reason": "Existing Gate position direction conflicts with the proposed add-on"},
                "submitted": False, "stop_cycle": False}
    if is_add_on:
        coverage = protection_coverage_status(client.protection_orders(trade_symbol) or [], existing_size)
        if not coverage["fully_protected"]:
            return {"trade": {"status": "blocked_add_unprotected_existing_position", "contract": trade_symbol,
                              "reason": "Existing Gate position is not fully covered by both native take-profit and stop-loss orders; add-on blocked fail-closed",
                              "required_size": GateFuturesClient._api_size(abs(existing_size)),
                              "take_profit_covered_size": GateFuturesClient._api_size(coverage["take_profit"]),
                              "stop_loss_covered_size": GateFuturesClient._api_size(coverage["stop_loss"])},
                    "submitted": False, "stop_cycle": False}

    current_margin = max(_account_committed_margin(account), risk_state["baseline_margin"] + risk_state["reserved_margin"])
    current_notional = max(_portfolio_position_notional(client, positions), risk_state["baseline_notional"] + risk_state["reserved_notional"])
    risk = {"order_margin_usd": estimated_margin, "current_margin_usd": current_margin,
            "current_position_notional_usd": current_notional, "order_notional_usd": order_notional,
            "environment": settings.environment, "live_enabled": settings.live_trading_enabled}
    try:
        limits.check_order(**risk)
    except (PermissionError, ValueError) as exc:
        return {"trade": {"status": "blocked_risk_limit", "contract": trade_symbol, "reason": str(exc), "risk": risk},
                "submitted": False, "stop_cycle": False}
    try:
        from .strategy_adapter import validate_decision
        active_positions = {str(p.get("contract")): p for p in positions if float(p.get("size") or 0) != 0}
        active_sides = {symbol: "long" if float(position.get("size") or 0) > 0 else "short" for symbol, position in active_positions.items()}
        package = {**trade_feature, "instId": trade_symbol, "data_quality": "valid", "macro_4h": trade_feature.get("macro_4h", "RANGE")}
        gated_action, gate_reason, rr = validate_decision(package, decision, active_inst_ids=set(active_positions), active_position_sides=active_sides, risk_snapshot=risk_snapshot)
        if gated_action != action:
            return {"trade": {"status": "blocked_by_strategy_interceptor", "contract": trade_symbol,
                              "reason": gate_reason or "Gate strategy interceptor rejected decision", "risk_reward": rr},
                    "submitted": False, "stop_cycle": False}
    except Exception as exc:
        return {"trade": {"status": "blocked_by_strategy_interceptor_error", "contract": trade_symbol, "reason": str(exc)},
                "submitted": False, "stop_cycle": False}

    client_id = f"t-gate-ai-{now_ms}-{sequence}"
    try:
        position_response = client.positions(trade_symbol) or {}
        if isinstance(position_response, list):
            position = next((row for row in position_response if isinstance(row, dict) and float(row.get("size") or 0) != 0), {})
        else:
            position = position_response if isinstance(position_response, dict) else {}
    except RuntimeError as exc:
        if "POSITION_NOT_FOUND" not in str(exc):
            raise
        position = {}
    cross_margin = str(position.get("pos_margin_mode") or "cross").lower() == "cross" or float(position.get("leverage") or 0) == 0
    limit_price = str(entry_plan["price"])
    tp = float(decision.get("take_profit_price") or (gate_reference + 2 * trade_feature["atr14"] if size > 0 else gate_reference - 2 * trade_feature["atr14"]))
    sl = float(decision.get("stop_loss_price") or (gate_reference - trade_feature["atr14"] if size > 0 else gate_reference + trade_feature["atr14"]))
    rounded_tp, rounded_sl = _round_price(tp, tick), _round_price(sl, tick)
    tp_distance_pct = (tp - entry_price) / entry_price if entry_price else 0.0
    sl_distance_pct = (sl - entry_price) / entry_price if entry_price else 0.0
    valid_distances = ((size > 0 and tp_distance_pct > 0 and sl_distance_pct < 0)
                       or (size < 0 and tp_distance_pct < 0 and sl_distance_pct > 0))
    if not valid_distances:
        return {"trade": {"status": "blocked_invalid_protection_geometry", "contract": trade_symbol,
                          "reason": "AI 止盈止损与入场方向不一致，未提交订单"},
                "submitted": False, "stop_cycle": False}
    journal = ExecutionJournal(EXECUTION_JOURNAL)
    intent = journal.prepare({"client_id": client_id, "environment": settings.environment, "contract": trade_symbol,
                              "requested_size": str(size), "baseline_position_size": str(existing_size),
                              "entry_price": limit_price, "take_profit_price": rounded_tp, "stop_loss_price": rounded_sl,
                              "take_profit_distance_pct": str(tp_distance_pct), "stop_loss_distance_pct": str(sl_distance_pct),
                              "order_type": entry_plan["order_type"], "entry_action": action,
                              "max_slippage_pct": str(entry_plan.get("max_slippage_pct") or 0),
                              "expiration_seconds": str(entry_plan.get("expiration_seconds") or settings.breakout_expiration_seconds),
                              "order_notional_usdt": str(order_notional), "estimated_margin_usdt": str(estimated_margin),
                              "price_tick": str(tick),
                              "requote": requote, "created_at_ms": now_ms,
                              "policy_version": policy_snapshot["policy_version"], "policy_hash": policy_snapshot["policy_hash"]})
    try:
        leverage_result = client.update_position_leverage(contract=trade_symbol, leverage=leverage, cross_margin=cross_margin)
        service = GateTradingService(client, limits)
        if entry_plan["order_type"] == "breakout":
            order = service.place_trigger_entry(
                contract=trade_symbol,
                size=size,
                trigger_price=str(entry_plan["trigger_price"]),
                execution_price=limit_price,
                rule=int(entry_plan["trigger_rule"]),
                client_id=client_id,
                expiration=PRICE_ORDER_EXPIRATION_QUANTUM_SECONDS,
                risk=risk,
            )
        else:
            order = service.place_order(contract=trade_symbol, size=size, price=limit_price,
                                        tif=str(entry_plan["tif"]), client_id=client_id, risk=risk)
    except AmbiguousOrderError as exc:
        journal.update(client_id, "prepared", last_error=str(exc))
        return {"trade": {"status": "order_submission_ambiguous", "contract": trade_symbol, "client_id": client_id,
                          "reason": f"Gate entry submission is ambiguous and will only be reconciled by client order id: {exc}"},
                "submitted": False, "stop_cycle": True}
    except Exception as exc:
        journal.update(client_id, "order_rejected", last_error=str(exc))
        reason = f"Gate entry order rejected safely: {exc}"
        decision.update({"action": "WAIT", "entry_price": 0.0, "take_profit_price": 0.0,
                         "stop_loss_price": 0.0, "rejection_reason": reason})
        return {"trade": {"status": "order_rejected_safe_wait", "contract": trade_symbol,
                          "client_id": client_id, "reason": reason}, "submitted": False, "stop_cycle": False}

    raw_order = ((order.get("orders") or order.get("order")) if isinstance(order, dict) and order.get("reconciled") else order)
    raw_order = raw_order if isinstance(raw_order, dict) else {}
    submitted_status = "awaiting_trigger" if entry_plan["order_type"] == "breakout" else "submitted"
    intent = journal.update(client_id, submitted_status, order_id=str(raw_order.get("id") or ""),
                            order_status=str(raw_order.get("status") or ("waiting_trigger" if submitted_status == "awaiting_trigger" else "unknown")))
    record_protection_intent(PROTECTION_INTENTS, {"environment": settings.environment, "contract": trade_symbol,
        "entry_client_id": client_id, "position_side": "long" if size > 0 else "short", "entry_size": str(abs(size)),
        "entry_price": limit_price, "requested_entry_price": entry_price, "take_profit_price": rounded_tp, "stop_loss_price": rounded_sl,
        "take_profit_distance_pct": tp_distance_pct, "stop_loss_distance_pct": sl_distance_pct,
        "entry_intent": decision.get("entry_intent"), "order_type": entry_plan["order_type"], "requote": requote,
        "created_at_ms": now_ms, "policy_version": policy_snapshot["policy_version"], "policy_hash": policy_snapshot["policy_hash"]})
    try:
        lifecycle = reconcile_intent(client, journal, intent, settings, now_ms=now_ms)
    except Exception as exc:
        journal.update(client_id, last_error=f"initial reconciliation: {exc}")
        lifecycle = {"status": "reconciliation_pending", "last_error": str(exc)}
    risk_state["reserved_margin"] += estimated_margin
    risk_state["reserved_notional"] += order_notional
    lifecycle_status = str(lifecycle.get("status") or "")
    protected_now = lifecycle_status in {"filled_protected", "partially_filled"}
    terminal_failure = lifecycle_status in {
        "stop_failed_flatten_attempted",
        "take_profit_failed_flatten_attempted",
        "flattened_invalid_protection",
    }
    trade = {"status": "protection_failed_flatten_attempted" if terminal_failure else f"submitted_{settings.environment}",
             "environment": settings.environment, "contract": trade_symbol, "client_id": client_id,
             "position_operation": "add" if is_add_on else "open", "order_type": entry_plan["order_type"], "price": limit_price,
             "entry_intent": decision.get("entry_intent"), "trigger_price": entry_plan.get("trigger_price"),
             "size": GateFuturesClient._api_size(size), "expected_position_size": GateFuturesClient._api_size(existing_size + size),
             "configured_leverage": leverage, "risk_snapshot": risk_snapshot, "estimated_margin_usdt": estimated_margin,
             "leverage_update": leverage_result, "order": order, "take_profit": {"planned_price": rounded_tp},
             "stop_loss": {"planned_price": rounded_sl}, "execution_lifecycle": lifecycle,
             "protection_covered": protected_now, "take_profit_covered": protected_now,
             "stop_loss_covered": protected_now or lifecycle_status == "stop_protected_tp_pending"}
    return {"trade": trade, "submitted": True,
            "stop_cycle": terminal_failure or lifecycle_status == "reconciliation_pending"}


def _run_serial_candidates(candidates: list[dict], max_entries: int, executor) -> tuple[list[dict], list[dict]]:
    """Run candidates until the submitted-entry budget is consumed or fail-closed."""
    outcomes: list[dict] = []
    submitted: list[dict] = []
    for sequence, candidate in enumerate(candidates, start=1):
        if len(submitted) >= max_entries:
            contract = str(candidate.get("instId") or candidate.get("contract") or "--") if isinstance(candidate, dict) else str(candidate)
            outcomes.append({"contract": contract, "status": "blocked_cycle_entry_limit",
                             "reason": f"Configured per-cycle entry limit {max_entries} was reached; signal was evaluated but not submitted"})
            continue
        outcome = executor(candidate, sequence)
        trade = outcome["trade"]
        outcomes.append(trade)
        if outcome["submitted"]:
            submitted.append(trade)
        if outcome["stop_cycle"]:
            break
    return outcomes, submitted


def _manage_breakout_plans(client: GateFuturesClient, journal: ExecutionJournal,
                           intents: list[dict], decisions_payload: dict, settings) -> tuple[set[str], list[dict], bool]:
    """Keep same-direction plans, or cancel them when the next AI decision withdraws the signal."""
    kept: set[str] = set()
    outcomes: list[dict] = []
    stop_cycle = False
    service = GateTradingService(
        client,
        RiskLimits(settings.max_position_notional_usd, settings.max_total_margin_usd, settings.max_order_margin_usd),
    )
    for intent in intents:
        if str(intent.get("order_type") or "") != "breakout" or str(intent.get("status") or "") != "awaiting_trigger":
            continue
        contract = str(intent.get("contract") or "").upper()
        decision = (decisions_payload.get(contract) or {}).get("decision") or {}
        same_plan = (
            str(decision.get("action") or "WAIT") == str(intent.get("entry_action") or "")
            and str(decision.get("entry_intent") or "").lower() == "breakout"
        )
        if same_plan:
            kept.add(contract)
            outcomes.append({"status": "existing_breakout_plan_kept", "contract": contract,
                             "client_id": intent.get("client_id"), "reason": "下一轮 AI 仍维持同方向突破意图"})
            continue
        order_id = str(intent.get("order_id") or "")
        try:
            plan = client.price_order(order_id) if order_id else client.find_trigger_entry_by_client_id(str(intent["client_id"]), contract)
            if not isinstance(plan, dict):
                raise RuntimeError("Gate 突破计划单状态不可见")
            status = str(plan.get("status") or "").lower()
            finish_as = str(plan.get("finish_as") or "").lower()
            if status == "open":
                result = service.cancel_trigger_entry_confirmed(contract=contract, order_id=str(plan.get("id") or order_id))
                journal.update(str(intent["client_id"]), "trigger_cancelled", order_status="cancelled", last_error="")
                outcomes.append({"status": "breakout_cancelled_by_new_signal", "contract": contract, "result": result})
            elif finish_as == "succeeded" or plan.get("trade_id"):
                journal.update(str(intent["client_id"]), "triggered_order_pending", order_status="triggered",
                               last_error="计划单已触发，必须先完成成交与保护对账")
                outcomes.append({"status": "breakout_triggered_reconciliation_pending", "contract": contract})
                stop_cycle = True
            else:
                terminal = {"expired": "trigger_expired", "cancelled": "trigger_cancelled", "failed": "trigger_failed"}.get(finish_as, "manual_review")
                journal.update(str(intent["client_id"]), terminal, order_status=finish_as or status,
                               last_error=str(plan.get("reason") or ""))
                outcomes.append({"status": terminal, "contract": contract})
                stop_cycle = stop_cycle or terminal == "manual_review"
        except Exception as exc:
            journal.update(str(intent["client_id"]), "manual_review", last_error=str(exc))
            outcomes.append({"status": "breakout_cancel_ambiguous", "contract": contract, "reason": str(exc)})
            stop_cycle = True
    return kept, outcomes, stop_cycle


def _ema(values, period):
    if not values: return 0.0
    k = 2 / (period + 1); out = values[0]
    for value in values[1:]: out = value * k + out * (1 - k)
    return out


def _rsi(closes, period=14):
    if len(closes) <= period:
        return 50.0
    diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(0.0, value) for value in diffs[-period:]]
    losses = [max(0.0, -value) for value in diffs[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    return 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _atr(highs, lows, closes, period=14):
    if len(closes) <= period:
        return 0.0
    true_ranges = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])) for i in range(1, len(closes))]
    return sum(true_ranges[-period:]) / period


def _adx(highs, lows, closes, period=14):
    if len(closes) <= period * 2:
        return 0.0
    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
        up, down = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    dx = []
    for i in range(period, len(trs)):
        tr = sum(trs[i - period + 1:i + 1])
        if tr <= 0:
            continue
        plus = 100 * sum(plus_dm[i - period + 1:i + 1]) / tr
        minus = 100 * sum(minus_dm[i - period + 1:i + 1]) / tr
        dx.append(100 * abs(plus - minus) / (plus + minus) if plus + minus else 0.0)
    return sum(dx[-period:]) / min(period, len(dx)) if dx else 0.0


def _features(client, contract, ticker=None):
    raw = client.candlesticks(contract, "15m", 160)
    rows = list(reversed(raw or []))
    closes = [float(x.get("c") or 0) for x in rows]
    highs = [float(x.get("h") or 0) for x in rows]
    lows = [float(x.get("l") or 0) for x in rows]
    volumes = [float(x.get("v") or 0) for x in rows]
    if len(closes) < 30: raise RuntimeError(f"insufficient Gate candles for {contract}")
    atr_15m = _atr(highs, lows, closes)
    vwap_denominator = sum(volumes)
    vwap = sum(price * volume for price, volume in zip(closes, volumes)) / vwap_denominator if vwap_denominator else closes[-1]
    volume_baseline = sum(volumes[-6:-1]) / 5 if len(volumes) >= 6 else 0
    obv = sum((volumes[i] if closes[i] > closes[i - 1] else -volumes[i] if closes[i] < closes[i - 1] else 0) for i in range(1, len(closes)))
    returns_15m = [(closes[i] / closes[i - 1] - 1.0) for i in range(1, len(closes)) if closes[i - 1] > 0]
    mean_return = sum(returns_15m) / len(returns_15m) if returns_15m else 0.0
    centered = [value - mean_return for value in returns_15m]
    variance = sum(value * value for value in centered) / len(centered) if centered else 0.0
    stddev = variance ** 0.5
    skewness = sum(value ** 3 for value in centered) / len(centered) / (stddev ** 3) if stddev else 0.0
    kurtosis = sum(value ** 4 for value in centered) / len(centered) / (stddev ** 4) - 3.0 if stddev else 0.0
    sorted_returns = sorted(returns_15m)
    tail_count = max(1, int(len(sorted_returns) * 0.05)) if sorted_returns else 1
    var_95 = abs(sorted_returns[tail_count - 1]) * 100 if sorted_returns else 0.0
    cvar_95 = abs(sum(sorted_returns[:tail_count]) / tail_count) * 100 if sorted_returns else 0.0
    impulse = sum(returns_15m[-8:]) if returns_15m else 0.0
    jerk = (returns_15m[-1] - 2 * returns_15m[-2] + returns_15m[-3]) if len(returns_15m) >= 3 else 0.0
    energy_integral = sum(value * value for value in returns_15m[-24:])
    deviation_area = sum(abs(value - mean_return) for value in returns_15m[-24:])
    volume_action = sum((1 if closes[i] >= closes[i - 1] else -1) * volumes[i] for i in range(1, len(closes))[-24:])

    higher = {}
    for interval, limit in (("1h", 100), ("4h", 60)):
        higher_raw = client.candlesticks(contract, interval, limit)
        higher_rows = list(reversed(higher_raw or []))
        h_closes = [float(x.get("c") or 0) for x in higher_rows]
        h_highs = [float(x.get("h") or 0) for x in higher_rows]
        h_lows = [float(x.get("l") or 0) for x in higher_rows]
        if len(h_closes) < 20:
            continue
        if interval == "1h":
            sma7 = sum(h_closes[-7:]) / 7
            sma20 = sum(h_closes[-20:]) / 20
            returns = [(h_closes[i] / h_closes[i - 1] - 1) for i in range(1, len(h_closes)) if h_closes[i - 1] > 0]
            velocity = sum(returns[-5:]) / min(5, len(returns)) if returns else 0.0
            acceleration = (sum(returns[-3:]) / min(3, len(returns)) - sum(returns[-8:-3]) / min(5, max(1, len(returns[-8:-3])))) if len(returns) >= 3 else 0.0
            higher.update({"atr_1h": round(_atr(h_highs, h_lows, h_closes), 6), "rsi_1h": round(_rsi(h_closes), 2), "adx_1h": round(_adx(h_highs, h_lows, h_closes), 2), "velocity_1h": round(velocity, 8), "acceleration_1h": round(acceleration, 8), "continuation_prob_pct": round(sum(value > 0 for value in returns[-20:]) / min(20, len(returns)) * 100, 2) if returns else 0.0, "breakdown_prob_pct": round(sum(value < 0 for value in returns[-20:]) / min(20, len(returns)) * 100, 2) if returns else 0.0, "recent_1h": [[float(x.get("o") or 0), float(x.get("h") or 0), float(x.get("l") or 0), float(x.get("c") or 0), float(x.get("v") or 0)] for x in reversed(higher_rows[-12:])], "sma7_1h": round(sma7, 8), "sma20_1h": round(sma20, 8), "structure_1h": "BULL" if h_closes[-1] > sma7 > sma20 else "BEAR" if h_closes[-1] < sma7 < sma20 else "CHOP"})
        else:
            sma5 = sum(h_closes[-5:]) / 5
            sma12 = sum(h_closes[-12:]) / 12
            higher.update({"macro_4h": "BULL" if h_closes[-1] > sma5 > sma12 else "BEAR" if h_closes[-1] < sma5 < sma12 else "RANGE", "recent_4h": [[float(x.get("o") or 0), float(x.get("h") or 0), float(x.get("l") or 0), float(x.get("c") or 0), float(x.get("v") or 0)] for x in reversed(higher_rows[-8:])], "sma5_4h": round(sma5, 8), "sma12_4h": round(sma12, 8)})

    ticker = ticker or {}
    return {"contract": contract, "last": closes[-1], "ema20": _ema(closes[-80:], 20), "ema50": _ema(closes[-120:], 50), "rsi14": round(_rsi(closes), 2), "atr14": round(atr_15m, 6), "atr_15m": round(atr_15m, 6), "change_24h_pct": round((closes[-1] / closes[max(0, len(closes)-96)] - 1) * 100, 4), "vwap_bias_pct": round((closes[-1] - vwap) / vwap * 100, 4) if vwap else 0.0, "volume_ratio": round(volumes[-1] / volume_baseline, 4) if volume_baseline else 1.0, "obv_flow": "BULL_FLOW" if obv > 0 else "BEAR_FLOW" if obv < 0 else "NEUTRAL", "funding_rate": float(ticker.get("funding_rate") or ticker.get("funding_rate_indicative") or 0), "open_interest_contracts": float(ticker.get("total_size") or 0), "calculus": {"valid": True, "regime": "POSITIVE_MOMENTUM" if impulse > 0 else "NEGATIVE_MOMENTUM" if impulse < 0 else "RANGE_LOW_VELOCITY", "quality": 1.0, "velocity": mean_return, "acceleration": jerk, "impulse": impulse, "max_abs_jerk": abs(jerk), "definite_integrals": {"energy_integral": energy_integral, "deviation_area_integral": deviation_area, "volume_action_integral": volume_action}, "probability_theory": {"skewness": skewness, "kurtosis": kurtosis, "continuation_prob_pct": round(sum(value > 0 for value in returns_15m[-20:]) / min(20, len(returns_15m)) * 100, 2) if returns_15m else 50.0, "breakdown_prob_pct": round(sum(value < 0 for value in returns_15m[-20:]) / min(20, len(returns_15m)) * 100, 2) if returns_15m else 50.0, "var_95_pct": var_95, "cvar_95_pct": cvar_95}}, "recent_15m": [[float(x.get("o") or 0), float(x.get("h") or 0), float(x.get("l") or 0), float(x.get("c") or 0), float(x.get("v") or 0)] for x in reversed(rows[-12:])], **higher}


def _strategy_package(feature: dict, ticker: dict | None = None) -> dict:
    """Map Gate-native fields into the inherited strategy package contract."""
    ticker = ticker or {}
    inst = feature["contract"].replace("_USDT", "-USDT-SWAP")
    calculus = feature.get("calculus") or {}
    calc_1h = {"velocity": feature.get("velocity_1h", 0), "acceleration": feature.get("acceleration_1h", 0), "impulse": calculus.get("impulse", 0), "jerk": calculus.get("max_abs_jerk", 0), "regime": calculus.get("regime", "GATE_NATIVE"), "definite_integrals": calculus.get("definite_integrals", {}), "probability_theory": calculus.get("probability_theory", {})}
    return {
        "instId": inst, "name": feature["contract"], "type": "crypto", "precision": ticker.get("order_price_round", ""),
        "price": feature["last"], "chg24h": feature["change_24h_pct"], "bidPx": float(ticker.get("highest_bid") or feature["last"]), "askPx": float(ticker.get("lowest_ask") or feature["last"]),
        "fundingRate": feature.get("funding_rate", 0), "oiUsd": feature.get("open_interest_contracts", 0), "lsRatio": "N/A", "takerNetUsd": "N/A",
        "atr": feature.get("atr_1h", feature.get("atr14", 0)), "atr_15m": feature.get("atr_15m", 0), "atr_1h": feature.get("atr_1h", 0),
        "rsi": feature.get("rsi14", 50), "rsi_15m": feature.get("rsi14", 50), "rsi_1h": feature.get("rsi_1h", 50), "vwap_bias": feature.get("vwap_bias_pct", 0),
        "vol_ratio": feature.get("volume_ratio", 1), "obv_flow": feature.get("obv_flow", "NEUTRAL"), "adx_1h": feature.get("adx_1h", 0), "structure_1h": feature.get("structure_1h", "CHOP"), "macro_4h": feature.get("macro_4h", "RANGE"),
        "smart_money": {"weighted_long_pct": 50.0, "net_flow_usdt": "N/A", "avg_long_entry": "--", "avg_short_entry": "--", "top_win_rate": "N/A"},
        "calculus": {**calculus, "valid": True, "regime": calculus.get("regime", "GATE_NATIVE"), "quality": calculus.get("quality", 1.0), "velocity": feature.get("velocity_1h", 0), "acceleration": feature.get("acceleration_1h", 0), "probability_theory": {**calculus.get("probability_theory", {}), "continuation_prob_pct": feature.get("continuation_prob_pct", 0), "breakdown_prob_pct": feature.get("breakdown_prob_pct", 0)}, "timeframes": {"1H": calc_1h}}, "data_quality": "valid",
        "recent_15m": feature.get("recent_15m", []), "recent_1h": feature.get("recent_1h", []), "recent_4h": feature.get("recent_4h", []),
    }


def _text_part(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or item.get("thinking") or ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(value or "")


def _extract_response_object(*parts):
    """Find the last complete JSON response containing all six Gate contracts."""
    text = "\n".join(p for p in parts if p)
    candidates = []
    for part in parts:
        if not part:
            continue
        try:
            direct = json.loads(part)
            if isinstance(direct, dict):
                candidates.append(json.dumps(direct, ensure_ascii=False))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    # Prefer fenced JSON blocks, then scan every balanced object from the end.
    for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.I | re.S):
        candidates.append(match.group(1))
    for start, char in enumerate(text):
        if char != "{":
            continue
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            current = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    in_string = False
                continue
            if current == '"':
                in_string = True
            elif current == "{":
                depth += 1
            elif current == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:index + 1])
                    break
    required = set(SYMBOLS)
    for raw in reversed(candidates):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        # Only accept an explicit top-level `decisions` object. Otherwise a
        # nested position/pending map with the same six symbol keys could be
        # mistaken for trade decisions.
        decisions = parsed.get("decisions") if isinstance(parsed, dict) else None
        if isinstance(decisions, dict):
            normalized = {}
            for key, value in decisions.items():
                canonical = str(key).upper().replace("-USDT-SWAP", "_USDT").replace("-", "_")
                normalized[canonical] = value
            decisions = normalized
        if isinstance(decisions, dict) and required.issubset(decisions.keys()):
            # Reject nested/partial objects whose values belong to a lifecycle
            # map (e.g. KEEP/HOLD) rather than the trade decision contract.
            valid_actions = True
            for symbol in required:
                value = decisions.get(symbol)
                if isinstance(value, str):
                    valid_actions = value.upper() in {"BUY_LONG", "SELL_SHORT", "WAIT"}
                elif isinstance(value, dict):
                    valid_actions = str(value.get("action", "")).upper() in {"BUY_LONG", "SELL_SHORT", "WAIT"}
                else:
                    valid_actions = False
                if not valid_actions:
                    break
            if valid_actions:
                result = dict(parsed)
                result["decisions"] = decisions
                return result
    return None


def _extract_decision_object(*parts):
    response = _extract_response_object(*parts)
    return response.get("decisions") if isinstance(response, dict) else None


def _round_price(value: float, step: str | float, rounding=ROUND_HALF_UP) -> str:
    """Round a trigger to Gate's contract price unit without float drift."""
    quantum = Decimal(str(step or "0"))
    if quantum <= 0:
        return f"{value:.8g}"
    rounded = (Decimal(str(value)) / quantum).quantize(Decimal("1"), rounding=rounding) * quantum
    return format(rounded, "f").rstrip("0").rstrip(".") or "0"


def _order_size_for_margin(*, margin_usdt: float, leverage: float, entry_price: float, multiplier: float, minimum: str | float = 1, maximum: str | float = 0, enable_decimal: bool = False) -> Decimal:
    """Convert margin to Gate-native contracts using the exchange's size quantum."""
    margin = Decimal(str(margin_usdt)); lev = Decimal(str(leverage)); price = Decimal(str(entry_price)); mult = Decimal(str(multiplier))
    if margin <= 0 or lev <= 0 or price <= 0 or mult <= 0:
        raise ValueError("Gate order sizing requires positive margin, leverage, entry price and quanto_multiplier")
    minimum_size = Decimal(str(minimum or 1))
    quantum = minimum_size if enable_decimal else Decimal("1")
    raw_size = (margin * lev) / (price * mult)
    size = (raw_size / quantum).quantize(Decimal("1"), rounding=ROUND_DOWN) * quantum
    if size < minimum_size:
        raise ValueError("Configured margin is too small for Gate contract minimum size")
    maximum_size = Decimal(str(maximum or 0))
    if maximum_size > 0 and size > maximum_size:
        size = (maximum_size / quantum).quantize(Decimal("1"), rounding=ROUND_DOWN) * quantum
    return size


def _protection_matches(rows: list[dict], *, client_id: str, size: int | float | Decimal, rule: int) -> bool:
    signed_size = Decimal(str(size))
    expected_side = "long" if signed_size < 0 else "short"
    for row in rows or []:
        initial = row.get("initial") or {}
        trigger = row.get("trigger") or {}
        covered = abs(Decimal(str(initial.get("size") or 0)))
        required = abs(signed_size)
        full_close = (
            str(initial.get("auto_size") or "").lower() == f"close_{expected_side}"
            or str(row.get("order_type") or "").lower() == f"close-{expected_side}-position"
        )
        if str(initial.get("text") or "") == client_id and (covered >= required or full_close) and int(trigger.get("rule") or 0) == rule:
            return True
    return False


def _as_instruction_list(value) -> list[dict]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        # Accept the common single-instruction object and keyed contract map.
        if "action" in value or "contract" in value or "instId" in value:
            return [value]
        rows = []
        for key, item in value.items():
            if isinstance(item, dict):
                rows.append({"contract": key, **item})
            elif isinstance(item, str):
                rows.append({"contract": key, "action": item})
        return rows
    return []


def _execution_enabled(settings) -> bool:
    """Environment-bound write gate; one environment's switch cannot unlock the other."""
    if not get_risk_profile(settings.risk_profile).execution_allowed:
        return False
    if settings.environment == "testnet":
        return bool(settings.testnet_execute_trades)
    if settings.environment == "live":
        return bool(settings.live_trading_enabled)
    return False


def run_cycle() -> dict:
    settings = load_settings()
    risk_profile = get_risk_profile(settings.risk_profile)
    risk_snapshot = risk_profile.snapshot(leverage=settings.leverage, environment=settings.environment)
    risk_snapshot["configured_max_entries_per_cycle"] = settings.max_entries_per_cycle
    try:
        from r20_backend.policy_snapshot import generate_policy_snapshot
        policy_snapshot = generate_policy_snapshot(root_dir=ROOT)
    except Exception as exc:
        policy_snapshot = {"policy_version": "gate@unavailable", "policy_hash": "unavailable", "error": str(exc)}
    risk_snapshot.update({"policy_version": policy_snapshot["policy_version"], "policy_hash": policy_snapshot["policy_hash"]})
    execution_enabled = _execution_enabled(settings)
    client = GateFuturesClient(settings)
    market = GateFuturesClient(replace(settings, environment=settings.public_market_environment, api_key="public-readonly", api_secret="public-readonly", live_trading_enabled=True))
    try:
        tickers = market.tickers()
    except Exception:
        raise
    ticker_by_symbol = {str(row.get("contract")): row for row in (tickers or []) if isinstance(row, dict)}
    features = [_features(market, symbol, ticker_by_symbol.get(symbol)) for symbol in SYMBOLS]
    strategy_packages = [_strategy_package(feature, ticker_by_symbol.get(feature["contract"])) for feature in features]
    private_context = {"account": {}, "positions": [], "pending_orders": [], "trigger_entries": [], "protections": [], "private_errors": []}
    try:
        private_context["account"] = client.account() or {}
        private_context["positions"] = client.positions() or []
        private_context["pending_orders"] = client.open_orders() or []
        private_context["trigger_entries"] = client.trigger_entry_orders() or []
        private_context["protections"] = client.protection_orders() or []
    except Exception as exc:
        private_context["private_errors"].append({"category": classify_error(exc), "error": f"{type(exc).__name__}: {exc}"})
    reconciliation = reconcile_exchange_state(
        private_context["positions"], private_context["pending_orders"], private_context["protections"],
        max_pending_age_seconds=settings.max_pending_order_age_seconds,
    )
    pending_executions = ExecutionJournal(EXECUTION_JOURNAL).active(settings.environment)
    unsafe_pending_executions = [row for row in pending_executions if str(row.get("status") or "") != "awaiting_trigger"]
    if unsafe_pending_executions:
        reconciliation["issues"].append({
            "code": "execution_reconciliation_pending",
            "count": len(unsafe_pending_executions),
            "contracts": sorted({str(row.get("contract") or "") for row in unsafe_pending_executions}),
        })
        reconciliation["safe_for_new_risk"] = False
    untracked_trigger_entries = _untracked_trigger_entries(private_context["trigger_entries"], pending_executions)
    if untracked_trigger_entries:
        reconciliation["issues"].append({
            "code": "untracked_trigger_entry_orders",
            "count": len(untracked_trigger_entries),
            "orders": untracked_trigger_entries,
        })
        reconciliation["safe_for_new_risk"] = False
    equity = float(private_context["account"].get("total") or private_context["account"].get("available") or 0)
    daily_loss = daily_loss_state(LEDGER, max_loss_usd=settings.max_daily_loss_usd, max_loss_ratio=settings.max_daily_loss_ratio, equity=equity)
    cooldowns = {symbol: cooldown_state(LEDGER, cooldown_seconds=settings.stop_cooldown_seconds, contract=symbol) for symbol in SYMBOLS}
    cooldown = {"active": False, "remaining_seconds": 0, "reason": "per_contract", "contracts": cooldowns}
    safety_status = {
        "environment": settings.environment, "checked_at_ms": int(time.time() * 1000),
        "reconciliation": reconciliation, "daily_loss": daily_loss, "cooldown": cooldown,
        "private_errors": private_context["private_errors"],
    }
    safety_status["safe_for_new_risk"] = bool(not private_context["private_errors"] and reconciliation["safe_for_new_risk"] and not daily_loss["tripped"])
    lifecycle_actions: list[dict] = []
    if execution_enabled and not private_context["private_errors"]:
        service = GateTradingService(client, RiskLimits(settings.max_position_notional_usd, settings.max_total_margin_usd, settings.max_order_margin_usd))
        lifecycle_close_attempts: set[str] = set()
        protection_intents = load_protection_intents(PROTECTION_INTENTS, DECISION_AUDIT, DECISION_HISTORY, settings.environment)
        for orphan in reconciliation["orphan_protections"]:
            order_id = str(orphan.get("id_string") or orphan.get("id") or "")
            contract_name = str((orphan.get("initial") or {}).get("contract") or orphan.get("contract") or "").upper()
            if not order_id or not is_system_protection(orphan):
                lifecycle_actions.append({"action": "KEEP_UNVERIFIED_ORPHAN", "contract": contract_name, "order_id": order_id, "reason": "not_created_by_gate_quant"})
                continue
            try:
                lifecycle_actions.append({"action": "CANCEL_SYSTEM_ORPHAN", "contract": contract_name, **service.cancel_protection_confirmed(order_id=order_id)})
            except Exception as exc:
                lifecycle_actions.append({"action": "CANCEL_SYSTEM_ORPHAN", "contract": contract_name, "order_id": order_id, "error": str(exc), "category": classify_error(exc)})
        for stale in reconciliation["stale_orders"]:
            order_id = str(stale.get("id") or "")
            contract_name = str(stale.get("contract") or "").upper()
            if not order_id or not contract_name:
                continue
            try:
                lifecycle_actions.append({"action": "CANCEL_STALE", "contract": contract_name, **service.cancel_order_confirmed(contract=contract_name, order_id=order_id)})
            except Exception as exc:
                lifecycle_actions.append({"action": "CANCEL_STALE", "contract": contract_name, "order_id": order_id, "error": str(exc), "category": classify_error(exc)})
        for position in private_context["positions"]:
            size = Decimal(str(position.get("size") or 0))
            if size == 0:
                continue
            age = position_age_seconds(position)
            if age is not None and settings.max_position_age_seconds > 0 and age >= settings.max_position_age_seconds:
                contract_name = str(position.get("contract") or "").upper()
                lifecycle_close_attempts.add(contract_name)
                try:
                    closed = service.close_position_safely(contract=contract_name, client_id=f"t-gate-time-{int(time.time() * 1000)}")
                    lifecycle_actions.append({"action": "CLOSE_MAX_AGE", "contract": contract_name, "age_seconds": int(age), "result": closed})
                except Exception as exc:
                    lifecycle_actions.append({"action": "CLOSE_MAX_AGE", "contract": contract_name, "age_seconds": int(age), "error": str(exc), "category": classify_error(exc)})
        recovery_source_available = True
        try:
            # Cleanup and time-based closes may have changed exchange state. A
            # protection repair must only use a fresh, post-action position.
            private_context["positions"] = client.positions() or []
            private_context["pending_orders"] = client.open_orders() or []
            private_context["protections"] = client.protection_orders() or []
            # A max-age close can turn all protections for that contract into
            # orphans. Reconcile once more after the close and remove only
            # Gate-created protections; manual/unknown orders remain visible
            # and keep fail-closed protection.
            post_close_reconciliation = reconcile_exchange_state(
                private_context["positions"], private_context["pending_orders"], private_context["protections"],
                max_pending_age_seconds=settings.max_pending_order_age_seconds,
            )
            for orphan in post_close_reconciliation["orphan_protections"]:
                order_id = str(orphan.get("id_string") or orphan.get("id") or "")
                contract_name = str((orphan.get("initial") or {}).get("contract") or orphan.get("contract") or "").upper()
                if not order_id or not is_system_protection(orphan):
                    lifecycle_actions.append({"action": "KEEP_UNVERIFIED_ORPHAN", "contract": contract_name, "order_id": order_id, "reason": "not_created_by_gate_quant"})
                    continue
                try:
                    lifecycle_actions.append({"action": "CANCEL_SYSTEM_ORPHAN_AFTER_CLOSE", "contract": contract_name, **service.cancel_protection_confirmed(order_id=order_id)})
                except Exception as exc:
                    lifecycle_actions.append({"action": "CANCEL_SYSTEM_ORPHAN_AFTER_CLOSE", "contract": contract_name, "order_id": order_id, "error": str(exc), "category": classify_error(exc)})
            if post_close_reconciliation["orphan_protections"]:
                private_context["protections"] = client.protection_orders() or []
        except Exception as exc:
            recovery_source_available = False
            lifecycle_actions.append({"action": "REFRESH_BEFORE_PROTECTION_REPAIR", "error": str(exc), "category": classify_error(exc)})
        plans = recovery_plans(private_context["positions"], private_context["protections"], protection_intents) if recovery_source_available else []
        protection_closed_contracts: set[str] = set()
        for plan in plans:
            contract_name = plan["contract"]
            if contract_name in lifecycle_close_attempts or contract_name in protection_closed_contracts:
                continue
            if plan["close_required"]:
                client_id = f"t-gate-pclose-{int(time.time() * 1000)}"
                try:
                    closed = service.close_position_safely(contract=contract_name, client_id=client_id)
                    lifecycle_actions.append({"action": "CLOSE_CROSSED_MISSING_PROTECTION", **plan, "client_id": client_id, "result": closed})
                    protection_closed_contracts.add(contract_name)
                except Exception as exc:
                    lifecycle_actions.append({"action": "CLOSE_CROSSED_MISSING_PROTECTION", **plan, "client_id": client_id, "error": str(exc), "category": classify_error(exc)})
                continue
            if not plan["recoverable"]:
                lifecycle_actions.append({"action": "KEEP_PROTECTION_GAP", **plan})
                continue
            kind = plan["kind"]
            client_tag = "rtp" if kind == "take_profit" else "rsl"
            client_id = f"t-gate-{client_tag}-{int(time.time() * 1000)}"
            try:
                meta = client.contracts(contract_name)
                trigger_price = _round_price(float(plan["trigger_price"]), meta.get("order_price_round") or meta.get("mark_price_round") or "0")
                created = client.create_protection_order(contract=contract_name, size=Decimal(plan["close_size"]), trigger_price=trigger_price, rule=plan["rule"], client_id=client_id)
                current_rows = client.protection_orders(contract_name) or []
                if not _protection_matches(current_rows, client_id=client_id, size=Decimal(plan["close_size"]), rule=plan["rule"]):
                    raise RuntimeError(f"Gate repaired {kind} order was not confirmed")
                lifecycle_actions.append({"action": "RESTORE_TAKE_PROFIT" if kind == "take_profit" else "RESTORE_STOP_LOSS", **plan, "client_id": client_id, "result": created})
            except Exception as exc:
                lifecycle_actions.append({"action": "RESTORE_TAKE_PROFIT" if kind == "take_profit" else "RESTORE_STOP_LOSS", **plan, "client_id": client_id, "error": str(exc), "category": classify_error(exc)})
        if lifecycle_actions:
            try:
                private_context["positions"] = client.positions() or []
                private_context["pending_orders"] = client.open_orders() or []
                private_context["protections"] = client.protection_orders() or []
                reconciliation = reconcile_exchange_state(private_context["positions"], private_context["pending_orders"], private_context["protections"], max_pending_age_seconds=settings.max_pending_order_age_seconds)
                safety_status["reconciliation"] = reconciliation
                safety_status["safe_for_new_risk"] = bool(reconciliation["safe_for_new_risk"] and not daily_loss["tripped"])
            except Exception as exc:
                safety_status["safe_for_new_risk"] = False
                safety_status["private_errors"].append({"category": classify_error(exc), "error": f"post-action reconciliation: {exc}"})
    safety_status["lifecycle_actions"] = lifecycle_actions
    atomic_json(SAFETY_STATUS, safety_status)
    atomic_json(RUNTIME_HEARTBEAT, {"component": "gate_trader", "pid": os.getpid(), "timestamp_ms": safety_status["checked_at_ms"], "status": "running", "safe_for_new_risk": safety_status["safe_for_new_risk"]})
    selected = features[0]
    decisions = {f["contract"]: {"action": "WAIT", "confidence": 0.0, "entry_price": 0.0, "summary_reason": "No valid LLM decision"} for f in features}
    llm_source = "gate-multifactor-fallback"
    llm_error = ""
    llm_preview = ""
    model_telemetry = None
    try:
        from r20_backend.llm_manager import get_active_llm_runtime
        try:
            from .strategy_adapter import build_prompt
            active_positions_detail = []
            for position in private_context["positions"]:
                size = float(position.get("size") or 0)
                if not size:
                    continue
                active_positions_detail.append({"instId": str(position.get("contract") or "").replace("_USDT", "-USDT-SWAP"), "name": position.get("contract"), "side": "long" if size > 0 else "short", "pos": str(abs(size)), "lever": position.get("leverage") or position.get("lever") or "--", "avgPx": position.get("entry_price"), "lastPx": position.get("mark_price"), "upl": position.get("unrealised_pnl"), "uplRatio": 0.0})
            pending_orders_detail = [{"ordId": str(order.get("id")), "instId": str(order.get("contract") or "").replace("_USDT", "-USDT-SWAP"), "side": "buy" if float(order.get("size") or 0) > 0 else "sell", "posSide": "net", "px": order.get("price"), "sz": abs(float(order.get("size") or 0)), "state": order.get("status", "open"), "cTime": str(order.get("create_time_ms") or ""), "text": order.get("text", "")} for order in private_context["pending_orders"] if isinstance(order, dict)]
            pending_orders_detail.extend([
                {"ordId": str(order.get("id")), "instId": str((order.get("initial") or {}).get("contract") or "").replace("_USDT", "-USDT-SWAP"),
                 "side": "buy" if float((order.get("initial") or {}).get("size") or 0) > 0 else "sell",
                 "posSide": "net", "px": (order.get("trigger") or {}).get("price"),
                 "sz": abs(float((order.get("initial") or {}).get("size") or 0)), "state": "waiting_breakout",
                 "cTime": str(order.get("create_time") or ""), "text": (order.get("initial") or {}).get("text", "")}
                for order in private_context["trigger_entries"] if isinstance(order, dict)
            ])
            available = float(private_context["account"].get("available") or 0)
            committed_margin = _account_committed_margin(private_context["account"])
            system_prompt, prompt = build_prompt(strategy_packages, positions=active_positions_detail, pending_orders=pending_orders_detail, available_usdt=available, execution_leverage=settings.leverage, max_order_margin_usdt=settings.max_order_margin_usd, max_total_margin_usdt=settings.max_total_margin_usd, current_margin_usdt=committed_margin, risk_snapshot=risk_snapshot, entry_intent_enabled=settings.entry_intent_enabled)
            prompt += (f"\n本轮最多允许实际提交 {settings.max_entries_per_cycle} 个开仓/加仓订单。"
                       "每个合约应独立判断；某个合约等待二次报价确认或被风控拒绝，不代表其他合格合约必须观望。")
        except Exception:
            prompt = ('Output ONLY one compact final JSON object with top-level keys decisions, position_management, pending_orders_management. '
                      f'The decisions object must contain all six contracts and each item must include action BUY_LONG|SELL_SHORT|WAIT, confidence 0-100, entry_price, take_profit_price, stop_loss_price, leverage={settings.leverage:g}, margin_usdt (positive and <= {settings.max_order_margin_usd:.2f}), summary_reason'
                      + (', and entry_intent immediate|retracement|breakout. ' if settings.entry_intent_enabled else '. ')
                      + f'Active Gate risk profile={risk_profile.label}: confidence>={risk_profile.min_confidence:g}%, DOGE>={risk_profile.doge_min_confidence:g}%, ADX>={risk_profile.min_adx:g}, R:R>={risk_profile.min_rr:g}, target R:R>={risk_profile.target_rr:g}, effective per-order margin cap={settings.max_order_margin_usd * risk_profile.margin_ratio:.2f} USDT, total margin cap={settings.max_total_margin_usd:.2f} USDT, max actual entries this cycle={settings.max_entries_per_cycle}. Different Gate contracts may be held concurrently and an active contract may be added to only in the same direction, subject to aggregate limits and complete TP/SL coverage. A deferred or rejected symbol must not force unrelated qualified symbols to WAIT. '
                      'position_management actions are HOLD|CLOSE_MARKET|UPDATE_SL; pending_orders_management actions are KEEP|CANCEL. '
                      'Gate Futures data: ' + json.dumps({"instruments": features, "account": private_context["account"], "positions": private_context["positions"], "pending_orders": private_context["pending_orders"]}, ensure_ascii=False))
            system_prompt = "You are a Gate Futures risk-controlled trading decision model. Return the final JSON object only."
        runtime = get_active_llm_runtime()
        request_url = runtime["base_url"].rstrip("/") + "/chat/completions"
        request_headers = {"Authorization": f"Bearer {runtime['api_key']}", "Content-Type": "application/json"}
        request_body = {"model": runtime["model"], "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}], "temperature": 0.0, "max_tokens": 6000, "reasoning_effort": "none", "response_format": {"type": "json_object"}}
        try:
            from r20_gateway.telemetry import ModelCallTelemetry
            model_telemetry = ModelCallTelemetry("gate_quant.ai_worker", runtime["model"], "none", system_prompt, prompt)
        except Exception:
            model_telemetry = None
        request_proxies = {"http": settings.proxy_url, "https": settings.proxy_url} if settings.proxy_url else None
        response = requests.post(request_url, headers=request_headers, json=request_body, timeout=60, proxies=request_proxies)
        # Some OpenAI-compatible gateways reject response_format; retry the same
        # read-only decision request without it, never an exchange order.
        if response.status_code == 400 and any(name in response.text.lower() for name in ("response_format", "reasoning_effort")):
            request_body.pop("response_format", None)
            request_body.pop("reasoning_effort", None)
            response = requests.post(request_url, headers=request_headers, json=request_body, timeout=60, proxies=request_proxies)
        response.raise_for_status()
        message = response.json()["choices"][0].get("message") or {}
        content = _text_part(message.get("content"))
        reasoning = _text_part(message.get("reasoning_content"))
        llm_preview = (content or reasoning)[-500:]
        full_response = _extract_response_object(content, reasoning)
        raw_decisions = full_response.get("decisions") if full_response else None
        valid = raw_decisions is not None
        if not valid:
            raise ValueError("LLM response did not contain a complete six-contract JSON decision")
        for feature in features:
            item = raw_decisions.get(feature["contract"]) or {}
            if isinstance(item, str):
                item = {"action": item}
            if not isinstance(item, dict):
                raise ValueError(f"invalid decision object for {feature['contract']}")
            candidate = str(item.get("action", "WAIT")).upper()
            if candidate not in {"BUY_LONG", "SELL_SHORT", "WAIT"}:
                raise ValueError(f"invalid action for {feature['contract']}: {candidate}")
            raw_margin = float(item.get("margin_usdt") or item.get("margin_usd") or 0)
            effective_margin_cap = settings.max_order_margin_usd * risk_profile.margin_ratio
            accepted_margin = max(0.0, min(raw_margin, effective_margin_cap, available))
            entry_intent = str(item.get("entry_intent") or "").lower()
            intent_valid = candidate == "WAIT" or not settings.entry_intent_enabled or entry_intent in ENTRY_INTENTS
            decisions[feature["contract"]] = {"action": candidate if intent_valid else "WAIT", "raw_action": candidate, "confidence": max(0.0, min(100.0, float(item.get("confidence") or 0))), "entry_intent": entry_intent if candidate != "WAIT" else "", "entry_price": float(item.get("entry_price") or feature["last"]) if candidate != "WAIT" and intent_valid else 0.0, "take_profit_price": float(item.get("take_profit_price") or 0) if intent_valid else 0.0, "stop_loss_price": float(item.get("stop_loss_price") or 0) if intent_valid else 0.0, "leverage": settings.leverage, "margin_usdt": accepted_margin, "margin_usd": accepted_margin, "summary_reason": str(item.get("summary_reason") or "LLM decision"), **({"rejection_reason": "AI 未返回合法 entry_intent，已安全转为观望"} if not intent_valid else {})}
        llm_source = "gate-multifactor-llm"
        if model_telemetry:
            model_telemetry.finish("success", response.json(), output_chars=len(content or reasoning))
    except Exception as exc:
        if model_telemetry:
            model_telemetry.finish("error", output_chars=len(llm_preview), error=exc)
        llm_error = f"{type(exc).__name__}: {exc}"[:300]
        llm_source = "gate-multifactor-llm-error"
    now = int(time.time() * 1000)
    management = full_response if 'full_response' in locals() and isinstance(full_response, dict) else {}
    position_management = _as_instruction_list(management.get("position_management"))
    pending_management = _as_instruction_list(management.get("pending_orders_management"))
    decisions_payload = {symbol: {"instId": symbol, "decision_timestamp_ms": now, "decision": item, "source": llm_source, "llm_error": llm_error, "llm_preview": llm_preview, "policy_version": policy_snapshot["policy_version"], "policy_hash": policy_snapshot["policy_hash"], "risk_snapshot": risk_snapshot, "indicators": next(f for f in features if f["contract"] == symbol)} for symbol, item in decisions.items()}
    pending_requotes = _load_pending_requotes(now)
    # Apply the inherited deterministic quote gate before selecting a candidate.
    try:
        from .strategy_adapter import validate_decision
        active_positions = {str(p.get("contract")): p for p in private_context["positions"] if float(p.get("size") or 0) != 0}
        active_sides = {symbol: "long" if float(position.get("size") or 0) > 0 else "short" for symbol, position in active_positions.items()}
        for symbol, envelope in decisions_payload.items():
            item = envelope["decision"]
            if item.get("action") == "WAIT":
                continue
            # Normalize inherited council aliases at the strategy boundary.
            item["take_profit_price"] = float(item.get("take_profit_price") or item.get("take_profit") or 0)
            item["stop_loss_price"] = float(item.get("stop_loss_price") or item.get("stop_loss") or 0)
            item["entry_price"] = float(item.get("entry_price") or 0)
            package = {**envelope["indicators"], "instId": symbol, "data_quality": "valid", "macro_4h": envelope["indicators"].get("macro_4h", "RANGE")}
            gated, reason, rr = validate_decision(package, item, active_inst_ids=set(active_positions), active_position_sides=active_sides, risk_snapshot=risk_snapshot)
            item["risk_reward_ratio"] = rr
            if gated == "WAIT":
                item["rejection_reason"] = reason
                item["action"] = "WAIT"
                item["entry_price"] = item["take_profit_price"] = item["stop_loss_price"] = 0.0
    except Exception as exc:
        for envelope in decisions_payload.values():
            if envelope["decision"].get("action") != "WAIT":
                envelope["decision"].update({"action": "WAIT", "entry_price": 0.0, "take_profit_price": 0.0, "stop_loss_price": 0.0, "rejection_reason": f"strategy gate unavailable: {exc}"})
    breakout_kept_contracts: set[str] = set()
    breakout_lifecycle: list[dict] = []
    breakout_stop_cycle = False
    if execution_enabled and settings.entry_intent_enabled and llm_source == "gate-multifactor-llm" and not private_context["private_errors"]:
        breakout_kept_contracts, breakout_lifecycle, breakout_stop_cycle = _manage_breakout_plans(
            client,
            ExecutionJournal(EXECUTION_JOURNAL),
            pending_executions,
            decisions_payload,
            settings,
        )
    result: dict = {"trade": {"status": "not_submitted", "reason": f"Gate {settings.environment.title()} automatic execution is disabled" if not execution_enabled else f"No qualifying {settings.environment.title()} signal"}}
    executable = [d for d in decisions_payload.values() if d["decision"].get("action") in {"BUY_LONG", "SELL_SHORT"} and float(d["decision"].get("confidence") or 0) >= risk_profile.min_confidence]
    confirmed_requotes = _confirm_pending_requotes(pending_requotes, decisions_payload, min_confidence=risk_profile.min_confidence, now_ms=now)
    executable, quote_blocks = _preflight_candidate_quotes(client, executable, pending_requotes, confirmed_requotes, now_ms=now, entry_intent_enabled=settings.entry_intent_enabled)
    if breakout_kept_contracts:
        executable = [row for row in executable if str(row.get("instId") or "") not in breakout_kept_contracts]
    if quote_blocks:
        result["trade_candidates_blocked"] = quote_blocks
        if not executable:
            result["trade"] = quote_blocks[0]
    confirmed_executable = [d for d in executable if d["instId"] in confirmed_requotes]
    decision = max(confirmed_executable or executable or decisions_payload.values(), key=lambda d: float(d["decision"].get("confidence") or 0))
    trade_symbol = decision["instId"]
    trade_feature = next(f for f in features if f["contract"] == trade_symbol)
    action = decision["decision"]["action"]
    confidence = float(decision["decision"].get("confidence") or 0)
    last = trade_feature["last"]
    if execution_enabled and llm_source == "gate-multifactor-llm":
        management_result = {"positions": [], "pending_orders": []}
        # Apply only explicit, schema-valid lifecycle instructions. Missing or
        # malformed instructions are ignored; the entry path remains fail-closed.
        for instruction in position_management:
            if not isinstance(instruction, dict):
                continue
            contract_name = str(instruction.get("contract") or instruction.get("instId") or "").upper().replace("-USDT-SWAP", "_USDT").replace("-", "_")
            current = next((p for p in private_context["positions"] if str(p.get("contract") or "").upper() == contract_name and float(p.get("size") or 0) != 0), None)
            if not current:
                continue
            mgmt_action = str(instruction.get("action") or "HOLD").upper()
            try:
                if mgmt_action == "CLOSE_MARKET":
                    management_result["positions"].append({"contract": contract_name, "action": mgmt_action, "result": client.close_position(contract=contract_name, client_id=f"t-gate-close-{now}")})
                elif mgmt_action == "UPDATE_SL" and float(instruction.get("suggested_sl_price") or 0) > 0:
                    meta = client.contracts(contract_name)
                    step = meta.get("order_price_round") or meta.get("mark_price_round") or "0"
                    position_size = Decimal(str(current.get("size") or 0))
                    management_result["positions"].append({"contract": contract_name, "action": mgmt_action, "result": client.create_protection_order(contract=contract_name, size=-position_size, trigger_price=_round_price(float(instruction["suggested_sl_price"]), step), rule=2 if position_size > 0 else 1, client_id=f"t-gate-sl-update-{now}")})
            except Exception as exc:
                management_result["positions"].append({"contract": contract_name, "action": mgmt_action, "error": str(exc)})
        for instruction in pending_management:
            if not isinstance(instruction, dict) or str(instruction.get("action") or "").upper() != "CANCEL":
                continue
            order_id = str(instruction.get("order_id") or instruction.get("ordId") or "")
            contract_name = str(instruction.get("contract") or instruction.get("instId") or "").upper().replace("-USDT-SWAP", "_USDT").replace("-", "_")
            if not order_id or not contract_name:
                continue
            try:
                pending = next((row for row in private_context["pending_orders"] if str(row.get("id") or "") == order_id and str(row.get("contract") or "").upper() == contract_name), {})
                order_text = str(pending.get("text") or "")
                cancelled_protections = []
                if order_text.startswith("t-gate-ai-"):
                    suffix = order_text.removeprefix("t-gate-ai-")
                    for protection in client.protection_orders(contract_name) or []:
                        protection_text = str((protection.get("initial") or {}).get("text") or "")
                        if protection_text in {f"t-gate-tp-{suffix}", f"t-gate-sl-{suffix}"}:
                            cancelled_protections.append(client.cancel_protection_order(str(protection.get("id"))))
                management_result["pending_orders"].append({"contract": contract_name, "order_id": order_id, "result": client.cancel_order(order_id, contract_name), "cancelled_protections": cancelled_protections})
            except Exception as exc:
                management_result["pending_orders"].append({"contract": contract_name, "order_id": order_id, "error": str(exc)})
        result["management"] = management_result
    # Execute qualifying symbols independently. A deferred/rejected candidate is
    # recorded but does not consume an entry slot or hide the next valid signal.
    candidate_outcomes: list[dict] = []
    submitted_trades: list[dict] = []
    if execution_enabled and safety_status["safe_for_new_risk"] and not breakout_stop_cycle and llm_source == "gate-multifactor-llm":
        ordered_candidates = sorted(
            executable,
            key=lambda row: (row["instId"] in confirmed_requotes, float(row["decision"].get("confidence") or 0)),
            reverse=True,
        )
        fresh_account = client.account() or {}
        fresh_positions = client.positions() or []
        if isinstance(fresh_positions, dict):
            fresh_positions = [fresh_positions] if fresh_positions else []
        risk_state = {
            "baseline_margin": _account_committed_margin(fresh_account),
            "baseline_notional": _portfolio_position_notional(client, fresh_positions),
            "reserved_margin": sum(float(row.get("estimated_margin_usdt") or 0) for row in pending_executions if str(row.get("status") or "") == "awaiting_trigger" and str(row.get("contract") or "") in breakout_kept_contracts),
            "reserved_notional": sum(float(row.get("order_notional_usdt") or 0) for row in pending_executions if str(row.get("status") or "") == "awaiting_trigger" and str(row.get("contract") or "") in breakout_kept_contracts),
        }
        def execute_candidate(candidate, sequence):
            return _execute_entry_candidate(
                client, candidate, settings=settings, risk_profile=risk_profile,
                risk_snapshot=risk_snapshot, policy_snapshot=policy_snapshot,
                confirmed_requotes=confirmed_requotes, now_ms=now, sequence=sequence,
                risk_state=risk_state,
            )

        candidate_outcomes, submitted_trades = _run_serial_candidates(
            ordered_candidates, settings.max_entries_per_cycle, execute_candidate
        )
        for trade_outcome in candidate_outcomes:
            trade_outcome.setdefault("policy_version", policy_snapshot["policy_version"])
            trade_outcome.setdefault("policy_hash", policy_snapshot["policy_hash"])
    elif execution_enabled and (not safety_status["safe_for_new_risk"] or breakout_stop_cycle):
        candidate_outcomes.append({"status": "blocked_safety_fail_closed",
                                   "reason": "Gate reconciliation, breakout lifecycle, or daily-loss gate blocked new risk",
                                   "safety_status": safety_status})

    all_outcomes = [*(result.get("trade_candidates_blocked") or []), *breakout_lifecycle, *candidate_outcomes]
    if submitted_trades:
        result["trade"] = submitted_trades[0]
    elif candidate_outcomes:
        result["trade"] = candidate_outcomes[0]
    elif result.get("trade_candidates_blocked"):
        result["trade"] = result["trade_candidates_blocked"][0]
    result["trades"] = all_outcomes
    result["submitted_trades"] = submitted_trades
    result["trade"].setdefault("policy_version", policy_snapshot["policy_version"])
    result["trade"].setdefault("policy_hash", policy_snapshot["policy_hash"])
    payload = {**decisions_payload, "generated_at_ms": now, "exchange": "gate", "environment": settings.environment,
               "policy_snapshot": policy_snapshot, "risk_snapshot": risk_snapshot, "safety_status": safety_status,
               "position_management": position_management, "pending_orders_management": pending_management,
               "private_context": private_context, "trade": result["trade"], "trades": all_outcomes}
    _save_decision_payload(payload, decisions_payload, result["trade"], now, settings.environment, risk_snapshot)
    result["decisions"] = decisions_payload
    return result



if __name__ == "__main__":
    cycle_result = run_cycle()
    print(_console_summary(cycle_result))
