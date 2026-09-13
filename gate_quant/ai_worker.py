from __future__ import annotations

import json
import os
import time
import re
import requests
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from dataclasses import replace
from dotenv import load_dotenv

from .client import GateFuturesClient
from .config import load_settings
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


def _save_decision_payload(payload: dict, decisions_payload: dict, trade: dict, now: int, environment: str, risk_snapshot: dict | None = None) -> None:
    risk_snapshot = risk_snapshot or {}
    trade.setdefault("policy_version", risk_snapshot.get("policy_version", "gate@unknown"))
    trade.setdefault("policy_hash", risk_snapshot.get("policy_hash", "unknown"))
    DECISIONS.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    history_item = {
        "generated_at_ms": now,
        "environment": environment,
        "trade": trade,
        "policy_version": risk_snapshot.get("policy_version", "gate@unknown"),
        "policy_hash": risk_snapshot.get("policy_hash", "unknown"),
        "risk_snapshot": risk_snapshot,
        "decisions": {
            symbol: {
                "action": envelope["decision"].get("action", "WAIT"),
                "raw_action": envelope["decision"].get("raw_action", envelope["decision"].get("action", "WAIT")),
                "confidence": envelope["decision"].get("confidence", 0),
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
    return headline + "\nAI决策 | " + " | ".join(decision_parts)
SYMBOLS = ("BTC_USDT", "ETH_USDT", "SOL_USDT", "DOGE_USDT", "SUI_USDT", "XRP_USDT")

GATE_LIMIT_PRICE_DEVIATION = 0.02


def _quote_deviation(entry_price: float, reference_price: float) -> float:
    if entry_price <= 0 or reference_price <= 0:
        return 0.0
    return abs(entry_price - reference_price) / reference_price


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
    """Map Gate-native fields into the original OKX strategy package contract."""
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


def _round_price(value: float, step: str | float) -> str:
    """Round a trigger to Gate's contract price unit without float drift."""
    quantum = Decimal(str(step or "0"))
    if quantum <= 0:
        return f"{value:.8g}"
    rounded = (Decimal(str(value)) / quantum).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * quantum
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
    for row in rows or []:
        initial = row.get("initial") or {}
        trigger = row.get("trigger") or {}
        covered = abs(Decimal(str(initial.get("size") or 0)))
        required = abs(Decimal(str(size)))
        if str(initial.get("text") or "") == client_id and covered >= required and int(trigger.get("rule") or 0) == rule:
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
    private_context = {"account": {}, "positions": [], "pending_orders": [], "protections": [], "private_errors": []}
    try:
        private_context["account"] = client.account() or {}
        private_context["positions"] = client.positions() or []
        private_context["pending_orders"] = client.open_orders() or []
        private_context["protections"] = client.protection_orders() or []
    except Exception as exc:
        private_context["private_errors"].append({"category": classify_error(exc), "error": f"{type(exc).__name__}: {exc}"})
    reconciliation = reconcile_exchange_state(
        private_context["positions"], private_context["pending_orders"], private_context["protections"],
        max_pending_age_seconds=settings.max_pending_order_age_seconds,
    )
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
            available = float(private_context["account"].get("available") or 0)
            committed_margin = _account_committed_margin(private_context["account"])
            system_prompt, prompt = build_prompt(strategy_packages, positions=active_positions_detail, pending_orders=pending_orders_detail, available_usdt=available, execution_leverage=settings.leverage, max_order_margin_usdt=settings.max_order_margin_usd, max_total_margin_usdt=settings.max_total_margin_usd, current_margin_usdt=committed_margin, risk_snapshot=risk_snapshot)
        except Exception:
            prompt = ('Output ONLY one compact final JSON object with top-level keys decisions, position_management, pending_orders_management. '
                      f'The decisions object must contain all six contracts and each item must include action BUY_LONG|SELL_SHORT|WAIT, confidence 0-100, entry_price, take_profit_price, stop_loss_price, leverage={settings.leverage:g}, margin_usdt (positive and <= {settings.max_order_margin_usd:.2f}), summary_reason. '
                      f'Active Gate risk profile={risk_profile.label}: confidence>={risk_profile.min_confidence:g}%, DOGE>={risk_profile.doge_min_confidence:g}%, ADX>={risk_profile.min_adx:g}, R:R>={risk_profile.min_rr:g}, target R:R>={risk_profile.target_rr:g}, effective per-order margin cap={settings.max_order_margin_usd * risk_profile.margin_ratio:.2f} USDT, total margin cap={settings.max_total_margin_usd:.2f} USDT. Different Gate contracts may be held concurrently and an active contract may be added to only in the same direction, subject to aggregate limits and complete TP/SL coverage. '
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
            decisions[feature["contract"]] = {"action": candidate, "raw_action": candidate, "confidence": max(0.0, min(100.0, float(item.get("confidence") or 0))), "entry_price": float(item.get("entry_price") or feature["last"]) if candidate != "WAIT" else 0.0, "take_profit_price": float(item.get("take_profit_price") or 0), "stop_loss_price": float(item.get("stop_loss_price") or 0), "leverage": settings.leverage, "margin_usdt": accepted_margin, "margin_usd": accepted_margin, "summary_reason": str(item.get("summary_reason") or "LLM decision")}
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
    # Apply the same deterministic quote gate used by the original OKX
    # execution chain before selecting any candidate for execution.
    try:
        from .strategy_adapter import validate_decision
        active_positions = {str(p.get("contract")): p for p in private_context["positions"] if float(p.get("size") or 0) != 0}
        active_sides = {symbol: "long" if float(position.get("size") or 0) > 0 else "short" for symbol, position in active_positions.items()}
        for symbol, envelope in decisions_payload.items():
            item = envelope["decision"]
            if item.get("action") == "WAIT":
                continue
            # Gate output may use the OKX council aliases; normalize them here.
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
    result: dict = {"trade": {"status": "not_submitted", "reason": f"Gate {settings.environment.title()} automatic execution is disabled" if not execution_enabled else f"No qualifying {settings.environment.title()} signal"}}
    executable = [d for d in decisions_payload.values() if d["decision"].get("action") in {"BUY_LONG", "SELL_SHORT"} and float(d["decision"].get("confidence") or 0) >= risk_profile.min_confidence]
    decision = max(executable or decisions_payload.values(), key=lambda d: float(d["decision"].get("confidence") or 0))
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
    symbol_cooldown = cooldown_state(LEDGER, cooldown_seconds=settings.stop_cooldown_seconds, contract=trade_symbol)
    if execution_enabled and safety_status["safe_for_new_risk"] and not symbol_cooldown["active"] and llm_source == "gate-multifactor-llm" and action != "WAIT" and confidence >= risk_profile.min_confidence:
        limits = RiskLimits(settings.max_position_notional_usd, settings.max_total_margin_usd, settings.max_order_margin_usd)
        contract = client.contracts(trade_symbol)
        multiplier = float(contract.get("quanto_multiplier") or 0)
        entry_price = float(decision["decision"].get("entry_price") or 0)
        planned_margin = float(decision["decision"].get("margin_usdt") or decision["decision"].get("margin_usd") or 0)
        leverage = settings.leverage
        if planned_margin <= 0:
            result["trade"] = {"status": "blocked_invalid_margin", "contract": trade_symbol, "reason": "AI did not provide a positive margin_usdt; no order was submitted"}
            result["trade"].setdefault("policy_version", policy_snapshot["policy_version"])
            result["trade"].setdefault("policy_hash", policy_snapshot["policy_hash"])
            payload = {**decisions_payload, "generated_at_ms": now, "exchange": "gate", "environment": settings.environment, "policy_snapshot": policy_snapshot, "risk_snapshot": risk_snapshot, "position_management": position_management, "pending_orders_management": pending_management, "private_context": private_context, "trade": result["trade"]}
            _save_decision_payload(payload, decisions_payload, result["trade"], now, settings.environment, risk_snapshot)
            result["decisions"] = decisions_payload
            return result
        leverage_min = float(contract.get("leverage_min") or 1)
        leverage_max = float(contract.get("leverage_max") or 0)
        if leverage < leverage_min or (leverage_max > 0 and leverage > leverage_max):
            raise RuntimeError(f"Configured Gate leverage {leverage:g}x is outside contract range {leverage_min:g}x-{leverage_max:g}x")
        try:
            contracts = _order_size_for_margin(margin_usdt=planned_margin, leverage=leverage, entry_price=entry_price, multiplier=multiplier, minimum=contract.get("order_size_min") or 1, maximum=contract.get("order_size_max") or 0, enable_decimal=bool(contract.get("enable_decimal")))
        except ValueError as exc:
            result["trade"] = {"status": "blocked_invalid_size", "contract": trade_symbol, "reason": str(exc), "planned_margin_usdt": planned_margin, "configured_leverage": leverage}
            payload = {**decisions_payload, "generated_at_ms": now, "exchange": "gate", "environment": settings.environment, "risk_snapshot": risk_snapshot, "position_management": position_management, "pending_orders_management": pending_management, "private_context": private_context, "trade": result["trade"]}
            _save_decision_payload(payload, decisions_payload, result["trade"], now, settings.environment, risk_snapshot)
            result["decisions"] = decisions_payload
            return result
        size = contracts if action == "BUY_LONG" else -contracts
        order_notional = float(abs(size) * Decimal(str(multiplier)) * Decimal(str(entry_price)))
        estimated_margin = order_notional / leverage
        account = client.account()
        positions = private_context["positions"]
        existing_position = next(
            (p for p in positions or [] if str(p.get("contract") or "").upper() == trade_symbol and float(p.get("size") or 0) != 0),
            None,
        )
        existing_size = Decimal(str((existing_position or {}).get("size") or 0))
        is_add_on = existing_size != 0
        if is_add_on and ((existing_size > 0) != (size > 0)):
            result["trade"] = {"status": "blocked_opposite_position", "contract": trade_symbol, "reason": "Existing Gate position direction conflicts with the proposed add-on"}
            payload = {**decisions_payload, "generated_at_ms": now, "exchange": "gate", "environment": settings.environment, "risk_snapshot": risk_snapshot, "position_management": position_management, "pending_orders_management": pending_management, "private_context": private_context, "trade": result["trade"]}
            _save_decision_payload(payload, decisions_payload, result["trade"], now, settings.environment, risk_snapshot)
            result["decisions"] = decisions_payload
            return result
        if is_add_on:
            existing_protections = client.protection_orders(trade_symbol) or []
            existing_coverage = protection_coverage_status(existing_protections, existing_size)
            if not existing_coverage["fully_protected"]:
                result["trade"] = {
                    "status": "blocked_add_unprotected_existing_position",
                    "contract": trade_symbol,
                    "reason": "Existing Gate position is not fully covered by both native take-profit and stop-loss orders; add-on blocked fail-closed",
                    "required_size": GateFuturesClient._api_size(abs(existing_size)),
                    "take_profit_covered_size": GateFuturesClient._api_size(existing_coverage["take_profit"]),
                    "stop_loss_covered_size": GateFuturesClient._api_size(existing_coverage["stop_loss"]),
                }
                payload = {**decisions_payload, "generated_at_ms": now, "exchange": "gate", "environment": settings.environment, "risk_snapshot": risk_snapshot, "position_management": position_management, "pending_orders_management": pending_management, "private_context": private_context, "trade": result["trade"]}
                _save_decision_payload(payload, decisions_payload, result["trade"], now, settings.environment, risk_snapshot)
                result["decisions"] = decisions_payload
                return result
        # Multiple different Gate contracts may be held concurrently. Capacity
        # is governed by configured aggregate margin and notional limits.
        current_margin = _account_committed_margin(account)
        current_notional = _portfolio_position_notional(client, positions)
        risk = {"order_margin_usd": estimated_margin, "current_margin_usd": current_margin, "current_position_notional_usd": current_notional, "order_notional_usd": order_notional, "environment": settings.environment, "live_enabled": settings.live_trading_enabled}
        try:
            limits.check_order(**risk)
        except PermissionError as exc:
            result["trade"] = {
                "status": "blocked_risk_limit",
                "contract": trade_symbol,
                "reason": str(exc),
                "risk": risk,
            }
            payload = {**decisions_payload, "generated_at_ms": now, "exchange": "gate", "environment": settings.environment, "policy_snapshot": policy_snapshot, "risk_snapshot": risk_snapshot, "position_management": position_management, "pending_orders_management": pending_management, "private_context": private_context, "trade": result["trade"]}
            _save_decision_payload(payload, decisions_payload, result["trade"], now, settings.environment, risk_snapshot)
            result["decisions"] = decisions_payload
            return result
        try:
            from .strategy_adapter import validate_decision
            package = {**trade_feature, "instId": trade_symbol, "data_quality": "valid", "macro_4h": trade_feature.get("macro_4h", "RANGE")}
            active_positions = {str(p.get("contract")): p for p in positions or [] if float(p.get("size") or 0) != 0}
            active_sides = {symbol: "long" if float(position.get("size") or 0) > 0 else "short" for symbol, position in active_positions.items()}
            gated_action, gate_reason, rr = validate_decision(package, decision["decision"], active_inst_ids=set(active_positions), active_position_sides=active_sides, risk_snapshot=risk_snapshot)
            if gated_action != action:
                result["trade"] = {"status": "blocked_by_strategy_interceptor", "contract": trade_symbol, "reason": gate_reason or "Gate strategy interceptor rejected decision", "risk_reward": rr}
                action = "WAIT"
        except Exception as exc:
            result["trade"] = {"status": "blocked_by_strategy_interceptor_error", "contract": trade_symbol, "reason": str(exc)}
            action = "WAIT"
        if action == "WAIT":
            payload = {**decisions_payload, "generated_at_ms": now, "exchange": "gate", "environment": settings.environment, "risk_snapshot": risk_snapshot, "position_management": position_management, "pending_orders_management": pending_management, "private_context": private_context, "trade": result["trade"]}
            _save_decision_payload(payload, decisions_payload, result["trade"], now, settings.environment, risk_snapshot)
            result["decisions"] = decisions_payload
            return result
        quote_rows = client.tickers(trade_symbol) or []
        quote = quote_rows[0] if isinstance(quote_rows, list) and quote_rows else (quote_rows if isinstance(quote_rows, dict) else {})
        gate_reference = float(quote.get("mark_price") or quote.get("last") or last or 0)
        requested_entry = float(decision["decision"].get("entry_price") or 0)
        deviation = _quote_deviation(requested_entry, gate_reference)
        if deviation > GATE_LIMIT_PRICE_DEVIATION:
            reason = (f"Gate limit price deviation {deviation * 100:.3f}% exceeds "
                      f"{GATE_LIMIT_PRICE_DEVIATION * 100:.1f}% band "
                      f"(entry={requested_entry:g}, mark={gate_reference:g}); safe WAIT")
            decision["decision"].update({"action": "WAIT", "entry_price": 0.0, "take_profit_price": 0.0, "stop_loss_price": 0.0, "rejection_reason": reason})
            result["trade"] = {"status": "blocked_price_deviation", "contract": trade_symbol, "reason": reason, "requested_entry": requested_entry, "reference_price": gate_reference, "deviation_pct": deviation * 100}
            payload = {**decisions_payload, "generated_at_ms": now, "exchange": "gate", "environment": settings.environment, "risk_snapshot": risk_snapshot, "position_management": position_management, "pending_orders_management": pending_management, "private_context": private_context, "trade": result["trade"]}
            _save_decision_payload(payload, decisions_payload, result["trade"], now, settings.environment, risk_snapshot)
            result["decisions"] = decisions_payload
            return result
        client_id = f"t-gate-ai-{now}"
        try:
            position = client.positions(trade_symbol) or {}
        except RuntimeError as exc:
            if "POSITION_NOT_FOUND" not in str(exc):
                raise
            position = {}
        cross_margin = str(position.get("pos_margin_mode") or "cross").lower() == "cross" or float(position.get("leverage") or 0) == 0
        limit_price = _round_price(entry_price, contract.get("order_price_round") or contract.get("mark_price_round") or "0")
        try:
            leverage_result = client.update_position_leverage(contract=trade_symbol, leverage=leverage, cross_margin=cross_margin)
            order = GateTradingService(client, limits).place_order(contract=trade_symbol, size=size, price=limit_price, tif="gtc", client_id=client_id, risk=risk)
        except Exception as order_error:
            reason = f"Gate entry order rejected safely: {order_error}"
            decision["decision"].update({"action": "WAIT", "entry_price": 0.0, "take_profit_price": 0.0, "stop_loss_price": 0.0, "rejection_reason": reason})
            result["trade"] = {"status": "order_rejected_safe_wait", "contract": trade_symbol, "client_id": client_id, "reason": reason}
            payload = {**decisions_payload, "generated_at_ms": now, "exchange": "gate", "environment": settings.environment, "risk_snapshot": risk_snapshot, "position_management": position_management, "pending_orders_management": pending_management, "private_context": private_context, "trade": result["trade"]}
            _save_decision_payload(payload, decisions_payload, result["trade"], now, settings.environment, risk_snapshot)
            result["decisions"] = decisions_payload
            return result
        tp = float(decision["decision"].get("take_profit_price") or (last + 2 * trade_feature["atr14"] if size > 0 else last - 2 * trade_feature["atr14"]))
        sl = float(decision["decision"].get("stop_loss_price") or (last - trade_feature["atr14"] if size > 0 else last + trade_feature["atr14"]))
        close_size = -size
        tick = contract.get("order_price_round") or contract.get("mark_price_round") or "0"
        tp_order = None
        sl_order = None
        try:
            tp_order = client.create_protection_order(contract=trade_symbol, size=close_size, trigger_price=_round_price(tp, tick), rule=1 if size > 0 else 2, client_id=f"t-gate-tp-{now}")
            sl_order = client.create_protection_order(contract=trade_symbol, size=close_size, trigger_price=_round_price(sl, tick), rule=2 if size > 0 else 1, client_id=f"t-gate-sl-{now}")
            protections = client.protection_orders(trade_symbol)
            tp_rule = 1 if size > 0 else 2
            sl_rule = 2 if size > 0 else 1
            tp_covered = _protection_matches(protections, client_id=f"t-gate-tp-{now}", size=close_size, rule=tp_rule)
            sl_covered = _protection_matches(protections, client_id=f"t-gate-sl-{now}", size=close_size, rule=sl_rule)
            expected_position_size = existing_size + size
            total_coverage = protection_coverage_status(protections, expected_position_size)
            if not tp_covered or not sl_covered or not total_coverage["fully_protected"]:
                raise RuntimeError(f"Gate {settings.environment.title()} protection coverage verification failed (new_tp={tp_covered}, new_sl={sl_covered}, total={total_coverage['fully_protected']})")
            result["trade"] = {"status": f"submitted_{settings.environment}", "environment": settings.environment, "contract": trade_symbol, "client_id": client_id, "position_operation": "add" if is_add_on else "open", "order_type": "limit", "price": limit_price, "size": GateFuturesClient._api_size(size), "expected_position_size": GateFuturesClient._api_size(expected_position_size), "configured_leverage": leverage, "risk_snapshot": risk_snapshot, "estimated_margin_usdt": estimated_margin, "leverage_update": leverage_result, "order": order, "take_profit": tp_order, "stop_loss": sl_order, "protection_covered": True, "take_profit_covered": True, "stop_loss_covered": True}
            record_protection_intent(PROTECTION_INTENTS, {
                "environment": settings.environment, "contract": trade_symbol, "entry_client_id": client_id,
                "position_side": "long" if size > 0 else "short", "entry_size": str(abs(size)),
                "entry_price": limit_price,
                "take_profit_price": _round_price(tp, tick), "stop_loss_price": _round_price(sl, tick),
                "created_at_ms": now, "policy_version": policy_snapshot["policy_version"], "policy_hash": policy_snapshot["policy_hash"],
            })
        except Exception as protection_error:
            cleanup = {}
            try:
                raw_order = order.get("orders") if isinstance(order, dict) and order.get("reconciled") else order
                entry_order_id = str((raw_order or {}).get("id") or client_id)
                cleanup["entry_order"] = client.cancel_order(entry_order_id, trade_symbol)
            except Exception as cancel_error:
                cleanup["entry_order"] = f"cancel failed: {cancel_error}"
            for created in (tp_order, sl_order):
                raw_created = created.get("order") if isinstance(created, dict) and created.get("reconciled") else created
                if isinstance(raw_created, dict) and raw_created.get("id"):
                    try:
                        cleanup[str(raw_created["id"])] = client.cancel_protection_order(str(raw_created["id"]))
                    except Exception as cancel_error:
                        cleanup[str(raw_created["id"])] = f"cancel failed: {cancel_error}"
            try:
                cleanup["close_order"] = client.close_position(contract=trade_symbol, client_id=f"t-gate-close-{now}")
            except Exception as close_error:
                cleanup["close_order"] = f"close failed: {close_error}"
            result["trade"] = {"status": "protection_failed_flatten_attempted", "contract": trade_symbol, "client_id": client_id, "order": order, "error": str(protection_error), "cleanup": cleanup}
    if execution_enabled and symbol_cooldown["active"] and result["trade"].get("status") == "not_submitted":
        result["trade"] = {"status": "blocked_symbol_cooldown", "contract": trade_symbol, "reason": f"{trade_symbol} is in post-stop cooldown; other contracts remain eligible", "cooldown": symbol_cooldown}
    elif execution_enabled and not safety_status["safe_for_new_risk"] and result["trade"].get("status") == "not_submitted":
        result["trade"] = {"status": "blocked_safety_fail_closed", "reason": "Gate reconciliation or daily-loss gate blocked new risk", "safety_status": safety_status}
    result["trade"].setdefault("policy_version", policy_snapshot["policy_version"])
    result["trade"].setdefault("policy_hash", policy_snapshot["policy_hash"])
    payload = {**decisions_payload, "generated_at_ms": now, "exchange": "gate", "environment": settings.environment, "policy_snapshot": policy_snapshot, "risk_snapshot": risk_snapshot, "safety_status": safety_status, "position_management": position_management, "pending_orders_management": pending_management, "private_context": private_context, "trade": result["trade"]}
    _save_decision_payload(payload, decisions_payload, result["trade"], now, settings.environment, risk_snapshot)
    result["decisions"] = decisions_payload
    return result


if __name__ == "__main__":
    cycle_result = run_cycle()
    print(_console_summary(cycle_result))
