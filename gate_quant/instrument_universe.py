"""Atomic, fail-closed Gate trading-universe changes."""
from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any

from scripts.instrument_pool import (
    configured_gate_contracts,
    from_gate_contract,
    instrument_pool_revision,
    load_instruments,
    save_instruments,
)


ROOT = Path(__file__).resolve().parents[1]
PREVIEW_FILE = ROOT / "data" / "gate_instrument_change_preview.json"
MAX_INSTRUMENTS = 6
PREVIEW_TTL_SECONDS = 10 * 60
_BASE_RE = re.compile(r"^[A-Z0-9]{2,20}$")


def normalize_contracts(values: Any) -> list[str]:
    if not isinstance(values, list):
        raise ValueError("交易对必须是列表")
    result: list[str] = []
    for item in values:
        value = str(item or "").strip().upper()
        value = value.replace("-USDT-SWAP", "_USDT").replace("-", "_")
        base = value[:-5] if value.endswith("_USDT") else value
        if not _BASE_RE.fullmatch(base):
            raise ValueError(f"无效的 Gate 合约代码：{item}")
        contract = f"{base}_USDT"
        if contract not in result:
            result.append(contract)
    if not 1 <= len(result) <= MAX_INSTRUMENTS:
        raise ValueError("自选交易对必须保留 1 至 6 个")
    return result


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".gate-universe-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def _read_preview() -> dict[str, Any]:
    try:
        value = json.loads(PREVIEW_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    return [value] if isinstance(value, dict) and value else []


def validate_gate_contracts(market_client: Any, contracts: list[str]) -> list[dict[str, Any]]:
    instruments: list[dict[str, Any]] = []
    for contract in contracts:
        raw = market_client.contracts(contract)
        if not isinstance(raw, dict) or str(raw.get("name") or "").upper() != contract:
            raise ValueError(f"Gate 未返回有效合约元数据：{contract}")
        if bool(raw.get("in_delisting")):
            raise ValueError(f"{contract} 正在退市，不能加入实盘交易池")
        trade_status = str(raw.get("trade_status") or "tradable").lower()
        if trade_status not in {"tradable", "trading", "live", ""}:
            raise ValueError(f"{contract} 当前不可交易：{trade_status}")
        instruments.append(from_gate_contract(raw))
    return instruments


def removal_blockers(
    private_client: Any,
    removed: list[str],
    *,
    execution_journal: Any | None = None,
    environment: str = "",
) -> dict[str, list[dict[str, Any]]]:
    """Read all private state once and reject any removed contract still under management."""
    removed_set = set(removed)
    result: dict[str, list[dict[str, Any]]] = {
        "positions": [],
        "orders": [],
        "trigger_entries": [],
        "protections": [],
        "execution_intents": [],
    }
    if not removed_set:
        return result

    for row in _rows(private_client.positions()):
        contract = str(row.get("contract") or "").upper()
        if contract in removed_set and float(row.get("size") or 0) != 0:
            result["positions"].append({"contract": contract, "size": str(row.get("size") or "0"), "mode": row.get("mode")})
    for row in _rows(private_client.open_orders()):
        contract = str(row.get("contract") or "").upper()
        if contract in removed_set:
            result["orders"].append({"contract": contract, "id": str(row.get("id") or ""), "text": str(row.get("text") or "")})
    for row in _rows(private_client.trigger_entry_orders()):
        initial = row.get("initial") or {}
        contract = str(initial.get("contract") or row.get("contract") or "").upper()
        if contract in removed_set:
            result["trigger_entries"].append({"contract": contract, "id": str(row.get("id_string") or row.get("id") or ""), "text": str(initial.get("text") or "")})
    for row in _rows(private_client.protection_orders()):
        initial = row.get("initial") or {}
        contract = str(initial.get("contract") or row.get("contract") or "").upper()
        if contract in removed_set:
            result["protections"].append({"contract": contract, "id": str(row.get("id_string") or row.get("id") or ""), "text": str(initial.get("text") or "")})
    if execution_journal is not None:
        for row in execution_journal.active(environment, limit=1000):
            contract = str(row.get("contract") or "").upper()
            if contract in removed_set:
                result["execution_intents"].append({"contract": contract, "client_id": str(row.get("client_id") or ""), "status": str(row.get("status") or "")})
    return result


def has_blockers(blockers: dict[str, list[dict[str, Any]]]) -> bool:
    return any(bool(rows) for rows in blockers.values())


def create_preview(
    contracts: list[str],
    *,
    actor: str,
    environment: str,
    market_client: Any,
    private_client: Any,
    execution_journal: Any | None = None,
) -> dict[str, Any]:
    requested = normalize_contracts(contracts)
    current = configured_gate_contracts()
    current_revision = instrument_pool_revision()
    added = [item for item in requested if item not in current]
    removed = [item for item in current if item not in requested]
    instruments = validate_gate_contracts(market_client, requested)
    blockers = removal_blockers(
        private_client, removed,
        execution_journal=execution_journal,
        environment=environment,
    )
    if has_blockers(blockers):
        raise ValueError("取消的交易对仍有持仓、挂单、保护单或未完成执行意图，拒绝修改")
    token = secrets.token_urlsafe(24)
    short_code = secrets.token_hex(3).upper()
    confirmation = f"APPLY {environment.upper()} SYMBOLS {short_code}"
    now_ms = int(time.time() * 1000)
    preview = {
        "token": token,
        "actor": actor,
        "environment": environment,
        "current_revision": current_revision,
        "current_contracts": current,
        "requested_contracts": requested,
        "added": added,
        "removed": removed,
        "instruments": instruments,
        "blockers": blockers,
        "confirmation_phrase": confirmation,
        "created_at_ms": now_ms,
        "expires_at_ms": now_ms + PREVIEW_TTL_SECONDS * 1000,
    }
    _atomic_json(PREVIEW_FILE, preview)
    return preview


def apply_preview(
    token: str,
    confirmation: str,
    *,
    actor: str,
    environment: str,
    market_client: Any,
    private_client: Any,
    execution_journal: Any | None = None,
) -> dict[str, Any]:
    preview = _read_preview()
    now_ms = int(time.time() * 1000)
    if not preview or not secrets.compare_digest(str(preview.get("token") or ""), str(token or "")):
        raise ValueError("标的池确认令牌无效，请重新预览")
    if now_ms > int(preview.get("expires_at_ms") or 0):
        raise ValueError("标的池确认已过期，请重新预览")
    if preview.get("actor") != actor or preview.get("environment") != environment:
        raise ValueError("标的池确认与当前管理员或交易环境不一致")
    if str(confirmation or "").strip().upper() != str(preview.get("confirmation_phrase") or "").upper():
        raise ValueError(f"确认短语必须精确为：{preview.get('confirmation_phrase')}")
    if instrument_pool_revision() != preview.get("current_revision"):
        raise ValueError("标的池已被其他操作修改，请重新预览")

    requested = normalize_contracts(preview.get("requested_contracts"))
    current = configured_gate_contracts()
    removed = [item for item in current if item not in requested]
    instruments = validate_gate_contracts(market_client, requested)
    blockers = removal_blockers(
        private_client, removed,
        execution_journal=execution_journal,
        environment=environment,
    )
    if has_blockers(blockers):
        raise ValueError("确认期间账户状态发生变化：取消的交易对出现持仓或在途订单，拒绝修改")
    save_instruments(instruments)
    try:
        PREVIEW_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    return {
        "ok": True,
        "environment": environment,
        "contracts": requested,
        "added": [item for item in requested if item not in current],
        "removed": removed,
        "revision": instrument_pool_revision(instruments),
        "effective": "next_ai_cycle_and_next_dashboard_refresh",
    }
