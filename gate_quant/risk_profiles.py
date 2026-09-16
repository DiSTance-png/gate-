from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any


PROFILE_VERSION = "gate-risk-v1"
ABSOLUTE_MIN_RR = 2.0


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
        "active", "积极", "适度降低趋势和置信度门槛，但不降低绝对 2R 底线。", 75, 80, 16, 2.0, 2.3, 0.85, 8, 1.6, 2.2, 1
    ),
    "aggressive": RiskProfile(
        "aggressive", "激进", "扩大可参与信号范围并使用完整保证金额度，绝对风控仍不可绕过。", 72, 78, 14, 2.0, 2.2, 1.0, 10, 1.6, 2.0, 2
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


validate_profile_catalog()
