from gate_quant.risk_profiles import derive_profit_lock_instruction
from scripts.evolution.observability import classify_trade


def _stop(price, contract="BTC_USDT", rule=2):
    return {"initial": {"contract": contract, "size": -1, "is_reduce_only": True}, "trigger": {"rule": rule, "price": str(price)}}


def test_profit_lock_ratchets_long_at_one_r_without_closing_position():
    result = derive_profit_lock_instruction(
        {"contract": "BTC_USDT", "size": 1, "entry_price": 100, "mark_price": 110},
        [_stop(90)], "standard",
    )
    assert result["action"] == "UPDATE_SL"
    assert result["stage"] == "breakeven"
    assert result["suggested_sl_price"] > 100


def test_profit_lock_uses_second_stage_after_two_r():
    result = derive_profit_lock_instruction(
        {"contract": "BTC_USDT", "size": 1, "entry_price": 100, "mark_price": 125},
        [_stop(90)], "standard",
    )
    assert result["stage"] == "profit_lock"
    assert result["suggested_sl_price"] == 107.5


def test_nested_gate_snapshot_is_observed():
    snapshot = {
        "feature": {
            "structure_1h": "BULL", "atr_1h": 10,
            "calculus_1h": {
                "regime": "POSITIVE_MOMENTUM", "velocity": 0.1,
                "acceleration": 0.2, "impulse": 0.3, "max_abs_jerk": 0.4,
                "definite_integrals": {"energy_integral": 1, "deviation_area_integral": 2},
                "probability_theory": {
                    "continuation_prob_pct": 60, "breakdown_prob_pct": 40,
                    "var_95_pct": 1, "cvar_95_pct": 2,
                },
            },
        },
        "decision": {"risk_reward_ratio": 2.5},
    }
    status, missing = classify_trade(snapshot, {"close_time": "2026-01-01", "net_pnl": 1})
    assert status == "OBSERVED"
    assert missing == []
