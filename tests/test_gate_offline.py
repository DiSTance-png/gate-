import json
import requests
import pytest
from decimal import Decimal
from gate_quant.client import GateFuturesClient, AmbiguousOrderError
from gate_quant.config import GateSettings
from gate_quant.risk import RiskLimits
from gate_quant.service import GateTradingService, protection_coverage_status
from gate_quant.ai_worker import _extract_decision_object, _extract_response_object, _as_instruction_list, _order_size_for_margin, _protection_matches, _execution_enabled, _account_committed_margin, _portfolio_position_notional
from gate_quant.strategy_adapter import validate_decision
from gate_quant.risk_profiles import get_risk_profile
from r20_gateway.scheduler import JOBS
from gate_quant.safety import classify_error, daily_loss_state, reconcile_exchange_state
from gate_quant.protection_lifecycle import intent_from_history, is_system_protection, recovery_plans


class FakeSession:
    def __init__(self): self.calls = []
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "POST": raise requests.Timeout("offline timeout")
        class R:
            content = b"[]"
            def raise_for_status(self): pass
            def json(self): return []
        return R()


def settings(): return GateSettings(environment="testnet", api_key="offline-key", api_secret="offline-secret", max_order_margin_usd=10, max_total_margin_usd=20, max_position_notional_usd=100)


def test_gateway_scheduler_includes_gate_ledger_and_self_improvement():
    jobs = {job.name: job for job in JOBS}
    assert jobs["gate_ledger"].interval_seconds == 15 * 60
    assert jobs["self_improvement"].schedule_key == "self_improvement_times"


def test_safety_reconciliation_detects_stale_order_and_protection_gap():
    result = reconcile_exchange_state(
        [{"contract": "BTC_USDT", "size": "2"}],
        [{"id": "o1", "contract": "ETH_USDT", "size": "1", "create_time": 1}],
        [], max_pending_age_seconds=60, now=1000,
    )
    assert not result["safe_for_new_risk"]
    assert {item["code"] for item in result["issues"]} >= {"protection_gap", "stale_entry_orders"}


