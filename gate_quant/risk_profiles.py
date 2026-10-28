from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any


PROFILE_VERSION = "gate-risk-v3"
ABSOLUTE_MIN_RR = 1.8

PROFIT_LOCK_PROFILES: dict[str, dict[str, Any]] = {
    "observe": {"breakeven_r": 0.8, "breakeven_roi_pct": 1.5, "second_lock_r": 1.5, "second_lock_roi_pct": 3.0, "locked_profit_r": 0.6, "peak_exit_roi_pct": 2.5, "peak_exit_r": 1.0, "peak_drawdown_min_pct": 35, "peak_drawdown_max_pct": 45, "dissipation_roi_pct": 1.5, "dissipation_phi": -0.12, "dissipation_curvature": 1.5, "dissipation_requires_all": False},
    "conservative": {"breakeven_r": 0.8, "breakeven_roi_pct": 1.5, "second_lock_r": 1.5, "second_lock_roi_pct": 3.0, "locked_profit_r": 0.6, "peak_exit_roi_pct": 2.5, "peak_exit_r": 1.0, "peak_drawdown_min_pct": 35, "peak_drawdown_max_pct": 45, "dissipation_roi_pct": 1.5, "dissipation_phi": -0.12, "dissipation_curvature": 1.5, "dissipation_requires_all": False},
    "standard": {"breakeven_r": 1.0, "breakeven_roi_pct": 1.8, "second_lock_r": 1.8, "second_lock_roi_pct": 3.2, "locked_profit_r": 0.75, "peak_exit_roi_pct": 3.0, "peak_exit_r": 1.3, "peak_drawdown_min_pct": 40, "peak_drawdown_max_pct": 48, "dissipation_roi_pct": 2.0, "dissipation_phi": -0.13, "dissipation_curvature": 1.6, "dissipation_requires_all": False},
    "active": {"breakeven_r": 1.2, "breakeven_roi_pct": 2.0, "second_lock_r": 2.0, "second_lock_roi_pct": 3.4, "locked_profit_r": 0.9, "peak_exit_roi_pct": 3.2, "peak_exit_r": 1.5, "peak_drawdown_min_pct": 42, "peak_drawdown_max_pct": 52, "dissipation_roi_pct": 2.2, "dissipation_phi": -0.14, "dissipation_curvature": 1.7, "dissipation_requires_all": True},
    # Aggressive entries still need a fast capital-protection ratchet: a move
    # to 1R followed by a normal pullback must not turn into a full stop loss.
    "aggressive": {"breakeven_r": 1.5, "execution_breakeven_r": 1.0, "breakeven_roi_pct": 2.2, "second_lock_r": 1.8, "second_lock_roi_pct": 3.2, "locked_profit_r": 0.7, "peak_exit_roi_pct": 3.5, "peak_exit_r": 1.8, "peak_drawdown_min_pct": 45, "peak_drawdown_max_pct": 55, "dissipation_roi_pct": 2.5, "dissipation_phi": -0.15, "dissipation_curvature": 1.8, "dissipation_requires_all": True},
}


