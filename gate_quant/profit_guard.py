"""Pure C1a profit-guard calculations and durable state shape.

This module deliberately has no Gate client, scheduler, or write path.  It is
safe to use for observe/shadow sampling before an execution state machine is
enabled.  A later C1b worker must treat the returned stop as a candidate and
re-read the exchange state under the shared write lock before applying it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


ZERO = Decimal("0")


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return ZERO
    return result if result.is_finite() else ZERO


def favorable_move(*, side: str, entry_price: Any, mark_price: Any) -> Decimal:
    """Return positive price movement in the position's favorable direction."""
    entry = _decimal(entry_price)
    mark = _decimal(mark_price)
    normalized = str(side or "").strip().lower()
    if normalized in {"long", "多", "buy", "buy_long"}:
        return max(ZERO, mark - entry)
    if normalized in {"short", "空", "sell", "sell_short"}:
        return max(ZERO, entry - mark)
    return ZERO


def initial_risk_distance(*, side: str, entry_price: Any, initial_stop_price: Any) -> Decimal:
    """Compute immutable D0; callers must persist it after fill confirmation."""
    entry = _decimal(entry_price)
    stop = _decimal(initial_stop_price)
    normalized = str(side or "").strip().lower()
    distance = entry - stop if normalized in {"long", "多", "buy", "buy_long"} else stop - entry
    return distance if distance > ZERO else ZERO


def r_multiple(*, side: str, entry_price: Any, mark_price: Any, initial_risk_distance_value: Any) -> Decimal:
    distance = _decimal(initial_risk_distance_value)
    if distance <= ZERO:
        return ZERO
    return favorable_move(side=side, entry_price=entry_price, mark_price=mark_price) / distance


def cost_distance(*, remaining_cost: Any, remaining_size: Any, contract_multiplier: Any = 1) -> Decimal:
    """Convert remaining fee/slippage budget into a price distance."""
    cost = max(ZERO, _decimal(remaining_cost))
    quantity = abs(_decimal(remaining_size))
    multiplier = _decimal(contract_multiplier)
    denominator = quantity * multiplier
    if denominator <= ZERO:
        return ZERO
    return cost / denominator


def _candidate_price(*, side: str, entry_price: Any, distance: Decimal) -> Decimal:
    entry = _decimal(entry_price)
    normalized = str(side or "").strip().lower()
    if normalized in {"long", "多", "buy", "buy_long"}:
        return entry + distance
    if normalized in {"short", "空", "sell", "sell_short"}:
        return entry - distance
    return ZERO


def candidate_stage(*, mfe_r: Any, d0: Any, cost_distance_value: Any) -> str:
    """Return the v4 observe stage without making an exchange decision."""
    risk = _decimal(d0)
    mfe = max(ZERO, _decimal(mfe_r))
    if risk <= ZERO:
        return "UNMANAGED"
    cost_r = max(ZERO, _decimal(cost_distance_value)) / risk
    if mfe >= Decimal("1.5"):
        return "S3"
    if mfe >= Decimal("1.0"):
        return "S2"
    if mfe >= max(Decimal("0.6"), cost_r):
        return "S1"
    return "S0"


def profit_floor_candidate(*, side: str, entry_price: Any, d0: Any, mfe_r: Any,
                           remaining_cost: Any, remaining_size: Any,
                           contract_multiplier: Any = 1) -> dict[str, Any]:
    """Build a pure v4 candidate for observe/shadow reporting.

    The result is descriptive only.  It never checks live prices, ticks,
    trigger types, ownership, or current protection rows; C1b must do those
    checks immediately before any write.
    """
    risk = _decimal(d0)
    mfe = max(ZERO, _decimal(mfe_r))
    costs = cost_distance(remaining_cost=remaining_cost, remaining_size=remaining_size,
                          contract_multiplier=contract_multiplier)
    stage = candidate_stage(mfe_r=mfe, d0=risk, cost_distance_value=costs)
    if stage == "UNMANAGED" or risk <= ZERO:
        return {"stage": "UNMANAGED", "candidate_stop_price": None, "reason": "invalid_initial_risk"}
    normalized_side = str(side or "").strip().lower()
    is_long = normalized_side in {"long", "多", "buy", "buy_long"}
    if stage == "S0":
        distance = risk * (Decimal("-1") if is_long else Decimal("1"))
    elif stage == "S1":
        distance = risk * (Decimal("-0.25") if is_long else Decimal("0.25"))
    elif stage == "S2":
        distance = costs * (Decimal("1") if is_long else Decimal("-1"))
    else:
        distance = max(costs, risk * mfe * Decimal("0.50")) * (Decimal("1") if is_long else Decimal("-1"))
    price = _candidate_price(side=side, entry_price=entry_price, distance=distance)
    return {
        "stage": stage,
        "mfe_r": str(mfe),
        "d0": str(risk),
        "cost_distance": str(costs),
        "candidate_stop_price": str(price),
        "enforceable": False,
        "reason": "observe_only_v4",
    }


@dataclass
class ProfitGuardState:
    """Serializable C1a state; no field authorizes a Gate write by itself."""

    schema_version: int = 1
    environment: str = ""
    account_scope: str = ""
    contract: str = ""
    side: str = ""
    position_cycle_id: str = ""
    entry_client_id: str = ""
    entry_price: str = ""
    initial_stop_price: str = ""
    initial_risk_distance: str = ""
    initial_size: str = ""
    remaining_size: str = ""
    contract_multiplier: str = "1"
    mfe_r: str = "0"
    mfe_observed_at_ms: int = 0
    stage: str = "S0"
    status: str = "OBSERVE"
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ProfitGuardState":
        fields = {field.name for field in cls.__dataclass_fields__.values()}
        return cls(**{key: value[key] for key in fields if key in value})


def observe_state(state: ProfitGuardState, *, mark_price: Any, observed_at_ms: int,
                  remaining_cost: Any) -> dict[str, Any]:
    """Update only local observe state and return a serializable sample."""
    current_r = r_multiple(side=state.side, entry_price=state.entry_price,
                           mark_price=mark_price,
                           initial_risk_distance_value=state.initial_risk_distance)
    state.mfe_r = str(max(_decimal(state.mfe_r), current_r))
    state.mfe_observed_at_ms = max(int(state.mfe_observed_at_ms or 0), int(observed_at_ms or 0))
    sample = profit_floor_candidate(
        side=state.side, entry_price=state.entry_price,
        d0=state.initial_risk_distance, mfe_r=state.mfe_r,
        remaining_cost=remaining_cost, remaining_size=state.remaining_size,
        contract_multiplier=state.contract_multiplier,
    )
    state.stage = str(sample["stage"])
    return {**state.to_dict(), "mark_price": str(_decimal(mark_price)), "sample": sample}
