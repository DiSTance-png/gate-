from decimal import Decimal

from gate_quant.profit_guard import (
    ProfitGuardState,
    candidate_stage,
    cost_distance,
    initial_risk_distance,
    observe_state,
    profit_floor_candidate,
    r_multiple,
)


def test_initial_risk_and_r_use_immutable_distance_for_both_sides():
    assert initial_risk_distance(side="long", entry_price=100, initial_stop_price=90) == Decimal("10")
    assert initial_risk_distance(side="short", entry_price=100, initial_stop_price=110) == Decimal("10")
    assert r_multiple(side="short", entry_price=100, mark_price=94, initial_risk_distance_value=10) == Decimal("0.6")


def test_remaining_cost_uses_remaining_quantity_not_initial_quantity():
    assert cost_distance(remaining_cost=5, remaining_size=5, contract_multiplier=1) == Decimal("1")
    assert cost_distance(remaining_cost=5, remaining_size=10, contract_multiplier=1) == Decimal("0.5")


def test_v4_stages_are_observe_only_and_do_not_submit_orders():
    assert candidate_stage(mfe_r="0.4", d0="10", cost_distance_value="1") == "S0"
    s1 = profit_floor_candidate(
        side="long", entry_price=100, d0=10, mfe_r="0.8",
        remaining_cost=1, remaining_size=1,
    )
    assert s1["stage"] == "S1"
    assert Decimal(s1["candidate_stop_price"]) == Decimal("97.5")
    assert s1["enforceable"] is False
    s2 = profit_floor_candidate(
        side="short", entry_price=100, d0=10, mfe_r="1.1",
        remaining_cost=1, remaining_size=1,
    )
    assert s2["stage"] == "S2"
    assert Decimal(s2["candidate_stop_price"]) == Decimal("101")
    s3 = profit_floor_candidate(
        side="long", entry_price=100, d0=10, mfe_r="1.6",
        remaining_cost=1, remaining_size=1,
    )
    assert s3["stage"] == "S3"
    assert Decimal(s3["candidate_stop_price"]) == Decimal("108")


def test_observe_state_only_ratchets_local_mfe_and_serializes():
    state = ProfitGuardState(
        environment="live", contract="BTC_USDT", side="long",
        entry_price="100", initial_stop_price="90",
        initial_risk_distance="10", initial_size="10", remaining_size="5",
    )
    first = observe_state(state, mark_price=108, observed_at_ms=100, remaining_cost=5)
    second = observe_state(state, mark_price=103, observed_at_ms=200, remaining_cost=5)
    assert first["mfe_r"] == "0.8"
    assert second["mfe_r"] == "0.8"
    assert second["stage"] == "S1"
    restored = ProfitGuardState.from_mapping(second)
    assert restored.to_dict()["contract"] == "BTC_USDT"
    assert restored.mfe_observed_at_ms == 200


def test_invalid_risk_is_unmanaged():
    result = profit_floor_candidate(
        side="long", entry_price=100, d0=0, mfe_r=2,
        remaining_cost=1, remaining_size=1,
    )
    assert result["stage"] == "UNMANAGED"
    assert result["candidate_stop_price"] is None
