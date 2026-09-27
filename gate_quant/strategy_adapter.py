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
from scripts.prompt_library import active_profile, apply_module_layout
from .regime import market_regime_rejection

_GATE_DATA = Path(__file__).resolve().parents[1] / "data"

ANTI_CHASE_RSI_HIGH = 78.0
ANTI_CHASE_RSI_LOW = 22.0
ANTI_CHASE_VWAP_BIAS_PCT = 6.0
ANTI_CHASE_24H_MOVE_PCT = 18.0


def anti_chase_rejection(package: dict[str, Any], action: str) -> str:
    """Return a symmetric, deterministic rejection for exhausted entries."""
    try:
        rsi_1h = float(package.get("rsi_1h", 50) or 50)
        vwap_bias = float(package.get("vwap_bias", 0) or 0)
        change_24h = float(package.get("chg24h", 0) or 0)
        timeframes = (package.get("calculus") or {}).get("timeframes") or {}
        acceleration_15m = float((timeframes.get("15M") or {}).get("acceleration", 0) or 0)
    except (TypeError, ValueError):
        return "追涨杀跌门禁所需指标无效，安全降级为 WAIT。"

    if action == "BUY_LONG":
        if rsi_1h >= ANTI_CHASE_RSI_HIGH:
            return f"防追涨门禁：1H RSI {rsi_1h:.2f} 已达到 {ANTI_CHASE_RSI_HIGH:g}，禁止在超买区开多。"
        if vwap_bias >= ANTI_CHASE_VWAP_BIAS_PCT:
            return f"防追涨门禁：价格高于 VWAP {vwap_bias:.2f}%，超过 {ANTI_CHASE_VWAP_BIAS_PCT:g}% 上限。"
        if change_24h >= ANTI_CHASE_24H_MOVE_PCT and acceleration_15m < 0:
            return f"防追涨门禁：24H 已上涨 {change_24h:.2f}% 且 15M 加速度转负，禁止末端追多。"
    elif action == "SELL_SHORT":
        if rsi_1h <= ANTI_CHASE_RSI_LOW:
            return f"防杀跌门禁：1H RSI {rsi_1h:.2f} 已低于 {ANTI_CHASE_RSI_LOW:g}，禁止在超卖区开空。"
        if vwap_bias <= -ANTI_CHASE_VWAP_BIAS_PCT:
            return f"防杀跌门禁：价格低于 VWAP {abs(vwap_bias):.2f}%，超过 {ANTI_CHASE_VWAP_BIAS_PCT:g}% 下限。"
        if change_24h <= -ANTI_CHASE_24H_MOVE_PCT and acceleration_15m > 0:
            return f"防杀跌门禁：24H 已下跌 {abs(change_24h):.2f}% 且 15M 加速度转正，禁止末端追空。"
    return ""

def strip_legacy_risk_values(text: str) -> str:
    """Preserve strategy meaning while removing inherited fixed risk numbers."""
    replacements = {
        "宽止损隔绝杂波": "1. 宽止损隔绝杂波：止损必须位于有效结构外，具体 ATR、杠杆与保证金额度只使用本周期 Gate 风险预算。",
        "阶梯 1": "   - 锁盈阶梯由本周期 Gate 风险预算决定；未达到对应门槛时保持结构止损。",
        "阶梯 2": "",
        "阶梯 3": "",
        "峰值回撤硬止盈": "     ① 峰值回撤止盈：只按本周期 Gate 风险预算中的峰值与回撤门槛判断。",
        "动能耗散止盈": "     ② 动能耗散止盈：只按本周期 Gate 风险预算中的 ROI、Phi 与曲率门槛判断。",
        "置信度自信标定": "   - 置信度应如实反映形态质量，并通过本周期 Gate 风险预算门槛。",
        "预期止盈目标空间": "   - 止盈空间与盈亏比必须满足本周期 Gate 风险预算和绝对 2R 底线。",
        "只要形态达标且": "   - 形态达标且通过本周期 Gate 风险预算时应给出与证据一致的置信度。",
        "单笔保证金建议": "  - 保证金和杠杆只使用本周期 Gate 风险预算，不得自行扩大。",
        "底仓必须 ROI": "- 同向加仓必须先满足底仓盈利、保护覆盖和本周期 Gate 加仓门槛；最多追加 1 次。",
        "自主规划拟开仓/加仓保证金": "   - AI 可提出保证金建议，但最终值不得超过本周期 Gate 风险预算。",
        "【交易风格：全维度波段强化": "【交易风格：全维度波段强化】保留概率优势、多空对称、顺势与避免噪音止损原则；所有数值只使用本周期 Gate 风险预算。",
        "【全维度波段强化裁决偏好": "【全维度波段强化裁决偏好】保留概率优势、多空对称、顺势回调/反弹和避免噪音止损原则；所有数值只使用本周期 Gate 风险预算。",
    }
    output = []
    for line in str(text or "").splitlines():
        replacement = next((value for marker, value in replacements.items() if marker in line), None)
        if replacement is None:
            output.append(line)
        elif replacement:
            output.append(replacement)
    return "\n".join(output).strip()


