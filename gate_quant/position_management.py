from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def management_contract(row: dict[str, Any]) -> str:
    return str(row.get("contract") or row.get("instId") or "").strip().upper().replace("-USDT-SWAP", "_USDT").replace("-", "_")


def active_position_contracts(positions: list[dict[str, Any]]) -> set[str]:
    contracts = set()
    for position in positions:
        if not isinstance(position, dict):
            continue
        try:
            size = Decimal(str(position.get("size") or 0))
        except (InvalidOperation, TypeError, ValueError):
            continue
        contract = management_contract(position)
        if contract and size.is_finite() and size != 0:
            contracts.add(contract)
    return contracts


def filter_position_management(instructions: list[dict[str, Any]], contracts: set[str] | None) -> list[dict[str, Any]]:
    """Normalize management identities and restrict them to the supplied snapshot.

    None means a legacy record has no position snapshot; preserve its known
    instructions instead of using today's holdings to rewrite historical facts.
    """
    result = []
    for row in instructions:
        if not isinstance(row, dict):
            continue
        contract = management_contract(row)
        if not contract or (contracts is not None and contract not in contracts):
            continue
        result.append({**row, "contract": contract, "instId": contract.replace("_USDT", "-USDT-SWAP")})
    return result


def history_position_management(cycle: dict[str, Any]) -> list[dict[str, Any]]:
    snapshot = cycle.get("position_management_snapshot") or {}
    contracts = None
    if isinstance(snapshot, dict) and isinstance(snapshot.get("contracts"), list):
        contracts = {management_contract({"contract": value}) for value in snapshot["contracts"]}
    elif isinstance((cycle.get("private_context") or {}).get("positions"), list):
        contracts = active_position_contracts(cycle["private_context"]["positions"])
    return filter_position_management(cycle.get("position_management") or [], contracts)
