import copy

import pytest

from gate_quant.ai_worker import _prepare_position_management
from gate_quant.position_management import history_position_management


POSITIONS = [
    {"contract": "BTC_USDT", "mode": "dual_short", "size": "-1"},
    {"contract": "BTC_USDT", "mode": "dual_long", "size": "0"},
    {"contract": "SOL_USDT", "mode": "dual_long", "size": "0"},
    {"contract": "SOL_USDT", "mode": "dual_short", "size": "0.0"},
    {"contract": "ETH_USDT", "mode": "dual_short", "size": 0},
]


def test_zero_dual_positions_never_receive_fallback_or_model_management():
    rows = _prepare_position_management([
        {"instId": "BTC-USDT-SWAP", "action": "HOLD", "reason": "keep"},
        {"instId": "SOL-USDT-SWAP", "action": "CLOSE_MARKET"},
    ], POSITIONS, [], "standard")
    assert len(rows) == 1
    assert rows[0]["contract"] == "BTC_USDT"
    assert rows[0]["reason"] == "keep"


def test_fallback_covers_only_nonzero_position_once_with_display_identity():
    rows = _prepare_position_management([], POSITIONS, [], "standard")
    assert len(rows) == 1
    assert rows[0]["contract"] == "BTC_USDT"
    assert rows[0]["instId"] == "BTC-USDT-SWAP"
    assert rows[0]["action"] == "HOLD"
    assert rows[0]["management_source"] == "deterministic_hold_fallback"


def test_flat_account_has_empty_management_even_when_model_returns_instructions():
    assert _prepare_position_management([
        {"contract": "SOL_USDT", "action": "HOLD"},
    ], POSITIONS[2:], [], "standard") == []


def test_profit_ratchet_supersedes_hold_without_duplicate_instruction():
    position = {"contract": "BTC_USDT", "size": 1, "entry_price": 100, "mark_price": 110}
    protection = {"initial": {"contract": "BTC_USDT", "size": -1, "is_reduce_only": True}, "trigger": {"rule": 2, "price": "90"}}
    rows = _prepare_position_management([
        {"contract": "BTC_USDT", "action": "HOLD"},
    ], [position], [protection], "standard")
    assert len(rows) == 1
    assert rows[0]["action"] == "UPDATE_SL"
    assert rows[0]["suggested_sl_price"] > 100


@pytest.mark.parametrize("action", ["CLOSE_MARKET", "UPDATE_SL"])
def test_explicit_active_position_management_is_preserved(action):
    instruction = {"instId": "BTC-USDT-SWAP", "action": action, "suggested_sl_price": 110}
    rows = _prepare_position_management([instruction], POSITIONS, [], "standard")
    assert len(rows) == 1
    assert rows[0]["action"] == action
    assert rows[0]["suggested_sl_price"] == 110


def test_history_filters_using_its_own_snapshot_without_rewriting_audit():
    cycle = {
        "position_management_snapshot": {"contracts": ["BTC_USDT"]},
        "position_management": [
            {"instId": "BTC-USDT-SWAP", "action": "HOLD"},
            {"contract": "SOL_USDT", "action": "HOLD", "management_source": "deterministic_hold_fallback"},
            {"contract": "SOL_USDT", "action": "HOLD", "management_source": "deterministic_hold_fallback"},
        ],
    }
    before = copy.deepcopy(cycle)
    rows = history_position_management(cycle)
    assert len(rows) == 1
    assert rows[0]["instId"] == "BTC-USDT-SWAP"
    assert cycle == before


def test_legacy_history_keeps_real_management_when_snapshot_unavailable():
    rows = history_position_management({"position_management": [{"contract": "SOL_USDT", "action": "HOLD"}]})
    assert rows[0]["instId"] == "SOL-USDT-SWAP"
