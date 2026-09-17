import json
import requests
import pytest
from concurrent.futures import Future
from decimal import Decimal
from gate_quant.client import GateFuturesClient, AmbiguousOrderError
from gate_quant.config import GateSettings
from gate_quant.risk import RiskLimits
from gate_quant.service import GateTradingService, protection_coverage_status
from gate_quant.ai_worker import _extract_decision_object, _extract_response_object, _as_instruction_list, _order_size_for_margin, _protection_matches, _reduce_crossed_protection_slice, _execution_enabled, _account_committed_margin, _portfolio_position_notional, _requote_decision, _confirm_pending_requotes, _preflight_candidate_quotes, _run_serial_candidates, _entry_execution_plan, _breakout_compatible_size, _manage_breakout_plans, _untracked_trigger_entries
from gate_quant.execution_journal import ExecutionJournal
from gate_quant.strategy_adapter import validate_decision
from gate_quant.risk_profiles import get_risk_profile
from r20_gateway.scheduler import JOBS, GatewayScheduler
from gate_quant.safety import classify_error, cooldown_state, daily_loss_state, position_age_stage, reconcile_exchange_state
from gate_quant.protection_lifecycle import actionable_recovery_plans, intent_from_history, is_system_protection, recovery_plans
from scripts.sync_gate_ledger import _ledger_row


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


def test_requote_repairs_stale_long_without_market_conversion():
    updated, meta = _requote_decision({
        "action": "BUY_LONG", "entry_price": 2500, "take_profit_price": 2600,
        "stop_loss_price": 2450,
    }, 2400)
    assert meta["requote_status"] == "requoted"
    assert updated["entry_price"] == pytest.approx(2400)
    assert updated["take_profit_price"] == pytest.approx(2500)
    assert updated["stop_loss_price"] == pytest.approx(2350)


def test_requote_repairs_stale_short_and_preserves_limit_intent():
    updated, meta = _requote_decision({
        "action": "SELL_SHORT", "entry_price": 2400, "take_profit_price": 2300,
        "stop_loss_price": 2450,
    }, 2500)
    assert meta["requote_status"] == "requoted"
    assert updated["entry_price"] == pytest.approx(2500)
    assert updated["take_profit_price"] == pytest.approx(2400)
    assert updated["stop_loss_price"] == pytest.approx(2550)


def test_requote_extreme_move_fails_closed():
    updated, meta = _requote_decision({"action": "BUY_LONG", "entry_price": 2500}, 2200)
    assert meta["requote_status"] == "blocked_extreme_deviation"
    assert updated["entry_price"] == 2500


@pytest.mark.parametrize(("action", "intent", "entry", "expected_type", "expected_tif"), [
    ("BUY_LONG", "immediate", 100.05, "immediate", "ioc"),
    ("BUY_LONG", "retracement", 99, "retracement", "gtc"),
    ("BUY_LONG", "breakout", 102, "breakout", "ioc"),
    ("SELL_SHORT", "immediate", 99.95, "immediate", "ioc"),
    ("SELL_SHORT", "retracement", 101, "retracement", "gtc"),
    ("SELL_SHORT", "breakout", 98, "breakout", "ioc"),
])
def test_explicit_entry_intent_maps_to_gate_order_semantics(action, intent, entry, expected_type, expected_tif):
    plan = _entry_execution_plan(
        action=action, entry_intent=intent, entry_price=entry,
        quote={"highest_bid": "99.9", "lowest_ask": "100.1"},
        max_slippage_pct=0.003, price_tick="0.1", expiration_seconds=840,
    )
    assert plan["valid"] is True
    assert plan["order_type"] == expected_type
    assert plan["tif"] == expected_tif
    if intent == "breakout":
        assert plan["trigger_rule"] == (1 if action == "BUY_LONG" else 2)
        assert plan["expiration_seconds"] == 840


@pytest.mark.parametrize(("action", "intent", "entry"), [
    ("BUY_LONG", "retracement", 101),
    ("BUY_LONG", "breakout", 99),
    ("SELL_SHORT", "retracement", 99),
    ("SELL_SHORT", "breakout", 101),
    ("BUY_LONG", "immediate", 105),
])
def test_entry_intent_price_relationship_mismatch_fails_closed(action, intent, entry):
    plan = _entry_execution_plan(
        action=action, entry_intent=intent, entry_price=entry,
        quote={"highest_bid": "99.9", "lowest_ask": "100.1"},
        max_slippage_pct=0.003, price_tick="0.1", expiration_seconds=840,
    )
    assert plan["valid"] is False


