"""Deterministic host-side evidence quality classification for evolution reviews."""
from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

DYNAMICS_FIELDS = (
    "regime", "structure_1h", "velocity", "acceleration", "jerk", "impulse",
    "energy_integral", "deviation_area_integral", "continuation_probability",
    "breakdown_probability", "var", "cvar", "atr", "risk_reward",
)


def _has_value(value: Any) -> bool:
    return value is not None and value != "" and value != "--"


def classify_trade(entry_snapshot: Any, exit_evidence: Any = None) -> tuple[str, list[str]]:
    entry = entry_snapshot if isinstance(entry_snapshot, dict) else {}
    exit_row = exit_evidence if isinstance(exit_evidence, dict) else {}
    missing = [field for field in DYNAMICS_FIELDS if not _has_value(entry.get(field))]
    has_entry = bool(entry)
    has_exit = any(_has_value(exit_row.get(field)) for field in ("close_time", "exit_reason", "exit_price", "net_pnl"))
    if has_entry and not missing and has_exit:
        return "OBSERVED", []
    if has_entry and len(missing) < len(DYNAMICS_FIELDS):
        return ("PARTIAL" if has_exit else "PRICE_ONLY"), missing
    if any(_has_value(entry.get(field)) for field in ("entry_price", "price", "mark_price")) or has_exit:
        return "PRICE_ONLY", missing
    return "UNAVAILABLE", missing


def summarize_trades(trades: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(trades)
    counts = Counter(str(row.get("snapshot_observability") or "UNAVAILABLE") for row in rows)
    return {
        "total_samples": len(rows),
        "counts": {key: counts.get(key, 0) for key in ("OBSERVED", "PARTIAL", "PRICE_ONLY", "UNAVAILABLE")},
        "usable_for_causal": counts.get("OBSERVED", 0),
        "missing_fields": sorted({field for row in rows for field in (row.get("missing_evidence_fields") or [])}),
    }