def build_gate_risk_budget(
    risk: dict[str, Any], *, execution_leverage: float, max_order_margin_usdt: float,
    max_total_margin_usdt: float, current_margin_usdt: float,
) -> str:
    effective_margin = max_order_margin_usdt * float(risk.get("margin_ratio", 1.0))
    lock = risk.get("profit_lock") or {}
    dissipation_join = "且" if lock.get("dissipation_requires_all") else "或"
    return (
        f"【Gate 当前周期唯一风险预算】档位={risk.get('label', risk.get('risk_profile', 'standard'))}；"
        f"配置版本={risk.get('risk_profile_version', 'unknown')}，哈希={risk.get('risk_profile_hash', 'unknown')}。"
        f"执行层固定使用 {execution_leverage:g}x 杠杆，AI 不得自行修改；"
        f"有效单笔保证金上限={effective_margin:.2f} USDT（系统绝对上限 {max_order_margin_usdt:.2f} USDT）；"
        f"账户累计保证金上限={max_total_margin_usdt:.2f} USDT，当前已占用={current_margin_usdt:.2f} USDT；"
        f"最低置信度={float(risk.get('min_confidence', 75)):g}%，DOGE={float(risk.get('doge_min_confidence', 80)):g}%；"
        f"最低 1H ADX={float(risk.get('min_adx', 18)):g}；执行 R:R 底线={float(risk.get('min_rr', 2.0)):g}，目标 R:R≥{float(risk.get('target_rr', 2.2)):g}；"
        f"结构止损参考 {float(risk.get('stop_atr_min', 1.8)):g}~{float(risk.get('stop_atr_max', 2.2)):g}x 1H ATR。"
        f"锁盈：达到 {float(lock.get('breakeven_r', 1.0)):g}R 或 ROI {float(lock.get('breakeven_roi_pct', 1.8)):g}% 时移至保本；"
        f"达到 {float(lock.get('second_lock_r', 1.8)):g}R 或 ROI {float(lock.get('second_lock_roi_pct', 3.2)):g}% 时至少锁定 {float(lock.get('locked_profit_r', 0.75)):g}R；"
        f"峰值达到 ROI {float(lock.get('peak_exit_roi_pct', 3.0)):g}% 或 {float(lock.get('peak_exit_r', 1.3)):g}R 后，回撤 {float(lock.get('peak_drawdown_min_pct', 40)):g}%~{float(lock.get('peak_drawdown_max_pct', 48)):g}% 才考虑退出；"
        f"动能耗散要求 ROI≥{float(lock.get('dissipation_roi_pct', 2.0)):g}%，并满足 Phi<{float(lock.get('dissipation_phi', -0.13)):g} {dissipation_join} 曲率≥{float(lock.get('dissipation_curvature', 1.6)):g}。"
    )


def _use_gate_strategy_inputs() -> None:
    """Keep the reused prompt code on Gate-owned memory/news/override files."""
    _r20_strategy.DATA_DIR = str(_GATE_DATA)
    _r20_strategy.AI_MEMORY_MD_FILE = str(_GATE_DATA / "AI_TRADING_MEMORY.md")
    _r20_strategy.AI_MEMORY_FILE = str(_GATE_DATA / "ai_trading_memory.json")
    _r20_strategy.NEWS_SENTIMENT_FILE = str(_GATE_DATA / "news_sentiment.json")
    _r20_strategy.PROMPT_OVERRIDE_FILE = str(_GATE_DATA / "system_prompt_override.txt")


