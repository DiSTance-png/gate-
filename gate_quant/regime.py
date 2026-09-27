from __future__ import annotations

from typing import Any


REGIME_VERSION = "gate-regime-v1"


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _probabilities(package: dict[str, Any], timeframe: str) -> tuple[float, float]:
    calculus = package.get("calculus") or {}
    if timeframe == "15M":
        frame = ((calculus.get("timeframes") or {}).get("15M") or calculus)
    else:
        frame = ((calculus.get("timeframes") or {}).get("1H") or package.get("calculus_1h") or {})
    probabilities = frame.get("probability_theory") or {}
    return (
        _number(probabilities.get("continuation_prob_pct"), 50.0),
        _number(probabilities.get("breakdown_prob_pct"), 50.0),
    )


def normalize_direction_bias(value: Any, action: str) -> str:
    aliases = {
        "BUY_LONG": "LONG",
        "LONG": "LONG",
        "BULL": "LONG",
        "SELL_SHORT": "SHORT",
        "SHORT": "SHORT",
        "BEAR": "SHORT",
        "WAIT": "NEUTRAL",
        "NONE": "NEUTRAL",
        "NEUTRAL": "NEUTRAL",
    }
    normalized = aliases.get(str(value or "").strip().upper())
    if normalized:
        return normalized
    return aliases.get(str(action or "WAIT").strip().upper(), "NEUTRAL")


def classify_market_regime(package: dict[str, Any]) -> dict[str, Any]:
    """Classify the current market state without relying on the LLM."""
    macro = str(package.get("macro_4h") or "4H_MACRO_RANGE").upper()
    structure = str(package.get("structure_1h") or "CHOP").upper()
    acceleration = _number(
        package.get("acceleration_1h"),
        _number((((package.get("calculus") or {}).get("timeframes") or {}).get("1H") or {}).get("acceleration")),
    )
    plus_di = _number(package.get("plus_di_1h"))
    minus_di = _number(package.get("minus_di_1h"))
    vwap_bias = _number(package.get("vwap_bias_pct"), _number(package.get("vwap_bias")))
    rsi_15m = _number(package.get("rsi14"), _number(package.get("rsi_15m"), 50.0))
    change_24h = _number(package.get("change_24h_pct"), _number(package.get("chg24h")))
    continuation_15m, breakdown_15m = _probabilities(package, "15M")
    continuation_1h, breakdown_1h = _probabilities(package, "1H")
    reasons: list[str] = []

    extended_up = vwap_bias >= 4.5 and (rsi_15m >= 68.0 or change_24h >= 8.0)
    extended_down = vwap_bias <= -4.5 and (rsi_15m <= 32.0 or change_24h <= -8.0)
    if extended_up:
        reasons.append("price is extended above VWAP after a material rise")
        name = "EXHAUSTION_UP"
    elif extended_down:
        reasons.append("price is extended below VWAP after a material decline")
        name = "EXHAUSTION_DOWN"
    else:
        macro_bull = "BULL" in macro
        macro_bear = "BEAR" in macro
        di_available = plus_di > 0 or minus_di > 0
        long_transition = macro_bull and (
            structure == "CHOP"
            or (acceleration < 0 and (
                breakdown_15m >= continuation_15m
                or continuation_1h - breakdown_1h < 15.0
            ))
            or (di_available and minus_di >= plus_di)
        )
        short_transition = macro_bear and (
            structure == "CHOP"
            or (acceleration > 0 and (
                continuation_15m >= breakdown_15m
                or breakdown_1h - continuation_1h < 15.0
            ))
            or (di_available and plus_di >= minus_di)
        )
        if long_transition or short_transition:
            reasons.append("higher-timeframe direction conflicts with current structure or momentum")
            name = "TRANSITION"
        elif macro_bull and structure == "BULL" and acceleration < 0:
            reasons.append("uptrend is pulling back and still requires entry confirmation")
            name = "PULLBACK_UP"
        elif macro_bear and structure == "BEAR" and acceleration > 0:
            reasons.append("downtrend is rebounding and still requires entry confirmation")
            name = "REBOUND_DOWN"
        elif macro_bull and structure == "BULL" and (not di_available or plus_di > minus_di):
            reasons.append("4H and 1H directions are aligned upward")
            name = "TREND_UP"
        elif macro_bear and structure == "BEAR" and (not di_available or minus_di > plus_di):
            reasons.append("4H and 1H directions are aligned downward")
            name = "TREND_DOWN"
        elif "RANGE" in macro:
            reasons.append("4H is range-bound")
            name = "RANGE"
        else:
            reasons.append("directional evidence is incomplete or mixed")
            name = "TRANSITION"

    return {
        "name": name,
        "version": REGIME_VERSION,
        "reasons": reasons,
        "metrics": {
            "acceleration_1h": acceleration,
            "plus_di_1h": plus_di,
            "minus_di_1h": minus_di,
            "vwap_bias_pct": vwap_bias,
            "rsi_15m": rsi_15m,
            "change_24h_pct": change_24h,
            "continuation_15m": continuation_15m,
            "breakdown_15m": breakdown_15m,
            "continuation_1h": continuation_1h,
            "breakdown_1h": breakdown_1h,
        },
    }


def market_regime_rejection(package: dict[str, Any], action: str) -> str:
    regime = package.get("market_regime")
    if not isinstance(regime, dict):
        # Legacy callers may not yet carry the deterministic classification.
        # Do not manufacture a hard rejection from an incomplete package.
        return ""
    name = str(regime.get("name") or "TRANSITION")
    if name == "TRANSITION":
        return "行情状态门禁：多周期结构与动量处于转换期，禁止新开仓。"
    if action == "BUY_LONG" and name == "EXHAUSTION_UP":
        return "行情状态门禁：价格处于上涨衰竭延展区，禁止继续追多。"
    if action == "SELL_SHORT" and name == "EXHAUSTION_DOWN":
        return "行情状态门禁：价格处于下跌衰竭延展区，禁止继续追空。"
    return ""
