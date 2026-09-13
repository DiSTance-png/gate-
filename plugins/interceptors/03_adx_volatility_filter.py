"""
R20 物理拦截插件规范
====================
id: 03_adx_volatility_filter
name: 1H ADX 趋势强度门禁
version: 1.0.0
author: R20 Official
description: 1H ADX 门槛由 Gate 本轮不可变风险快照提供。
tags: 震荡过滤, ADX, 官方预设
"""

def check_risk(package: dict, decision: dict, context: dict) -> tuple[bool, str]:
    action = str(decision.get("action", "WAIT")).upper()
    if action == "WAIT":
        return True, ""

    try:
        adx = float(package.get("adx_1h", 0) or 0)
    except (ValueError, TypeError):
        adx = 0.0

    minimum = float((context.get("risk_context") or {}).get("min_adx", 18.0))
    if adx <= 0:
        return False, "1H ADX 数据缺失或无效，安全降级为 WAIT。"
    if adx < minimum:
        return False, f"1H ADX 趋势强度仅 {adx:.1f}，未达当前档位 {minimum:g}，安全降级为 WAIT。"

    return True, ""
