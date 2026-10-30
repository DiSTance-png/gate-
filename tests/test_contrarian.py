from decimal import Decimal

from gate_quant.contrarian import build_candidate, opposite_action
from gate_quant.config import GateSettings


def _feature(regime="RANGE"):
    return {"contract": "BTC_USDT", "last": 100, "market_regime": {"name": regime}}


def test_opposite_action_is_symmetric_and_wait_safe():
    assert opposite_action("BUY_LONG") == "SELL_SHORT"
    assert opposite_action("SELL_SHORT") == "BUY_LONG"
    assert opposite_action("WAIT") == "WAIT"


def test_reverse_retracement_uses_current_quote_and_recomputes_one_to_two_protection():
    result = build_candidate(
        {"action": "SELL_SHORT", "entry_intent": "retracement", "entry_price": 105,
         "take_profit_price": 95, "stop_loss_price": 110, "confidence": 80},
        _feature(), {"highest_bid": "99.8", "lowest_ask": "100.2"},
        mode="shadow", take_profit_r=1, stop_loss_r=2,
    )
    assert result["effective_action"] == "BUY_LONG"
    assert result["entry_intent"] == "immediate"
    assert Decimal(result["entry_price"]) == Decimal("100.2")
    assert Decimal(result["risk_distance"]) == Decimal("5")
    assert Decimal(result["take_profit_price"]) == Decimal("105.2")
    assert Decimal(result["stop_loss_price"]) == Decimal("90.2")
    assert result["reward_to_risk"] == "0.5"
    assert result["eligible"] is True
    assert result["enforceable"] is False


def test_active_position_blocks_reverse_candidate_without_reversing_position():
    result = build_candidate(
        {"action": "BUY_LONG", "entry_intent": "retracement", "entry_price": 95,
         "take_profit_price": 105, "stop_loss_price": 90},
        _feature(), {"highest_bid": "99.8", "lowest_ask": "100.2"},
        current_position_side="short", mode="shadow",
    )
    assert result["effective_action"] == "SELL_SHORT"
    assert result["eligible"] is False
    assert "active_position_blocks_new_entry" in result["reason"]


def test_only_retracement_signals_are_candidates_and_invalid_geometry_fails_closed():
    assert build_candidate(
        {"action": "BUY_LONG", "entry_intent": "immediate", "entry_price": 100,
         "take_profit_price": 105, "stop_loss_price": 95},
        _feature(), {"highest_bid": 99, "lowest_ask": 100},
    ) is None
    result = build_candidate(
        {"action": "BUY_LONG", "entry_intent": "retracement", "entry_price": 100,
         "take_profit_price": 105, "stop_loss_price": 100},
        _feature(), {"highest_bid": 99, "lowest_ask": 100},
    )
    assert result["eligible"] is False
    assert "invalid_raw_risk_distance" in result["reason"]


def test_strong_trend_is_recorded_but_not_shadow_eligible():
    result = build_candidate(
        {"action": "SELL_SHORT", "entry_intent": "retracement", "entry_price": 105,
         "take_profit_price": 95, "stop_loss_price": 110},
        _feature("BULL_TREND"), {"highest_bid": 99, "lowest_ask": 100},
    )
    assert result["eligible"] is False
    assert "regime_not_eligible" in result["reason"]


def test_contrarian_configuration_is_explicit_and_fail_closed_for_invalid_values():
    settings = GateSettings(environment="testnet", contrarian_mode="shadow")
    settings.validate()
    try:
        GateSettings(environment="testnet", contrarian_mode="invalid").validate()
    except ValueError as exc:
        assert "GATE_CONTRARIAN_MODE" in str(exc)
    else:
        raise AssertionError("invalid contrarian mode must be rejected")