def test_ioc_tick_rounding_never_crosses_slippage_boundary():
    long_plan = _entry_execution_plan(
        action="BUY_LONG", entry_intent="breakout", entry_price=100.01,
        quote={"highest_bid": "99.9", "lowest_ask": "100"},
        max_slippage_pct=0.003, price_tick="0.1", expiration_seconds=840,
    )
    short_plan = _entry_execution_plan(
        action="SELL_SHORT", entry_intent="breakout", entry_price=99.99,
        quote={"highest_bid": "100", "lowest_ask": "100.1"},
        max_slippage_pct=0.003, price_tick="0.1", expiration_seconds=840,
    )
    assert Decimal(long_plan["price"]) <= Decimal("100.01") * Decimal("1.003")
    assert Decimal(short_plan["price"]) >= Decimal("99.99") * Decimal("0.997")
    assert Decimal(long_plan["trigger_price"]) > Decimal("100")
    assert Decimal(short_plan["trigger_price"]) < Decimal("100")


def test_breakout_size_never_rounds_fractional_contracts_up():
    assert _breakout_compatible_size(Decimal("6.9")) == Decimal("6")
    assert _breakout_compatible_size(Decimal("-6.9")) == Decimal("-6")
    with pytest.raises(ValueError):
        _breakout_compatible_size(Decimal("0.9"))


def test_deferred_signal_requires_same_direction_next_cycle(monkeypatch, tmp_path):
    import gate_quant.ai_worker as worker
    monkeypatch.setattr(worker, "PENDING_REQUOTES", tmp_path / "pending.json")
    pending = {"ETH_USDT": {"action": "BUY_LONG", "created_at_ms": 1000}}
    decisions = {"ETH_USDT": {"decision": {"action": "SELL_SHORT", "confidence": 90}}}
    assert _confirm_pending_requotes(pending, decisions, min_confidence=50, now_ms=2000) == set()
    assert pending == {}
    pending = {"ETH_USDT": {"action": "BUY_LONG", "created_at_ms": 1000}}
    decisions["ETH_USDT"]["decision"] = {"action": "BUY_LONG", "confidence": 90}
    assert _confirm_pending_requotes(pending, decisions, min_confidence=50, now_ms=2000) == {"ETH_USDT"}


def test_deferred_high_confidence_candidate_does_not_hide_next_signal(monkeypatch, tmp_path):
    import gate_quant.ai_worker as worker
    monkeypatch.setattr(worker, "PENDING_REQUOTES", tmp_path / "pending.json")
    class Quotes:
        def tickers(self, symbol):
            return [{"mark_price": {"ETH_USDT": "95", "SOL_USDT": "100"}[symbol]}]
    candidates = [
        {"instId": "ETH_USDT", "indicators": {"last": 95}, "decision": {"action": "BUY_LONG", "confidence": 80, "entry_price": 100, "take_profit_price": 110, "stop_loss_price": 95}},
        {"instId": "SOL_USDT", "indicators": {"last": 100}, "decision": {"action": "BUY_LONG", "confidence": 78, "entry_price": 101, "take_profit_price": 110, "stop_loss_price": 97}},
    ]
    pending = {}
    eligible, blocked = _preflight_candidate_quotes(Quotes(), candidates, pending, set(), now_ms=2000)
    assert [row["instId"] for row in eligible] == ["SOL_USDT"]
    assert blocked[0]["contract"] == "ETH_USDT"
    assert blocked[0]["status"] == "blocked_pending_reconfirmation"
    assert "ETH_USDT" in pending


def test_rejected_eth_reconfirmation_does_not_block_other_second_cycle_signal(monkeypatch, tmp_path):
    import gate_quant.ai_worker as worker
    monkeypatch.setattr(worker, "PENDING_REQUOTES", tmp_path / "pending.json")
    pending = {"ETH_USDT": {"contract": "ETH_USDT", "action": "BUY_LONG", "confidence": 80,
                            "entry_price": 2500, "created_at_ms": 1000}}
    decisions = {
        "ETH_USDT": {"decision": {"action": "WAIT", "confidence": 82}},
        "BTC_USDT": {"decision": {"action": "BUY_LONG", "confidence": 79}},
    }
    confirmed = _confirm_pending_requotes(pending, decisions, min_confidence=72, now_ms=2000)
    assert confirmed == set()
    assert "ETH_USDT" not in pending
    assert decisions["BTC_USDT"]["decision"]["action"] == "BUY_LONG"


