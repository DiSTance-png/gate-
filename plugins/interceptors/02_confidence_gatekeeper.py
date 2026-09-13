"""
R20 物理拦截插件规范
====================
id: 02_confidence_gatekeeper
name: 高置信度质量门禁
version: 1.1.0
author: R20 Official
description: 置信度门槛由 Gate 本轮不可变风险快照提供。
tags: 置信度, 胜率优化, 官方预设
"""

def check_risk(package: dict, decision: dict, context: dict) -> tuple[bool, str]:
    action = str(decision.get("action", "WAIT")).upper()
    if action == "WAIT":
        return True, ""

    try:
        conf = float(decision.get("confidence", 0) or 0)
    except (ValueError, TypeError):
        conf = 0.0

    risk = context.get("risk_context") or {}
    minimum = float(risk.get("min_confidence", 75.0))
    doge_minimum = float(risk.get("doge_min_confidence", max(80.0, minimum)))
    name = str(package.get("name") or package.get("contract") or package.get("instId") or "").upper()
    if "DOGE" in name and conf < doge_minimum:
        return False, f"DOGE 高杂波标的置信度 {conf:.1f}% 未达当前档位 {doge_minimum:g}% 门禁，安全降级为 WAIT。"

    if conf < minimum:
        return False, f"置信度 {conf:.1f}% 低于当前档位 {minimum:g}% 门禁，安全降级为 WAIT。"

    return True, ""
