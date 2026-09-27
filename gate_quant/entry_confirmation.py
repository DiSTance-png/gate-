from __future__ import annotations

from typing import Any


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def evaluate_retracement_confirmation(
    pending: dict[str, Any], indicators: dict[str, Any], *, now_ms: int
) -> dict[str, Any]:
    """Advance a retracement candidate from touch to reclaim confirmation."""
    action = str(pending.get("action") or "").upper()
    entry = _number(pending.get("entry_price"))
    stop = _number(pending.get("stop_loss_price"))
    candles = indicators.get("recent_15m") or []
    if action not in {"BUY_LONG", "SELL_SHORT"} or entry <= 0 or not candles:
        return {"confirmed": False, "invalidated": False, "state": "WAITING_DATA", "signals": []}

    latest = candles[0] if isinstance(candles[0], (list, tuple)) and len(candles[0]) >= 4 else []
    previous = candles[1] if len(candles) > 1 and isinstance(candles[1], (list, tuple)) and len(candles[1]) >= 4 else []
    if not latest:
        return {"confirmed": False, "invalidated": False, "state": "WAITING_DATA", "signals": []}

    open_price, high, low, close = (_number(value) for value in latest[:4])
    previous_close = _number(previous[3]) if previous else open_price
    previous_low = _number(previous[2]) if previous else low
    previous_high = _number(previous[1]) if previous else high
    acceleration = _number((indicators.get("calculus") or {}).get("acceleration"))
    touched_before = bool(pending.get("touched_at_ms"))

    if action == "BUY_LONG":
        touched_now = low <= entry
        reclaimed = close > entry
        invalidated = stop > 0 and close <= stop
        signals = [
            name for name, passed in (
                ("bullish_close", close > open_price),
                ("acceleration_positive", acceleration > 0),
                ("close_progress", close > previous_close),
                ("higher_low", low > previous_low),
            ) if passed
        ]
    else:
        touched_now = high >= entry
        reclaimed = close < entry
        invalidated = stop > 0 and close >= stop
        signals = [
            name for name, passed in (
                ("bearish_close", close < open_price),
                ("acceleration_negative", acceleration < 0),
                ("close_progress", close < previous_close),
                ("lower_high", high < previous_high),
            ) if passed
        ]

    if invalidated:
        return {"confirmed": False, "invalidated": True, "state": "INVALIDATED", "signals": signals}

    touched = touched_before or touched_now
    if not touched:
        return {"confirmed": False, "invalidated": False, "state": "WAITING_TOUCH", "signals": signals}

    if touched_now and not touched_before:
        pending["touched_at_ms"] = now_ms
    if reclaimed and len(signals) >= 2:
        return {"confirmed": True, "invalidated": False, "state": "CONFIRMED", "signals": signals}
    return {"confirmed": False, "invalidated": False, "state": "WAITING_RECLAIM", "signals": signals}