def test_serial_candidates_block_does_not_consume_two_entry_budget():
    candidates = [{"id": "ETH", "instId": "ETH_USDT"}, {"id": "SOL", "instId": "SOL_USDT"}, {"id": "BTC", "instId": "BTC_USDT"}, {"id": "XRP", "instId": "XRP_USDT"}]
    def execute(candidate, sequence):
        if candidate["id"] == "ETH":
            return {"trade": {"contract": "ETH", "status": "blocked_pending_reconfirmation"}, "submitted": False, "stop_cycle": False}
        return {"trade": {"contract": candidate["id"], "status": "submitted_live", "client_id": f"t-{sequence}"}, "submitted": True, "stop_cycle": False}
    outcomes, submitted = _run_serial_candidates(candidates, 2, execute)
    assert [row["contract"] for row in outcomes] == ["ETH", "SOL", "BTC", "XRP_USDT"]
    assert [row["contract"] for row in submitted] == ["SOL", "BTC"]
    assert submitted[0]["client_id"] != submitted[1]["client_id"]
    assert outcomes[-1]["status"] == "blocked_cycle_entry_limit"


def test_serial_candidates_ambiguous_order_stops_following_entries():
    def execute(candidate, sequence):
        return {"trade": {"contract": candidate, "status": "order_submission_ambiguous"}, "submitted": False, "stop_cycle": True}
    outcomes, submitted = _run_serial_candidates(["ETH", "SOL"], 2, execute)
    assert [row["contract"] for row in outcomes] == ["ETH"]
    assert submitted == []


def test_position_response_list_is_normalized_before_leverage_lookup():
    response = [{"contract": "BTC_USDT", "size": "2", "pos_margin_mode": "cross", "leverage": "5"}]
    position = next((row for row in response if isinstance(row, dict) and float(row.get("size") or 0) != 0), {})
    assert position["contract"] == "BTC_USDT"
    assert position["pos_margin_mode"] == "cross"


def test_risk_profile_limits_entries_per_cycle():
    assert get_risk_profile("standard").max_entries_per_cycle == 1
    assert get_risk_profile("aggressive").max_entries_per_cycle == 2
    GateSettings(environment="testnet", risk_profile="standard", max_entries_per_cycle=1).validate()
    with pytest.raises(ValueError, match="cannot exceed 1"):
        GateSettings(environment="testnet", risk_profile="standard", max_entries_per_cycle=2).validate()


def test_gate_position_close_maps_native_lifecycle_fields():
    row = _ledger_row({
        "contract": "BTC_USDT", "text": "t-gate-time-1", "side": "long",
        "long_price": "76815.334991974318", "short_price": "78231.893258426966",
        "max_size": "546", "first_open_time": 1789336927, "time": 1789395332,
        "lever": "5", "margin_mode": "cross", "pnl": "83.165703925875",
        "pnl_pnl": "88.25158", "pnl_fee": "-4.42752016", "pnl_fund": "-0.658355914125",
    }, quanto_multiplier=0.0001)
    assert row is not None
    assert row["side"] == "多"
    assert row["lever"] == "5x"
    assert row["open_px"] == pytest.approx(76815.334991974318)
    assert row["close_px"] == pytest.approx(78231.893258426966)
    assert row["open_time"] == "2026-09-14 06:02:07"
    assert row["close_time"] == "2026-09-14 22:15:32"
    assert row["margin"] == pytest.approx(838.82345771)
    assert row["net_pnl"] == pytest.approx(83.16570393)
    assert row["fee"] == pytest.approx(-4.42752016)
    assert row["funding_fee"] == pytest.approx(-0.65835591)
    assert row["hold_duration"] == "16小时13分"
    assert row["exit_reason"] == "超过最大持仓时间自动平仓"


def test_gateway_scheduler_includes_gate_ledger_and_self_improvement():
    jobs = {job.name: job for job in JOBS}
    assert jobs["anomaly_audit"].interval_seconds == 60
    assert jobs["anomaly_audit"].script == "-m gate_quant.anomaly_audit"
    assert jobs["gate_ledger"].interval_seconds == 15 * 60
    assert jobs["self_improvement"].schedule_key == "self_improvement_times"


def test_gateway_does_not_launch_reconciler_while_trader_is_running(monkeypatch):
    import r20_gateway.scheduler as scheduler_module

    class Store:
        def set_state(self, *args):
            raise AssertionError("blocked job must not update its schedule timestamp")

    reconcile = next(spec for spec in JOBS if spec.name == "execution_reconcile")
    scheduler = GatewayScheduler(Store())
    scheduler.running["trader"] = Future()
    monkeypatch.setattr(scheduler_module, "current_jobs", lambda: (reconcile,))
    monkeypatch.setattr(scheduler_module, "load_schedule", lambda: {})
    monkeypatch.setattr(scheduler, "due", lambda spec, now, schedule: True)
    try:
        assert scheduler.tick() == []
    finally:
        scheduler.shutdown()


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