def build_prompt(packages: list[dict[str, Any]], *, positions: list[dict[str, Any]], pending_orders: list[dict[str, Any]], available_usdt: float, execution_leverage: float = 3.0, max_order_margin_usdt: float = 0.0, max_total_margin_usdt: float = 0.0, current_margin_usdt: float = 0.0, risk_snapshot: dict[str, Any] | None = None, entry_intent_enabled: bool = False) -> tuple[str, str]:
    _use_gate_strategy_inputs()
    prompt = construct_full_market_prompt(
        packages,
        pos_summary=f"Gate Futures active_positions={len(positions)}; available={available_usdt:.2f} USDT",
        active_positions_detail=positions,
        pending_orders_detail=pending_orders,
        usdt_available=available_usdt,
    )
    risk = risk_snapshot or {}
    entry_contract = (
        "每个非 WAIT 决策必须增加 entry_intent，且只能为 immediate、retracement、breakout。"
        "immediate 表示按当前盘口立即入场，entry_price 必须接近当前盘口；"
        "retracement 表示做多等待低于卖一的回调、做空等待高于买一的反弹；"
        "breakout 表示做多等待上破高于卖一的触发价、做空等待下破低于买一的触发价。"
        "只表达入场意图，不得自行决定滑点、有效期或绕过风控。"
        if entry_intent_enabled else
        "entry_price 按既有 Gate GTC 限价契约解释。"
    )
    gate_constraints = (
        "\n\n" + build_gate_risk_budget(
            risk, execution_leverage=execution_leverage, max_order_margin_usdt=max_order_margin_usdt,
            max_total_margin_usdt=max_total_margin_usdt, current_margin_usdt=current_margin_usdt,
        ) +
        "允许不同 Gate 合约同时持仓，也允许已有合约沿原方向受控加仓；禁止反向开仓，所有加仓仍受累计保证金、总敞口和双保护覆盖约束；"
        "本周期只允许使用以上动态数值；不得恢复继承模板中的历史固定金额、杠杆、门槛或锁盈阈值。"
        "任何动态配置都不能覆盖数据有效、4H 方向、价格几何、绝对 2R、保证金/仓位上限、双保护覆盖和超时查单等 P0 条件。"
        f"防追涨杀跌 P0：BUY_LONG 时 1H RSI 必须低于 {ANTI_CHASE_RSI_HIGH:g} 且 VWAP 正偏离必须低于 {ANTI_CHASE_VWAP_BIAS_PCT:g}%；"
        f"SELL_SHORT 时 1H RSI 必须高于 {ANTI_CHASE_RSI_LOW:g} 且 VWAP 负偏离绝对值必须低于 {ANTI_CHASE_VWAP_BIAS_PCT:g}%；"
        f"24H 涨跌绝对值达到 {ANTI_CHASE_24H_MOVE_PCT:g}% 后，若 15M 加速度朝反转方向变化则必须 WAIT。"
        "每个决策必须区分 direction_bias 与 action：direction_bias 只允许 LONG、SHORT、NEUTRAL；"
        "即使中期方向偏多或偏空，只要当前入场未确认，action 必须为 WAIT。"
        "4H 方向只限定研究方向，不构成立即入场理由；ADX 只代表强度，必须结合 +DI/-DI 判断方向。"
        "PULLBACK/REBOUND 不是入场信号；TRANSITION 或 EXHAUSTION 状态必须 WAIT。"
        "不得为了通过置信度门槛抬高置信度，也不得因存在候选而强制开单。"
        + entry_contract
    )
    profile = active_profile()
    base_system = strip_legacy_risk_values(SYSTEM_PROMPT)
    laid_out_system = strip_legacy_risk_values(apply_module_layout(
        base_system, profile, "trading_system", f"{profile.get('name', '稳健')}交易系统提示词模板"
    ))
    effective_system = get_effective_system_prompt(laid_out_system)
    return effective_system + gate_constraints, strip_legacy_risk_values(prompt) + gate_constraints


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
    regime_reason = market_regime_rejection(package, action) if action != "WAIT" else ""
    if regime_reason:
        return "WAIT", regime_reason, 0.0
    anti_chase_reason = anti_chase_rejection(package, action)
    if anti_chase_reason:
        return "WAIT", anti_chase_reason, 0.0
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
