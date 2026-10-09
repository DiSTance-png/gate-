"""Shared deterministic safety checks for trade quotes and risk gates.
No configuration, no state, no price repair.
"""
from __future__ import annotations

import math
from typing import Any, Tuple


def validate_quote_geometry_and_rr(action: str, entry: Any, tp: Any, sl: Any, *, min_rr: float = 2.0) -> Tuple[bool, str, float]:
    """Validates that opening quote prices are positive, finite numbers satisfying
    action-specific geometry, and that the calculated risk-reward ratio meets the supplied floor.
    The default remains 2.0 for inherited/non-Gate callers.
    Returns (is_valid, failure_reason, rr_ratio).
    """
    raw_act = str(action or "").upper()
    if raw_act not in {"BUY_LONG", "SELL_SHORT"}:
        return False, f"不支持的开仓方向: {action}", 0.0

    try:
        e = float(entry)
        t = float(tp)
        s = float(sl)
    except (TypeError, ValueError, OverflowError):
        return False, "核心风控拦截：入场价、止盈价、止损价必须是有效数字", 0.0

    if not (math.isfinite(e) and math.isfinite(t) and math.isfinite(s)):
        return False, "核心风控拦截：入场价、止盈价、止损价必须是有限数值 (NaN/Inf 拒绝)", 0.0

    if e <= 0 or t <= 0 or s <= 0:
        return False, "核心风控拦截：入场价、止盈价、止损价必须大于 0", 0.0

    if raw_act == "BUY_LONG":
        if not (s < e < t):
            return False, f"核心风控拦截：买多几何不合法 (须 止损 {s} < 限价 {e} < 止盈 {t})", 0.0
        risk = e - s
        reward = t - e
    else:  # SELL_SHORT
        if not (t < e < s):
            return False, f"核心风控拦截：卖空几何不合法 (须 止盈 {t} < 限价 {e} < 止损 {s})", 0.0
        risk = s - e
        reward = e - t

    if risk <= 0:
        return False, "核心风控拦截：单笔承担风险必须大于 0", 0.0

    rr = reward / risk
    if not math.isfinite(rr):
        return False, "核心风控拦截：盈亏比计算异常", 0.0

    try:
        rr_floor = float(min_rr)
    except (TypeError, ValueError, OverflowError):
        return False, "核心风控拦截：盈亏比底线配置无效", rr
    if not math.isfinite(rr_floor) or rr_floor <= 0:
        return False, "核心风控拦截：盈亏比底线配置无效", rr

    if rr + 1e-9 < rr_floor:
        return False, f"核心风控拦截：盈亏比不足 {rr_floor:g} (当前 R:R = {rr:.2f}:1，底线 {rr_floor:g}:1)", rr

    return True, "", rr