def test_stop_cooldown_is_scoped_to_the_losing_contract(tmp_path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps([
        {"status": "closed", "inst": "ETH_USDT", "close_time": "2026-09-14 03:49:48", "net_pnl": -2.7},
    ]), encoding="utf-8")
    now = __import__("datetime").datetime.strptime("2026-09-14 04:00:00", "%Y-%m-%d %H:%M:%S").timestamp()
    assert cooldown_state(ledger, cooldown_seconds=1800, contract="ETH_USDT", now=now)["active"]
    assert not cooldown_state(ledger, cooldown_seconds=1800, contract="BTC_USDT", now=now)["active"]


def test_safety_error_classification_is_explicit():
    assert classify_error("Gate API INVALID_KEY (HTTP 401)") == "authentication"
    assert classify_error("Gate request timed out") == "timeout"


def test_risk_limit_errors_are_classified_as_blocked_not_worker_crashes():
    limits = RiskLimits(1000, 1000, 300)
    with pytest.raises(PermissionError, match="position notional"):
        limits.check_order(order_margin_usd=10, current_margin_usd=0, current_position_notional_usd=1001, order_notional_usd=1, environment="testnet", live_enabled=False)


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


def test_public_get_retries_transient_gate_500_without_retrying_writes(monkeypatch):
    class Response:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self.payload = payload
            self.reason = "Internal Server Error" if status_code == 500 else "OK"
            self.content = json.dumps(payload).encode()

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(response=self)

        def json(self):
            return self.payload

    class Session:
        def __init__(self):
            self.calls = 0
            self.proxies = {}

        def request(self, *args, **kwargs):
            self.calls += 1
            return Response(500, {"label": "INTERNAL", "message": "Internal error"}) if self.calls == 1 else Response(200, [{"contract": "BTC_USDT"}])

    session = Session()
    monkeypatch.setattr("gate_quant.client.time.sleep", lambda _: None)
    assert GateFuturesClient(settings(), session).tickers() == [{"contract": "BTC_USDT"}]
    assert session.calls == 2


