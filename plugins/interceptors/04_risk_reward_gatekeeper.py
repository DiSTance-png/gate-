"""
R20 物理拦截插件规范
====================
id: 04_risk_reward_gatekeeper
name: 动态真实盈亏比门禁
version: 1.0.0
author: R20 Official
description: 执行层真实 R:R 门槛由 Gate 风险快照提供，且永不低于绝对 2R。
tags: 盈亏比, 赔率保障, 官方预设
"""

def check_risk(package: dict, decision: dict, context: dict) -> tuple[bool, str]:
    action = str(decision.get("action", "WAIT")).upper()
    if action == "WAIT":
        return True, ""

    try:
        entry = float(decision.get("entry_price", 0) or 0)
        tp = float(decision.get("take_profit_price", 0) or 0)
        sl = float(decision.get("stop_loss_price", 0) or 0)
    except (ValueError, TypeError):
        return False, "订单价格几何参数缺失或非浮点数，安全降级为 WAIT。"

    rr = 0.0
    if action == "BUY_LONG" and entry > sl > 0 and tp > entry:
        rr = (tp - entry) / (entry - sl)
    elif action == "SELL_SHORT" and sl > entry > tp > 0:
        rr = (entry - tp) / (sl - entry)

    risk = context.get("risk_context") or {}
    minimum = max(2.0, float(risk.get("min_rr", 2.0)))
    if rr < minimum:
        return False, f"模型报价盈亏比 {rr:.2f}R 未满足当前档位 {minimum:g}R 门禁，执行层降级为 WAIT。"

    return True, ""
