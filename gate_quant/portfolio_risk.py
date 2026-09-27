from __future__ import annotations

from typing import Any


PORTFOLIO_RISK_VERSION = "gate-portfolio-risk-v1"
CRYPTO_BETA_CLUSTER = frozenset({
    "BTC_USDT", "ETH_USDT", "SOL_USDT", "DOGE_USDT", "SUI_USDT", "XRP_USDT",
})


def _contract(value: Any) -> str:
    return str(value or "").upper().replace("-USDT-SWAP", "_USDT").replace("-", "_")


def correlated_exposure_gate(
    positions: list[dict[str, Any]], reservations: list[dict[str, Any]], *, contract: str, action: str
) -> dict[str, Any]:
    """Limit highly correlated same-direction instruments as one beta risk unit."""
    target = _contract(contract)
    side = "long" if action == "BUY_LONG" else "short" if action == "SELL_SHORT" else ""
    if not side or target not in CRYPTO_BETA_CLUSTER:
        return {"allowed": True, "margin_scale": 1.0, "same_direction_contracts": [], "version": PORTFOLIO_RISK_VERSION}

    exposed: set[str] = set()
    for row in positions or []:
        name = _contract(row.get("contract"))
        try:
            size = float(row.get("size") or 0)
        except (TypeError, ValueError):
            continue
        if name in CRYPTO_BETA_CLUSTER and size and ((size > 0) == (side == "long")):
            exposed.add(name)
    for row in reservations or []:
        name = _contract(row.get("contract"))
        if name in CRYPTO_BETA_CLUSTER and str(row.get("side") or "").lower() == side:
            exposed.add(name)

    count = len(exposed)
    if count >= 2:
        return {
            "allowed": False,
            "margin_scale": 0.0,
            "same_direction_contracts": sorted(exposed),
            "reason": "组合相关风险门禁：已有两个同方向加密 Beta 敞口，禁止新增或加仓。",
            "version": PORTFOLIO_RISK_VERSION,
        }
    return {
        "allowed": True,
        "margin_scale": 0.5 if count == 1 else 1.0,
        "same_direction_contracts": sorted(exposed),
        "reason": "组合相关风险缩放：第二个同方向加密 Beta 敞口保证金减半。" if count == 1 else "",
        "version": PORTFOLIO_RISK_VERSION,
    }