def test_public_get_stops_after_bounded_timeout_retries(monkeypatch):
    class Session:
        def __init__(self):
            self.calls = 0
            self.proxies = {}

        def request(self, *args, **kwargs):
            self.calls += 1
            raise requests.Timeout("offline")

    session = Session()
    monkeypatch.setattr("gate_quant.client.time.sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="request timed out"):
        GateFuturesClient(settings(), session).candlesticks("BTC_USDT")
    assert session.calls == 3


@pytest.mark.parametrize(("mode", "expected_auto_size"), [
    ("dual_long", "close_long"),
    ("dual_short", "close_short"),
])
def test_dual_mode_close_uses_gate_native_auto_size(mode, expected_auto_size):
    class Capture:
        def __init__(self): self.payload = None; self.settings = settings()
        def _request(self, method, endpoint, *, payload=None, private=False):
            self.payload = payload
            return {"id": "close-1"}
        _api_size = staticmethod(GateFuturesClient._api_size)
        create_order = GateFuturesClient.create_order
        close_position = GateFuturesClient.close_position

    client = Capture()
    client.close_position(contract="BTC_USDT", client_id="t-close-dual", position_mode=mode)
    assert client.payload == {
        "contract": "BTC_USDT", "size": 0, "price": "0", "tif": "ioc",
        "text": "t-close-dual", "reduce_only": True, "close": False,
        "auto_size": expected_auto_size,
    }


def test_single_mode_close_keeps_gate_close_flag():
    class Capture:
        def __init__(self): self.payload = None; self.settings = settings()
        def _request(self, method, endpoint, *, payload=None, private=False):
            self.payload = payload
            return {"id": "close-1"}
        _api_size = staticmethod(GateFuturesClient._api_size)
        create_order = GateFuturesClient.create_order
        close_position = GateFuturesClient.close_position

    client = Capture()
    client.close_position(contract="BTC_USDT", client_id="t-close-single", position_mode="single")
    assert client.payload["size"] == 0
    assert client.payload["close"] is True
    assert client.payload["reduce_only"] is True
    assert "auto_size" not in client.payload


def test_invalid_dual_mode_auto_size_fails_closed_before_request():
    client = GateFuturesClient(settings())
    with pytest.raises(ValueError, match="auto_size"):
        client.create_order(contract="BTC_USDT", size=0, client_id="t-invalid", auto_size="close_both")
    with pytest.raises(ValueError, match="size=0"):
        client.create_order(contract="BTC_USDT", size=1, client_id="t-invalid", reduce_only=True, auto_size="close_long")
    with pytest.raises(ValueError, match="reduce_only=true"):
        client.create_order(contract="BTC_USDT", size=0, client_id="t-invalid", reduce_only=False, auto_size="close_long")


def test_close_timeout_reconciles_by_client_id_without_resubmission():
    class C:
        def __init__(self): self.close_calls = 0; self.lookup_calls = 0
        def close_position(self, **kwargs):
            self.close_calls += 1
            raise AmbiguousOrderError("timeout")
        def find_by_client_id(self, client_id, contract):
            self.lookup_calls += 1
            return {"id": "reconciled-close", "text": client_id}

    client = C()
    result = GateTradingService(client, RiskLimits(100, 20, 10)).close_position_safely(
        contract="BTC_USDT", client_id="t-close-timeout", position_mode="dual_long",
    )
    assert result["reconciled"] is True
    assert client.close_calls == 1
    assert client.lookup_calls == 1


def test_position_age_enters_review_before_absolute_close():
    position = {"create_time": 1_000}
    review = position_age_stage(position, review_after_seconds=16 * 3600, force_close_after_seconds=36 * 3600, now=1_000 + 16 * 3600)
    forced = position_age_stage(position, review_after_seconds=16 * 3600, force_close_after_seconds=36 * 3600, now=1_000 + 36 * 3600)
    assert review == {"stage": "review", "age_seconds": 16 * 3600}
    assert forced == {"stage": "force_close", "age_seconds": 36 * 3600}


def test_absolute_position_limit_must_follow_review_threshold():
    with pytest.raises(ValueError, match="must be greater"):
        GateSettings(max_position_age_seconds=16 * 3600, absolute_max_position_age_seconds=16 * 3600).validate()


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
                {"initial": {"size": -2, "is_reduce_only": True}, "trigger": {"rule": 1}},
                {"initial": {"size": -2, "is_reduce_only": True}, "trigger": {"rule": 2}},
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
        {"initial": {"size": -4, "is_reduce_only": True}, "trigger": {"rule": 1}},
        {"initial": {"size": -2, "is_reduce_only": True}, "trigger": {"rule": 2}},
    ]
    coverage = protection_coverage_status(rows, 4)
    assert coverage["take_profit"] == 4
    assert coverage["stop_loss"] == 2
    assert not coverage["fully_protected"]
    rows.append({"initial": {"size": -2, "is_reduce_only": True}, "trigger": {"rule": 2}})
    assert protection_coverage_status(rows, 4)["fully_protected"]


def test_full_close_protections_cover_decimal_hedge_position():
    rows = [
        {"order_type": "close-long-position", "initial": {"size": 0, "auto_size": "close_long", "is_reduce_only": True}, "trigger": {"rule": 1}},
        {"order_type": "close-long-position", "initial": {"size": 0, "auto_size": "close_long", "is_reduce_only": True}, "trigger": {"rule": 2}},
    ]
    coverage = protection_coverage_status(rows, Decimal("7.7"))
    assert coverage["take_profit"] == Decimal("7.7")
    assert coverage["stop_loss"] == Decimal("7.7")
    assert coverage["fully_protected"]


def test_wrong_side_full_close_does_not_cover_position():
    rows = [
        {"order_type": "close-short-position", "initial": {"size": 0, "auto_size": "close_short", "is_reduce_only": True}, "trigger": {"rule": 1}},
        {"order_type": "close-short-position", "initial": {"size": 0, "auto_size": "close_short", "is_reduce_only": True}, "trigger": {"rule": 2}},
    ]
    assert not protection_coverage_status(rows, Decimal("7.7"))["fully_protected"]


def test_non_reduce_only_trigger_cannot_count_as_position_protection():
    rows = [
        {"initial": {"size": -4, "is_reduce_only": False}, "trigger": {"rule": 1}},
        {"initial": {"size": -4, "is_reduce_only": False}, "trigger": {"rule": 2}},
    ]
    coverage = protection_coverage_status(rows, 4)
    assert coverage["take_profit"] == 0
    assert coverage["stop_loss"] == 0
    assert not coverage["fully_protected"]


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
        {"initial": {"text": "t-gate-tp-1", "size": -2, "is_reduce_only": True}, "trigger": {"rule": 1}},
        {"initial": {"text": "t-gate-sl-1", "size": -2, "is_reduce_only": True}, "trigger": {"rule": 2}},
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


