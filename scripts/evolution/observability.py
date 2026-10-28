"""Deterministic host-side evidence quality classification for evolution reviews."""
from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

DYNAMICS_FIELDS = (
    "regime", "structure_1h", "velocity", "acceleration", "jerk", "impulse",
    "energy_integral", "deviation_area_integral", "continuation_probability",
    "breakdown_probability", "var", "cvar", "atr", "risk_reward",
)


def snapshot_evidence(snapshot: Any) -> dict[str, Any]:
    """Flatten the nested Gate entry snapshot into auditable evidence fields.

    Gate stores the feature package under ``entry_snapshot.feature`` and keeps
    the 1H calculus values one level deeper.  Older reviews looked only at
    top-level keys, so real Gate snapshots were incorrectly labelled
    ``PRICE_ONLY``.  This function deliberately copies only known fields and
    never invents values.
    """
    if not isinstance(snapshot, dict):
        return {}
    feature = snapshot.get("feature") if isinstance(snapshot.get("feature"), dict) else {}
    package = snapshot.get("strategy_package") if isinstance(snapshot.get("strategy_package"), dict) else {}
    decision = snapshot.get("decision") if isinstance(snapshot.get("decision"), dict) else {}
    evidence: dict[str, Any] = {}
    for source in (snapshot, feature, package, decision):
        for key, value in source.items():
            if key not in evidence and _has_value(value):
                evidence[key] = value

    calculus_sources = [
        feature.get("calculus_1h"),
        package.get("calculus_1h"),
        feature.get("calculus"),
        package.get("calculus"),
    ]
    for calculus in calculus_sources:
        if not isinstance(calculus, dict):
            continue
        for key in ("regime", "velocity", "acceleration", "impulse", "jerk", "max_abs_jerk"):
            if key not in evidence and _has_value(calculus.get(key)):
                evidence[key] = calculus[key]
        integrals = calculus.get("definite_integrals")
        if isinstance(integrals, dict):
            for key in ("energy_integral", "deviation_area_integral"):
                if key not in evidence and _has_value(integrals.get(key)):
                    evidence[key] = integrals[key]
        probability = calculus.get("probability_theory")
        if isinstance(probability, dict):
            aliases = {
                "continuation_probability": ("continuation_probability", "continuation_prob_pct"),
                "breakdown_probability": ("breakdown_probability", "breakdown_prob_pct"),
                "var": ("var", "var_95_pct"),
                "cvar": ("cvar", "cvar_95_pct"),
            }
            for target, keys in aliases.items():
                if target not in evidence:
                    value = next((probability.get(key) for key in keys if _has_value(probability.get(key))), None)
                    if _has_value(value):
                        evidence[target] = value

    aliases = {
        "regime": ("regime", "market_regime"),
        "structure_1h": ("structure_1h",),
        "velocity": ("velocity", "velocity_1h"),
        "acceleration": ("acceleration", "acceleration_1h"),
        "impulse": ("impulse", "impulse_1h"),
        "jerk": ("jerk", "max_abs_jerk", "jerk_1h"),
        "atr": ("atr", "atr_1h", "atr14"),
        "risk_reward": ("risk_reward", "risk_reward_ratio"),
        "continuation_probability": ("continuation_probability", "continuation_prob_pct"),
        "breakdown_probability": ("breakdown_probability", "breakdown_prob_pct"),
        "var": ("var", "var_95_pct"),
        "cvar": ("cvar", "cvar_95_pct"),
    }
    for target, keys in aliases.items():
        if target in evidence:
            continue
        value = next((evidence.get(key) for key in keys if _has_value(evidence.get(key))), None)
        if _has_value(value):
            evidence[target] = value
    return evidence


def _has_value(value: Any) -> bool:
    return value is not None and value != "" and value != "--"


def classify_trade(entry_snapshot: Any, exit_evidence: Any = None) -> tuple[str, list[str]]:
    entry = snapshot_evidence(entry_snapshot)
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
