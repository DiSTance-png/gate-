from __future__ import annotations
from .client import GateFuturesClient, AmbiguousOrderError
from .risk import RiskLimits
from decimal import Decimal


def protection_coverage_status(protections: list[dict], position_size: int | float | Decimal) -> dict:
    """Measure Gate-native TP and SL coverage independently for one net position."""
    signed_position = Decimal(str(position_size))
    required = abs(signed_position)
    if required == 0:
        return {"take_profit": Decimal("0"), "stop_loss": Decimal("0"), "required": required, "fully_protected": True}
    take_profit_rule = 1 if signed_position > 0 else 2
    stop_loss_rule = 2 if signed_position > 0 else 1
    expected_close_sign = -1 if signed_position > 0 else 1
    coverage = {take_profit_rule: Decimal("0"), stop_loss_rule: Decimal("0")}
    for protection in protections or []:
        initial = protection.get("initial") or {}
        trigger = protection.get("trigger") or {}
        try:
            rule = int(trigger.get("rule") or 0)
            size = Decimal(str(initial.get("size") or 0))
        except (TypeError, ValueError):
            continue
        if rule not in coverage:
            continue
        if bool(initial.get("close")):
            coverage[rule] = required
        elif size != 0 and (1 if size > 0 else -1) == expected_close_sign:
            coverage[rule] += abs(size)
    take_profit = coverage[take_profit_rule]
    stop_loss = coverage[stop_loss_rule]
    return {
        "take_profit": take_profit,
        "stop_loss": stop_loss,
        "required": required,
        "fully_protected": take_profit >= required and stop_loss >= required,
    }


class GateTradingService:
    def __init__(self, client: GateFuturesClient, limits: RiskLimits):
        self.client, self.limits = client, limits

    def place_order(self, **kwargs):
        self.limits.check_order(**kwargs.pop("risk"))
        try:
            return self.client.create_order(**kwargs)
        except AmbiguousOrderError:
            client_id = kwargs["client_id"]
            contract = kwargs["contract"]
            found = self.client.find_by_client_id(client_id, contract)
            if found:
                return {"reconciled": True, "orders": found}
            raise

    def verify_protection_coverage(self, contract: str, position_size: int | float | Decimal) -> bool:
        protections = self.client.protection_orders(contract)
        return bool(protection_coverage_status(protections, position_size)["fully_protected"])

    def close_position_safely(self, *, contract: str, client_id: str):
        try:
            return self.client.close_position(contract=contract, client_id=client_id)
        except AmbiguousOrderError:
            found = self.client.find_by_client_id(client_id, contract)
            if found:
                return {"reconciled": True, "order": found}
            raise

    def cancel_order_confirmed(self, *, contract: str, order_id: str) -> dict:
        result = self.client.cancel_order(order_id, contract)
        remaining = self.client.open_orders(contract) or []
        if any(str(row.get("id") or "") == str(order_id) for row in remaining if isinstance(row, dict)):
            raise RuntimeError(f"Gate cancellation not confirmed for order {order_id}")
        return {"cancelled": True, "order_id": str(order_id), "result": result}