def test_protection_intent_recovers_new_planned_price_format():
    row = {
        "environment": "testnet",
        "generated_at_ms": 1234,
        "trade": {
            "status": "submitted_testnet",
            "contract": "ETH_USDT",
            "size": 4,
            "client_id": "t-gate-ai-1234",
            "take_profit": {"planned_price": "110"},
            "stop_loss": {"planned_price": "90"},
        },
    }
    intent = intent_from_history(row)
    assert intent is not None
    assert intent["take_profit_price"] == "110"
    assert intent["stop_loss_price"] == "90"


def test_recovery_plan_only_restores_missing_side_from_matching_intent():
    positions = [{"contract": "ETH_USDT", "size": 4, "mark_price": "2600", "open_time": 1}]
    protections = [{"trigger": {"rule": 1}, "initial": {"contract": "ETH_USDT", "size": -4, "text": "t-gate-tp-1", "is_reduce_only": True}}]
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


def test_crossed_stop_suppresses_same_contract_take_profit_repair():
    protections = [
        {"trigger": {"rule": 1}, "initial": {"contract": "BTC_USDT", "size": -98, "text": "t-gate-tp-old", "is_reduce_only": True}},
        {"trigger": {"rule": 2}, "initial": {"contract": "BTC_USDT", "size": -98, "text": "t-gate-sl-old", "is_reduce_only": True}},
    ]
    plans = recovery_plans(
        [{"contract": "BTC_USDT", "size": 197, "mark_price": "76327.6", "open_time": 1}], protections,
        [{"contract": "BTC_USDT", "position_side": "long", "entry_client_id": "t-gate-ai-new",
          "take_profit_price": "79500", "stop_loss_price": "76950", "created_at_ms": 1000}],
    )
    actionable = actionable_recovery_plans(plans)
    assert len(actionable) == 1
    assert actionable[0]["kind"] == "stop_loss"
    assert actionable[0]["close_required"] is True
    assert actionable[0]["missing_size"] == "99"
    assert actionable[0]["close_size"] == "-99"


def test_partial_crossed_stop_plan_keeps_reduction_size_not_full_position():
    plans = actionable_recovery_plans([
        {"contract": "BTC_USDT", "kind": "take_profit", "missing_size": "99", "close_size": "-99", "close_required": False},
        {"contract": "BTC_USDT", "kind": "stop_loss", "missing_size": "99", "close_size": "-99", "close_required": True},
    ])
    assert plans == [{
        "contract": "BTC_USDT", "kind": "stop_loss", "missing_size": "99",
        "close_size": "-99", "close_required": True,
    }]

    class Service:
        def __init__(self):
            self.reductions = []

        def reduce_position_safely(self, **kwargs):
            self.reductions.append(kwargs)
            return {"status": "finished", "size": str(kwargs["size"])}

        def close_position_safely(self, **kwargs):
            raise AssertionError("partial protection recovery must never flatten the contract")

    service = Service()
    reduced_size, result = _reduce_crossed_protection_slice(service, plans[0], "t-gate-pred-test")
    assert reduced_size == Decimal("-99")
    assert service.reductions == [{"contract": "BTC_USDT", "size": Decimal("-99"), "client_id": "t-gate-pred-test"}]
    assert result["size"] == "-99"


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


def test_gate_native_breakout_payload_uses_price_order_and_bounded_ioc():
    class Response:
        content = b'{"id":123}'
        def raise_for_status(self): pass
        def json(self): return {"id": 123}
    class Session:
        def __init__(self): self.calls = []; self.proxies = {}
        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return Response()
    session = Session()
    client = GateFuturesClient(settings(), session=session)
    result = client.create_trigger_entry_order(
        contract="BTC_USDT", size=2, trigger_price="77250", execution_price="77481.8",
        rule=1, client_id="t-gate-ai-breakout", expiration=86400,
    )
    assert result == {"id": 123}
    method, url, kwargs = session.calls[0]
    payload = json.loads(kwargs["data"])
    assert method == "POST" and url.endswith("/futures/usdt/price_orders")
    assert payload["initial"] == {"contract": "BTC_USDT", "size": 2, "price": "77481.8", "tif": "ioc", "reduce_only": False, "close": False, "text": "t-gate-ai-breakout"}
    assert payload["trigger"] == {"price": "77250", "rule": 1, "expiration": 86400, "strategy_type": 0, "price_type": 0}


def test_gate_native_breakout_rejects_subday_exchange_expiration():
    client = GateFuturesClient(settings())
    with pytest.raises(ValueError, match="86400"):
        client.create_trigger_entry_order(
            contract="BTC_USDT", size=1, trigger_price="77250", execution_price="77481.8",
            rule=1, client_id="t-gate-ai-breakout", expiration=840,
        )