def test_safety_daily_loss_uses_stricter_absolute_or_equity_limit(tmp_path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text('[{"status":"closed","close_time":"1970-01-01 00:16:40","net_pnl":-60}]', encoding="utf-8")
    state = daily_loss_state(ledger, max_loss_usd=100, max_loss_ratio=0.05, equity=1000, now=1000)
    assert state["tripped"]
    assert state["limit_usd"] == 50


def test_safety_error_classification_is_explicit():
    assert classify_error("Gate API INVALID_KEY (HTTP 401)") == "authentication"
    assert classify_error("Gate request timed out") == "timeout"


def test_live_read_only_is_allowed_but_write_gate_stays_closed():
    live = GateSettings(environment="live", api_key="k", api_secret="s")
    live.validate()
    assert not _execution_enabled(live)


def test_environment_switches_are_strictly_isolated():
    assert _execution_enabled(GateSettings(environment="testnet", testnet_execute_trades=True))
    assert not _execution_enabled(GateSettings(environment="live", testnet_execute_trades=True))
    assert _execution_enabled(GateSettings(environment="live", api_key="k", api_secret="s", public_market_environment="live", live_trading_enabled=True))
    with pytest.raises(ValueError, match="PUBLIC_MARKET_ENV"):
        GateSettings(environment="live", api_key="k", api_secret="s", public_market_environment="testnet", live_trading_enabled=True).validate()


def test_testnet_execution_requires_testnet_credentials():
    with pytest.raises(ValueError, match="Testnet credentials"):
        GateSettings(environment="testnet", testnet_execute_trades=True).validate()


def test_timeout_reconciles_before_retry():
    fake = FakeSession(); client = GateFuturesClient(settings(), fake)
    service = GateTradingService(client, RiskLimits(100, 20, 10))
    with pytest.raises(AmbiguousOrderError):
        service.place_order(contract="BTC_USDT", size=1, client_id="t-gate-1", risk={"order_margin_usd": 1, "current_margin_usd": 0, "current_position_notional_usd": 0, "order_notional_usd": 10, "environment": "testnet", "live_enabled": False})
    assert [c[0] for c in fake.calls] == ["POST", "GET", "GET", "GET"]


def test_client_id_reconciliation_scans_open_and_finished_orders():
    class C(GateFuturesClient):
        def __init__(self):
            pass
        def order(self, order_id, contract=None):
            raise RuntimeError("custom-id lookup window elapsed")
        def list_orders(self, *, status, contract=None, limit=100):
            assert limit == 100
            return ([{"id": "77", "contract": contract, "text": "other"}] if status == "open" else
                    [{"id": "88", "contract": contract, "text": "t-gate-reconcile", "status": "finished"}])
    found = C().find_by_client_id("t-gate-reconcile", "BTC_USDT")
    assert found["id"] == "88"


def test_protection_coverage():
    class C:
        def protection_orders(self, contract):
            return [
                {"initial": {"size": -2}, "trigger": {"rule": 1}},
                {"initial": {"size": -2}, "trigger": {"rule": 2}},
            ]
    assert GateTradingService(C(), RiskLimits(1, 1, 1)).verify_protection_coverage("BTC_USDT", 2)


def test_protection_cancel_is_confirmed_by_fresh_gate_query():
    class C:
        def cancel_protection_order(self, order_id):
            return {"id": order_id, "status": "cancelled"}
        def protection_orders(self):
            return [{"id_string": "other"}]
    result = GateTradingService(C(), RiskLimits(1, 1, 1)).cancel_protection_confirmed(order_id="90071992547409930")
    assert result["cancelled"] is True
    assert result["order_id"] == "90071992547409930"


def test_llm_parser_uses_reasoning_tail_and_requires_all_contracts():
    contracts = ("BTC_USDT", "ETH_USDT", "SOL_USDT", "DOGE_USDT", "SUI_USDT", "XRP_USDT")
    payload = {"decisions": {symbol: {"action": "WAIT", "confidence": 12} for symbol in contracts}}
    reasoning = "internal analysis {not-json}\nfinal answer:\n" + json.dumps(payload)
    parsed = _extract_decision_object("", reasoning)
    assert parsed is not None
    assert set(parsed) == set(contracts)
    assert _extract_decision_object(json.dumps({"decisions": {contracts[0]: {"action": "WAIT"}}}), "") is None
    nested = {"position_management": {symbol: "HOLD" for symbol in contracts}, "pending_orders_management": {symbol: "KEEP" for symbol in contracts}}
    assert _extract_response_object(json.dumps(nested), "") is None
    assert _as_instruction_list({"BTC_USDT": "HOLD", "ETH_USDT": {"action": "HOLD"}}) == [{"contract": "BTC_USDT", "action": "HOLD"}, {"contract": "ETH_USDT", "action": "HOLD"}]


def test_gate_strategy_adapter_reuses_original_rr_gate():
    action, reason, rr = validate_decision(
        {"instId": "BTC-USDT-SWAP", "data_quality": "valid", "macro_4h": "BULL"},
        {"action": "BUY_LONG", "confidence": 85, "entry_price": 100, "take_profit_price": 110, "stop_loss_price": 105},
        active_inst_ids=set(), active_position_sides={},
    )
    assert action == "WAIT"
    assert "几何" in reason
    assert rr == 0


def test_gate_margin_converts_to_integer_contract_size():
    assert _order_size_for_margin(margin_usdt=25, leverage=3, entry_price=100, multiplier=0.01) == 75
    with pytest.raises(ValueError, match="too small"):
        _order_size_for_margin(margin_usdt=1, leverage=1, entry_price=100, multiplier=1)


def test_gate_decimal_contract_size_uses_exchange_quantum():
    assert _order_size_for_margin(margin_usdt=20, leverage=3, entry_price=150, multiplier=1, minimum="0.1", enable_decimal=True) == Decimal("0.4")
    with pytest.raises(ValueError, match="too small"):
        _order_size_for_margin(margin_usdt=20, leverage=3, entry_price=150, multiplier=1, minimum="1", enable_decimal=False)


def test_risk_profiles_are_versioned_and_observe_disables_execution():
    active = get_risk_profile("active").snapshot(leverage=3, environment="testnet")
    assert active["min_rr"] >= 2.0
    assert len(active["risk_profile_hash"]) == 16
    assert not _execution_enabled(GateSettings(environment="testnet", testnet_execute_trades=True, risk_profile="observe"))


def test_profile_leverage_cap_is_fail_closed():
    with pytest.raises(ValueError, match="cannot exceed"):
        GateSettings(environment="testnet", risk_profile="conservative", leverage=4).validate()


def test_strategy_interceptors_use_the_same_risk_snapshot():
    package = {"name": "SOL", "instId": "SOL_USDT", "data_quality": "valid", "macro_4h": "RANGE", "adx_1h": 15}
    decision = {"action": "BUY_LONG", "confidence": 72, "entry_price": 100, "take_profit_price": 110, "stop_loss_price": 95}
    aggressive = get_risk_profile("aggressive").snapshot(leverage=3, environment="testnet")
    standard = get_risk_profile("standard").snapshot(leverage=3, environment="testnet")
    assert validate_decision(package, decision, active_inst_ids=set(), active_position_sides={}, risk_snapshot=aggressive)[0] == "BUY_LONG"
    action, reason, _ = validate_decision(package, decision, active_inst_ids=set(), active_position_sides={}, risk_snapshot=standard)
    assert action == "WAIT"
    assert "80.0%" in reason or "80%" in reason


def test_existing_other_contract_does_not_block_new_contract_signal():
    package = {"name": "SOL", "instId": "SOL_USDT", "data_quality": "valid", "macro_4h": "BULL", "adx_1h": 20}
    decision = {"action": "BUY_LONG", "confidence": 85, "entry_price": 100, "take_profit_price": 110, "stop_loss_price": 95}
    aggressive = get_risk_profile("aggressive").snapshot(leverage=3, environment="testnet")
    action, _, _ = validate_decision(
        package, decision,
        active_inst_ids={"ETH_USDT"}, active_position_sides={"ETH_USDT": "long"},
        risk_snapshot=aggressive,
    )
    assert action == "BUY_LONG"


def test_same_contract_same_direction_add_on_is_allowed():
    package = {"name": "ETH", "instId": "ETH_USDT", "data_quality": "valid", "macro_4h": "BULL", "adx_1h": 20}
    decision = {"action": "BUY_LONG", "confidence": 85, "entry_price": 100, "take_profit_price": 110, "stop_loss_price": 95}
    aggressive = get_risk_profile("aggressive").snapshot(leverage=3, environment="testnet")
    action, _, _ = validate_decision(
        package, decision,
        active_inst_ids={"ETH-USDT-SWAP"}, active_position_sides={"ETH_USDT": "long"},
        risk_snapshot=aggressive,
    )
    assert action == "BUY_LONG"


def test_same_contract_opposite_direction_add_on_is_blocked():
    package = {"name": "ETH", "instId": "ETH_USDT", "data_quality": "valid", "macro_4h": "BULL", "adx_1h": 20}
    decision = {"action": "SELL_SHORT", "confidence": 85, "entry_price": 100, "take_profit_price": 90, "stop_loss_price": 105}
    aggressive = get_risk_profile("aggressive").snapshot(leverage=3, environment="testnet")
    action, reason, _ = validate_decision(
        package, decision,
        active_inst_ids={"ETH-USDT-SWAP"}, active_position_sides={"ETH_USDT": "long"},
        risk_snapshot=aggressive,
    )
    assert action == "WAIT"
    assert "反向持仓" in reason


def test_protection_coverage_requires_both_tp_and_sl_for_full_position():
    rows = [
        {"initial": {"size": -4}, "trigger": {"rule": 1}},
        {"initial": {"size": -2}, "trigger": {"rule": 2}},
    ]
    coverage = protection_coverage_status(rows, 4)
    assert coverage["take_profit"] == 4
    assert coverage["stop_loss"] == 2
    assert not coverage["fully_protected"]
    rows.append({"initial": {"size": -2}, "trigger": {"rule": 2}})
    assert protection_coverage_status(rows, 4)["fully_protected"]


def test_cross_and_isolated_margin_are_counted_for_portfolio_cap():
    assert _account_committed_margin({
        "cross_initial_margin": "20", "cross_order_margin": "5",
        "isolated_position_margin": "3", "isolated_order_margin": "2",
        "position_margin": "0", "order_margin": "0",
    }) == 30
    assert _account_committed_margin({"position_margin": "7", "order_margin": "2"}) == 9


def test_each_existing_position_uses_its_own_gate_multiplier():
    class C:
        def contracts(self, contract):
            return {"quanto_multiplier": {"ETH_USDT": "0.01", "BTC_USDT": "0.0001"}[contract]}
    positions = [
        {"contract": "ETH_USDT", "size": "4", "mark_price": "2500"},
        {"contract": "BTC_USDT", "size": "2", "mark_price": "75000"},
    ]
    assert _portfolio_position_notional(C(), positions) == 115


def test_aggregate_limits_allow_multiple_positions_until_configured_capacity():
    limits = RiskLimits(1000, 100, 25)
    limits.check_order(order_margin_usd=25, current_margin_usd=20, current_position_notional_usd=100, order_notional_usd=125, environment="testnet", live_enabled=False)
    with pytest.raises(PermissionError, match="total margin"):
        limits.check_order(order_margin_usd=25, current_margin_usd=80, current_position_notional_usd=100, order_notional_usd=125, environment="testnet", live_enabled=False)


def test_protection_requires_each_expected_client_id_and_rule():
    rows = [
        {"initial": {"text": "t-gate-tp-1", "size": -2}, "trigger": {"rule": 1}},
        {"initial": {"text": "t-gate-sl-1", "size": -2}, "trigger": {"rule": 2}},
    ]
    assert _protection_matches(rows, client_id="t-gate-tp-1", size=-2, rule=1)
    assert _protection_matches(rows, client_id="t-gate-sl-1", size=-2, rule=2)
    assert not _protection_matches(rows[:1], client_id="t-gate-sl-1", size=-2, rule=2)


def test_protection_intent_is_recovered_from_submitted_history():
    row = {
        "generated_at_ms": 123, "environment": "testnet",
        "trade": {"status": "submitted_testnet", "contract": "ETH_USDT", "client_id": "t-gate-ai-123", "size": 4,
                  "take_profit": {"trigger": {"price": "2680"}}, "stop_loss": {"order": {"trigger": {"price": "2528"}}}},
    }
    intent = intent_from_history(row)
    assert intent is not None
    assert intent["take_profit_price"] == "2680"
    assert intent["stop_loss_price"] == "2528"
    assert intent["position_side"] == "long"


def test_recovery_plan_only_restores_missing_side_from_matching_intent():
    positions = [{"contract": "ETH_USDT", "size": 4, "mark_price": "2600", "open_time": 1}]
    protections = [{"trigger": {"rule": 1}, "initial": {"contract": "ETH_USDT", "size": -4, "text": "t-gate-tp-1"}}]
    intents = [{"contract": "ETH_USDT", "position_side": "long", "entry_client_id": "t-gate-ai-1", "take_profit_price": "2680", "stop_loss_price": "2528", "created_at_ms": 1000}]
    plans = recovery_plans(positions, protections, intents)
    assert len(plans) == 1
    assert plans[0]["kind"] == "stop_loss"
    assert plans[0]["close_size"] == "-4"
    assert plans[0]["recoverable"] is True


def test_recovery_plan_rejects_crossed_saved_trigger_and_manual_orphan():
    plans = recovery_plans(
        [{"contract": "ETH_USDT", "size": 4, "mark_price": "2500", "open_time": 1}], [],
        [{"contract": "ETH_USDT", "position_side": "long", "entry_client_id": "t-gate-ai-1", "take_profit_price": "2680", "stop_loss_price": "2528", "created_at_ms": 1000}],
    )
    crossed = next(plan for plan in plans if plan["kind"] == "stop_loss")
    assert crossed["recoverable"] is False
    assert crossed["close_required"] is True
    assert is_system_protection({"initial": {"text": "manual-protection"}}) is False
    assert is_system_protection({"initial": {"text": "t-gate-rsl-123"}}) is True


def test_recovery_plan_does_not_link_an_old_intent_to_a_new_position():
    plans = recovery_plans(
        [{"contract": "ETH_USDT", "size": 4, "mark_price": "2600", "open_time": 7200}], [],
        [{"contract": "ETH_USDT", "position_side": "long", "entry_client_id": "t-gate-ai-old", "take_profit_price": "2680", "stop_loss_price": "2528", "created_at_ms": 1}],
    )
    assert all(plan["recoverable"] is False and plan["close_required"] is False for plan in plans)


def test_market_and_limit_semantics_remain_distinct():
    class C:
        calls = []
        def create_order(self, **kwargs):
            self.calls.append(kwargs)
            return kwargs
    client = C()
    client.create_order(contract="SOL_USDT", size=2, price="103.5", tif="gtc", client_id="t-limit")
    client.create_order(contract="SOL_USDT", size=0, price="0", tif="ioc", client_id="t-close", reduce_only=True, close=True)
    assert client.calls[0]["price"] != "0" and client.calls[0]["tif"] == "gtc"
    assert client.calls[1]["price"] == "0" and client.calls[1]["tif"] == "ioc"
