from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class RiskLimits:
    max_position_notional_usd: float
    max_total_margin_usd: float
    max_order_margin_usd: float

    def check_order(self, *, order_margin_usd: float, current_margin_usd: float, current_position_notional_usd: float, order_notional_usd: float, environment: str, live_enabled: bool) -> None:
        if environment == "live" and not live_enabled:
            raise PermissionError("Live trading disabled (fail-closed)")
        if order_margin_usd <= 0:
            raise ValueError("order margin must be positive")
        if self.max_order_margin_usd <= 0 or order_margin_usd > self.max_order_margin_usd:
            raise PermissionError("order margin exceeds configured limit")
        if self.max_total_margin_usd <= 0 or current_margin_usd + order_margin_usd > self.max_total_margin_usd:
            raise PermissionError("total margin exceeds configured limit")
        if self.max_position_notional_usd <= 0 or current_position_notional_usd + abs(order_notional_usd) > self.max_position_notional_usd:
            raise PermissionError("position notional exceeds configured limit")