def test_trigger_entries_and_protections_are_classified_separately(monkeypatch):
    client = GateFuturesClient(settings())
    rows = [
        {"id": 1, "initial": {"contract": "BTC_USDT", "size": 2, "text": "t-gate-ai-x", "reduce_only": False}, "trigger": {"rule": 1}},
        {"id": 2, "initial": {"contract": "BTC_USDT", "size": -2, "text": "t-gate-rsl-x", "reduce_only": True}, "trigger": {"rule": 2}},
    ]
    monkeypatch.setattr(client, "price_orders", lambda **kwargs: rows)
    assert [row["id"] for row in client.trigger_entry_orders("BTC_USDT")] == [1]
    assert [row["id"] for row in client.protection_orders("BTC_USDT")] == [2]


def test_untracked_trigger_entry_is_fail_closed_input_not_a_protection():
    plans = [
        {"id": "501", "initial": {"contract": "BTC_USDT", "text": "t-gate-ai-known"}},
        {"id": "502", "initial": {"contract": "SOL_USDT", "text": "manual-plan"}},
    ]
    intents = [{"client_id": "t-gate-ai-known", "order_id": "501", "status": "awaiting_trigger"}]
    assert _untracked_trigger_entries(plans, intents) == [
        {"order_id": "502", "client_id": "manual-plan", "contract": "SOL_USDT"}
    ]


def test_breakout_timeout_reconciles_by_client_id_without_resubmit():
    class Client:
        create_calls = 0
        find_calls = 0
        def create_trigger_entry_order(self, **kwargs):
            self.create_calls += 1
            raise AmbiguousOrderError("timeout")
        def find_trigger_entry_by_client_id(self, client_id, contract):
            self.find_calls += 1
            return {"id": "501", "status": "open", "initial": {"text": client_id, "contract": contract}}
    client = Client()
    service = GateTradingService(client, RiskLimits(1000, 1000, 1000))
    result = service.place_trigger_entry(
        contract="BTC_USDT", size=2, trigger_price="101", execution_price="101.3",
        rule=1, client_id="t-gate-ai-timeout", expiration=840,
        risk={"order_margin_usd": 10, "current_margin_usd": 0, "current_position_notional_usd": 0,
              "order_notional_usd": 20, "environment": "testnet", "live_enabled": False},
    )
    assert result["reconciled"] is True
    assert client.create_calls == 1
    assert client.find_calls == 1


def test_next_ai_wait_cancels_untriggered_breakout_plan(tmp_path):
    journal = ExecutionJournal(tmp_path / "execution.db")
    intent = journal.prepare({
        "client_id": "t-gate-ai-plan", "environment": "testnet", "contract": "BTC_USDT",
        "requested_size": "2", "baseline_position_size": "0", "entry_price": "101",
        "take_profit_price": "110", "stop_loss_price": "95", "order_type": "breakout",
        "entry_action": "BUY_LONG", "order_notional_usdt": "20", "estimated_margin_usdt": "10",
        "created_at_ms": 1000,
    })
    intent = journal.update(intent["client_id"], "awaiting_trigger", order_id="501", order_status="waiting_trigger")
    class Client:
        cancelled = False
        def price_order(self, order_id):
            return {"id": order_id, "status": "open", "initial": {"contract": "BTC_USDT", "text": "t-gate-ai-plan"}}
        def cancel_trigger_entry_order(self, order_id):
            self.cancelled = True
            return {"id": order_id, "status": "finished"}
        def trigger_entry_orders(self, contract, status="open"):
            return [] if self.cancelled else [{"id": "501", "status": "open"}]
    client = Client()
    kept, outcomes, stop = _manage_breakout_plans(
        client, journal, [intent], {"BTC_USDT": {"decision": {"action": "WAIT", "entry_intent": ""}}}, settings()
    )
    assert kept == set() and stop is False
    assert outcomes[0]["status"] == "breakout_cancelled_by_new_signal"
    assert journal.get(intent["client_id"])["status"] == "trigger_cancelled"


def test_same_breakout_plan_is_kept_without_blocking_other_contracts(tmp_path):
    journal = ExecutionJournal(tmp_path / "execution.db")
    intent = journal.prepare({
        "client_id": "t-gate-ai-plan", "environment": "testnet", "contract": "BTC_USDT",
        "requested_size": "2", "baseline_position_size": "0", "entry_price": "101",
        "take_profit_price": "110", "stop_loss_price": "95", "order_type": "breakout",
        "entry_action": "BUY_LONG", "order_notional_usdt": "20", "estimated_margin_usdt": "10",
        "created_at_ms": 1000,
    })
    intent = journal.update(intent["client_id"], "awaiting_trigger", order_id="501", order_status="waiting_trigger")
    kept, outcomes, stop = _manage_breakout_plans(
        object(), journal, [intent],
        {"BTC_USDT": {"decision": {"action": "BUY_LONG", "entry_intent": "breakout"}},
         "SOL_USDT": {"decision": {"action": "BUY_LONG", "entry_intent": "retracement"}}},
        settings(),
    )
    assert kept == {"BTC_USDT"} and stop is False
    assert outcomes[0]["status"] == "existing_breakout_plan_kept"
    assert "SOL_USDT" not in kept


