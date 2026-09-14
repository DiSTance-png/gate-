"""Reuse the existing R20 decision chain behind a Gate-native data adapter.

This module deliberately exposes only pure strategy helpers. It never imports
exchange credentials, invokes a trading CLI, writes runtime files, or performs exchange IO.
"""
from __future__ import annotations

from typing import Any
from pathlib import Path

import scripts.ai_brain_trader as _r20_strategy
from scripts.ai_brain_trader import (
    SYSTEM_PROMPT,
    construct_full_market_prompt,
    get_effective_system_prompt,
    validate_and_filter_decision,
)

_GATE_DATA = Path(__file__).resolve().parents[1] / "data"


def _use_gate_strategy_inputs() -> None:
    """Keep the reused prompt code on Gate-owned memory/news/override files."""
    _r20_strategy.DATA_DIR = str(_GATE_DATA)
    _r20_strategy.AI_MEMORY_MD_FILE = str(_GATE_DATA / "AI_TRADING_MEMORY.md")
    _r20_strategy.AI_MEMORY_FILE = str(_GATE_DATA / "ai_trading_memory.json")
    _r20_strategy.NEWS_SENTIMENT_FILE = str(_GATE_DATA / "news_sentiment.json")
    _r20_strategy.PROMPT_OVERRIDE_FILE = str(_GATE_DATA / "system_prompt_override.txt")


def build_prompt(packages: list[dict[str, Any]], *, positions: list[dict[str, Any]], pending_orders: list[dict[str, Any]], available_usdt: float, execution_leverage: float = 3.0, max_order_margin_usdt: float = 0.0, max_total_margin_usdt: float = 0.0, current_margin_usdt: float = 0.0, risk_snapshot: dict[str, Any] | None = None) -> tuple[str, str]:
    _use_gate_strategy_inputs()
    prompt = construct_full_market_prompt(
        packages,
        pos_summary=f"Gate Futures active_positions={len(positions)}; available={available_usdt:.2f} USDT",
        active_positions_detail=positions,
        pending_orders_detail=pending_orders,
        usdt_available=available_usdt,
    )
    risk = risk_snapshot or {}
    effective_margin = max_order_margin_usdt * float(risk.get("margin_ratio", 1.0))
    gate_constraints = (
        f"\n\n【Gate 当前风险档位（本轮唯一有效配置）】{risk.get('label', risk.get('risk_profile', 'standard'))}；"
        f"配置版本={risk.get('risk_profile_version', 'unknown')}，哈希={risk.get('risk_profile_hash', 'unknown')}。"
        f"执行层固定使用 {execution_leverage:g}x 杠杆，AI 不得自行修改；"
        f"有效单笔保证金上限={effective_margin:.2f} USDT（系统绝对上限 {max_order_margin_usdt:.2f} USDT）；"
        f"账户累计保证金上限={max_total_margin_usdt:.2f} USDT，当前已占用={current_margin_usdt:.2f} USDT；"
        "允许不同 Gate 合约同时持仓，也允许已有合约沿原方向受控加仓；禁止反向开仓，所有加仓仍受累计保证金、总敞口和双保护覆盖约束；"
        f"最低置信度={float(risk.get('min_confidence', 75)):g}%，DOGE={float(risk.get('doge_min_confidence', 80)):g}%；"
        f"最低 1H ADX={float(risk.get('min_adx', 18)):g}；执行 R:R 底线={float(risk.get('min_rr', 2.0)):g}，目标 R:R≥{float(risk.get('target_rr', 2.2)):g}；"
        f"结构止损参考 {float(risk.get('stop_atr_min', 1.8)):g}~{float(risk.get('stop_atr_max', 2.2)):g}x 1H ATR。"
        "这里的动态数值覆盖原模板中同类建议数值，但不能覆盖数据有效、4H 方向、价格几何、绝对 2R、保证金/仓位上限、双保护覆盖和超时查单等 P0 条件。"
        "entry_price 是 GTC 限价，不是市价。"
    )
    return (get_effective_system_prompt() or SYSTEM_PROMPT) + gate_constraints, prompt + gate_constraints


def validate_decision(package: dict[str, Any], decision: dict[str, Any], *, active_inst_ids: set[str], active_position_sides: dict[str, str], risk_snapshot: dict[str, Any] | None = None) -> tuple[str, str, float]:
    """Run the reused interceptor chain with Gate's immutable per-cycle risk context."""
    _use_gate_strategy_inputs()
    inst_id = str(package.get("instId") or "").upper().replace("-USDT-SWAP", "_USDT").replace("-", "_")
    normalized_active = {
        str(value).upper().replace("-USDT-SWAP", "_USDT").replace("-", "_")
        for value in active_inst_ids
    }
    normalized_sides = {
        str(key).upper().replace("-USDT-SWAP", "_USDT").replace("-", "_"): str(value).lower()
        for key, value in active_position_sides.items()
    }
    action = str(decision.get("action") or "WAIT").upper()
    if action != "WAIT" and inst_id in normalized_active:
        side = normalized_sides.get(inst_id, "")
        same_direction = (side == "long" and action == "BUY_LONG") or (side == "short" and action == "SELL_SHORT")
        if not same_direction:
            return "WAIT", "该 Gate 合约已有反向持仓，禁止借加仓通道反手；请先由持仓管理链平仓。", 0.0
    context = {
        "active_inst_ids": normalized_active,
        "active_position_sides": normalized_sides,
        "risk_context": dict(risk_snapshot or {}),
    }
    try:
        from r20_backend.interceptor_manager import run_interceptor_pipeline
        return run_interceptor_pipeline(package, decision, context)
    except Exception:
        return validate_and_filter_decision(package, decision, normalized_active, normalized_sides)