@dataclass(frozen=True)
class RiskProfile:
    key: str
    label: str
    description: str
    min_confidence: float
    doge_min_confidence: float
    min_adx: float
    min_rr: float
    target_rr: float
    margin_ratio: float
    max_leverage: float
    stop_atr_min: float
    stop_atr_max: float
    max_entries_per_cycle: int
    execution_allowed: bool = True

    def snapshot(self, *, leverage: float, environment: str) -> dict[str, Any]:
        values = asdict(self)
        values.update(
            {
                "risk_profile": self.key,
                "risk_profile_version": PROFILE_VERSION,
                "leverage": float(leverage),
                "environment": environment,
                "absolute_min_rr": ABSOLUTE_MIN_RR,
                "profit_lock": dict(PROFIT_LOCK_PROFILES[self.key]),
            }
        )
        canonical = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        values["risk_profile_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        return values


RISK_PROFILES: dict[str, RiskProfile] = {
    "observe": RiskProfile(
        "observe", "观察模式", "只生成和审计 AI 决策，绝不提交订单。", 70, 75, 14, 2.0, 2.2, 0.0, 3, 1.6, 2.2, 0, False
    ),
    "conservative": RiskProfile(
        "conservative", "保守", "提高质量门槛并缩小保证金，适合验证稳定性。", 85, 88, 22, 2.4, 2.8, 0.35, 3, 2.0, 2.4, 1
    ),
    "standard": RiskProfile(
        "standard", "标准", "平衡信号质量与机会频率，保留完整硬风控。", 80, 83, 18, 2.2, 2.5, 0.65, 5, 1.8, 2.2, 1
    ),
    "active": RiskProfile(
        "active", "积极", "适度降低趋势和置信度门槛，但仍保持 2R 档位底线。", 75, 80, 16, 2.0, 2.3, 0.85, 8, 1.6, 2.2, 1
    ),
    "aggressive": RiskProfile(
        "aggressive", "激进", "扩大可参与信号范围并使用完整保证金额度，最低 1.9R，绝对风控仍不可绕过。", 72, 78, 14, 1.9, 2.2, 1.0, 10, 1.6, 2.0, 2
    ),
}


def get_risk_profile(name: str) -> RiskProfile:
    key = str(name or "standard").strip().lower()
    try:
        return RISK_PROFILES[key]
    except KeyError as exc:
        raise ValueError(f"GATE_RISK_PROFILE must be one of: {', '.join(RISK_PROFILES)}") from exc


def profile_catalog() -> list[dict[str, Any]]:
    return [asdict(profile) for profile in RISK_PROFILES.values()]


def validate_profile_catalog() -> None:
    for profile in RISK_PROFILES.values():
        if profile.min_rr < ABSOLUTE_MIN_RR:
            raise ValueError(f"{profile.key} cannot lower the absolute {ABSOLUTE_MIN_RR:g}R floor")
        if not 0 <= profile.margin_ratio <= 1:
            raise ValueError(f"{profile.key} margin_ratio must be between 0 and 1")
        if profile.stop_atr_min <= 0 or profile.stop_atr_max < profile.stop_atr_min:
            raise ValueError(f"{profile.key} stop ATR range is invalid")
        if not 0 <= profile.max_entries_per_cycle <= 2:
            raise ValueError(f"{profile.key} max entries per cycle must be between 0 and 2")
        lock = PROFIT_LOCK_PROFILES[profile.key]
        if lock["second_lock_r"] <= lock["breakeven_r"] or lock["peak_drawdown_min_pct"] >= lock["peak_drawdown_max_pct"]:
            raise ValueError(f"{profile.key} profit-lock thresholds are invalid")


validate_profile_catalog()


def derive_profit_lock_instruction(
    position: dict[str, Any], protections: list[dict[str, Any]], profile_key: str,
    *, fee_buffer_pct: float = 0.0005,
) -> dict[str, Any] | None:
    """Return a deterministic, favorable stop ratchet for an open position.

    This is intentionally a pure helper.  It does not place or cancel orders;
    the existing Gate write-locked management path remains the only executor.
    """
    try:
        size = float(position.get("size") or 0)
        entry = float(position.get("entry_price") or position.get("avgPx") or 0)
        mark = float(position.get("mark_price") or position.get("lastPx") or 0)
    except (TypeError, ValueError):
        return None
    if not size or entry <= 0 or mark <= 0:
        return None
    side = "long" if size > 0 else "short"
    stop_rule = 2 if side == "long" else 1
    stop_rows = []
    for row in protections or []:
        initial = row.get("initial") or {}
        contract = str(initial.get("contract") or row.get("contract") or "").upper()
        if contract != str(position.get("contract") or "").upper():
            continue
        if int((row.get("trigger") or {}).get("rule") or 0) != stop_rule:
            continue
        try:
            price = float((row.get("trigger") or {}).get("price") or 0)
        except (TypeError, ValueError):
            price = 0
        if price > 0:
            stop_rows.append(price)
    if not stop_rows:
        return None
    current_stop = max(stop_rows) if side == "long" else min(stop_rows)
    initial_risk = entry - current_stop if side == "long" else current_stop - entry
    favorable = mark - entry if side == "long" else entry - mark
    if initial_risk <= 0 or favorable <= 0:
        return None
    r_multiple = favorable / initial_risk
    lock = PROFIT_LOCK_PROFILES.get(str(profile_key or "standard"), PROFIT_LOCK_PROFILES["standard"])
    if r_multiple >= float(lock["second_lock_r"]):
        locked_r = float(lock["locked_profit_r"])
        target = entry + initial_risk * locked_r if side == "long" else entry - initial_risk * locked_r
        stage = "profit_lock"
    elif r_multiple >= float(lock.get("execution_breakeven_r", lock["breakeven_r"])):
        buffer = abs(entry) * max(0.0, float(fee_buffer_pct))
        target = entry + buffer if side == "long" else entry - buffer
        stage = "breakeven"
    else:
        return None
    favorable_move = target > current_stop if side == "long" else target < current_stop
    if not favorable_move:
        return None
    return {
        "contract": str(position.get("contract") or "").upper(),
        "action": "UPDATE_SL",
        "suggested_sl_price": target,
        "reason": f"deterministic_{stage}_ratchet_{r_multiple:.2f}R",
        "r_multiple": round(r_multiple, 4),
        "stage": stage,
    }
