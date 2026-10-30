"""Pure candidate construction for the optional contrarian shadow strategy.

The model's direction remains the source of truth for audit and learning.  This
module only derives a separate, immediately executable *hypothesis* from a
retracement signal.  It never talks to Gate and never authorizes an order.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


ZERO = Decimal("0")
VALID_MODES = {"off", "shadow", "testnet", "live"}
SUPPORTED_SHADOW_REGIMES = {"RANGE", "TRANSITION"}


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return ZERO
    return result if result.is_finite() else ZERO


def opposite_action(action: Any) -> str:
    normalized = str(action or "").strip().upper()
    if normalized == "BUY_LONG":
        return "SELL_SHORT"
    if normalized == "SELL_SHORT":
        return "BUY_LONG"
    return "WAIT"


def _side_for_action(action: str) -> str:
    return "long" if action == "BUY_LONG" else "short" if action == "SELL_SHORT" else ""


def _regime_name(feature: Mapping[str, Any], decision: Mapping[str, Any]) -> str:
    regime = feature.get("market_regime")
    if isinstance(regime, Mapping):
        return str(regime.get("name") or "").strip().upper()
    return str(regime or decision.get("market_regime") or "").strip().upper()


def _quote(feature: Mapping[str, Any], ticker: Mapping[str, Any], side: str) -> Decimal:
    ticker = ticker or {}
    if side == "long":
        values = (ticker.get("lowest_ask"), ticker.get("askPx"), feature.get("askPx"), feature.get("last"))
    else:
        values = (ticker.get("highest_bid"), ticker.get("bidPx"), feature.get("bidPx"), feature.get("last"))
    for value in values:
        price = _decimal(value)
        if price > ZERO:
            return price
    return ZERO


@dataclass(frozen=True)
class ContrarianCandidate:
    """Serializable shadow hypothesis; no field permits an exchange write."""

    strategy_mode: str
    contract: str
    raw_action: str
    effective_action: str
    raw_entry_price: str
    raw_take_profit_price: str
    raw_stop_loss_price: str
    entry_price: str | None
    take_profit_price: str | None
    stop_loss_price: str | None
    risk_distance: str
    take_profit_r: str
    stop_loss_r: str
    reward_to_risk: str
    entry_intent: str
    market_regime: str
    current_position_side: str
    eligible: bool
    reason: str
    enforceable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_candidate(
    decision: Mapping[str, Any],
    feature: Mapping[str, Any],
    ticker: Mapping[str, Any] | None = None,
    *,
    current_position_side: str = "",
    pending_entry: bool = False,
    mode: str = "shadow",
    take_profit_r: Any = "1.0",
    stop_loss_r: Any = "2.0",
    allowed_regimes: set[str] | frozenset[str] = SUPPORTED_SHADOW_REGIMES,
) -> dict[str, Any] | None:
    """Build a reverse retracement candidate without making an execution decision."""
    raw_action = str(decision.get("action") or decision.get("raw_action") or "").strip().upper()
    if raw_action not in {"BUY_LONG", "SELL_SHORT"}:
        return None
    if str(decision.get("entry_intent") or "").strip().lower() != "retracement":
        return None

    mode = str(mode or "off").strip().lower()
    effective_action = opposite_action(raw_action)
    effective_side = _side_for_action(effective_action)
    contract = str(feature.get("contract") or decision.get("contract") or "").strip().upper()
    raw_entry = _decimal(decision.get("entry_price"))
    raw_tp = _decimal(decision.get("take_profit_price"))
    raw_sl = _decimal(decision.get("stop_loss_price"))
    risk_distance = abs(raw_entry - raw_sl)
    tp_r = _decimal(take_profit_r)
    sl_r = _decimal(stop_loss_r)
    regime = _regime_name(feature, decision)
    current_side = str(current_position_side or "").strip().lower()
    entry = _quote(feature, ticker or {}, effective_side)
    reasons: list[str] = []
    if mode == "off":
        reasons.append("contrarian_mode_off")
    elif mode not in {"shadow", "testnet", "live"}:
        reasons.append("invalid_execution_mode")
    if not contract:
        reasons.append("invalid_contract")
    if raw_entry <= ZERO or raw_sl <= ZERO or risk_distance <= ZERO:
        reasons.append("invalid_raw_risk_distance")
    if raw_tp <= ZERO:
        reasons.append("invalid_raw_take_profit")
    if tp_r <= ZERO or sl_r <= ZERO:
        reasons.append("invalid_reward_risk_parameters")
    if entry <= ZERO:
        reasons.append("missing_executable_quote")
    if regime not in {str(value).upper() for value in allowed_regimes}:
        reasons.append("regime_not_eligible")
    if current_side in {"long", "short"}:
        reasons.append("active_position_blocks_new_entry")
    if pending_entry:
        reasons.append("pending_entry_blocks_new_entry")

    tp = entry + risk_distance * tp_r if effective_side == "long" else entry - risk_distance * tp_r
    sl = entry - risk_distance * sl_r if effective_side == "long" else entry + risk_distance * sl_r
    eligible = not reasons
    return ContrarianCandidate(
        strategy_mode="contrarian",
        contract=contract,
        raw_action=raw_action,
        effective_action=effective_action,
        raw_entry_price=str(raw_entry),
        raw_take_profit_price=str(raw_tp),
        raw_stop_loss_price=str(raw_sl),
        entry_price=str(entry) if entry > ZERO else None,
        take_profit_price=str(tp) if entry > ZERO and tp > ZERO else None,
        stop_loss_price=str(sl) if entry > ZERO and sl > ZERO else None,
        risk_distance=str(risk_distance),
        take_profit_r=str(tp_r),
        stop_loss_r=str(sl_r),
        reward_to_risk=str(tp_r / sl_r) if sl_r > ZERO else "0",
        entry_intent="immediate",
        market_regime=regime,
        current_position_side=current_side,
        eligible=eligible,
        reason="eligible_shadow_candidate" if eligible else ";".join(reasons),
        enforceable=bool(eligible and mode in {"testnet", "live"}),
    ).to_dict()