def test_trigger_cancel_timeout_is_reconciled_by_fresh_open_query():
    class Client:
        def cancel_trigger_entry_order(self, order_id):
            raise RuntimeError("request timed out")
        def trigger_entry_orders(self, contract, status="open"):
            return []
    service = GateTradingService(Client(), RiskLimits(1000, 1000, 1000))
    result = service.cancel_trigger_entry_confirmed(contract="BTC_USDT", order_id="501")
    assert result == {"cancelled": True, "order_id": "501", "reconciled_after_error": True}


def test_protection_cancel_timeout_is_reconciled_before_rollback_continues():
    class Client:
        def cancel_protection_order(self, order_id):
            raise RuntimeError("request timed out")
        def protection_orders(self, contract=None):
            return []
    service = GateTradingService(Client(), RiskLimits(1000, 1000, 1000))
    result = service.cancel_protection_confirmed(order_id="701")
    assert result == {"cancelled": True, "order_id": "701", "reconciled_after_error": True}


class StopReplacementClient:
    def __init__(self, *, fail_new=False):
        self.fail_new = fail_new
        self.rows = [{
            "id": "old-sl", "id_string": "old-sl", "status": "open",
            "trigger": {"rule": 2, "price": "96.82"},
            "initial": {"contract": "SOL_USDT", "size": -17.9, "text": "t-gate-sl-old", "is_reduce_only": True},
        }]
        self.created = []

    def protection_orders(self, contract=None):
        return list(self.rows)

    def cancel_protection_order(self, order_id):
        self.rows = [row for row in self.rows if str(row.get("id")) != str(order_id)]
        return {"id": order_id, "status": "finished"}

    def create_protection_order(self, **kwargs):
        if self.fail_new and not str(kwargs["client_id"]).endswith("-rb"):
            raise RuntimeError("Gate rejected replacement")
        self.created.append(kwargs)
        row = {"id": kwargs["client_id"], "status": "open", "trigger": {"rule": kwargs["rule"], "price": kwargs["trigger_price"]},
               "initial": {"contract": kwargs["contract"], "size": kwargs["size"], "text": kwargs["client_id"], "is_reduce_only": True}}
        self.rows.append(row)
        return row

    def find_protection_by_client_id(self, client_id, contract):
        return next((row for row in self.rows if row["initial"]["text"] == client_id), None)


def test_stop_loss_update_replaces_existing_gate_rule_in_safe_order():
    client = StopReplacementClient()
    result = GateTradingService(client, RiskLimits(1, 1, 1)).replace_stop_loss_safely(
        contract="SOL_USDT", position_size="17.9", mark_price="101.49",
        trigger_price="99.2", client_id="t-gate-slu-test",
    )
    assert result["replaced"] is True
    assert len(client.rows) == 1
    assert client.rows[0]["trigger"]["price"] == "99.2"


def test_stop_loss_update_restores_old_stop_when_new_order_fails():
    client = StopReplacementClient(fail_new=True)
    with pytest.raises(RuntimeError, match="old stop-loss restored"):
        GateTradingService(client, RiskLimits(1, 1, 1)).replace_stop_loss_safely(
            contract="SOL_USDT", position_size="17.9", mark_price="101.49",
            trigger_price="99.2", client_id="t-gate-slu-test",
        )
    assert len(client.rows) == 1
    assert client.rows[0]["trigger"]["price"] == "96.82"


def test_stop_loss_update_rejects_looser_or_crossed_price_without_cancelling():
    client = StopReplacementClient()
    service = GateTradingService(client, RiskLimits(1, 1, 1))
    with pytest.raises(ValueError, match="does not tighten"):
        service.replace_stop_loss_safely(contract="SOL_USDT", position_size="17.9", mark_price="101.49", trigger_price="95", client_id="t-gate-slu-loose")
    with pytest.raises(ValueError, match="wrong side"):
        service.replace_stop_loss_safely(contract="SOL_USDT", position_size="17.9", mark_price="101.49", trigger_price="102", client_id="t-gate-slu-crossed")
    assert [row["id"] for row in client.rows] == ["old-sl"]
