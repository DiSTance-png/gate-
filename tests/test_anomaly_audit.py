import json

from gate_quant.anomaly_audit import collect_anomalies, persist_anomaly_history


NOW = 2_000_000_000_000
CONTRACTS = ("BTC_USDT", "ETH_USDT", "SOL_USDT", "DOGE_USDT", "SUI_USDT", "XRP_USDT")


def complete_decision(*, management=None):
    payload = {
        symbol: {"decision": {"action": "WAIT", "confidence": 60}}
        for symbol in CONTRACTS
    }
    payload.update({"generated_at_ms": NOW - 60_000, "position_management": management or []})
    return payload


def healthy_inputs(**overrides):
    values = {
        "environment": "testnet",
        "job_runs": [{"id": 1, "job_name": "trader", "status": "success", "started_at": "2033-05-18 03:32:00", "return_code": 0}],
        "decision": complete_decision(),
        "safety_snapshot": {"checked_at_ms": NOW - 30_000, "safe_for_new_risk": True},
        "trader_heartbeat": {"timestamp_ms": NOW - 30_000, "status": "running"},
        "reconciler_heartbeat": {"timestamp_ms": NOW - 10_000, "status": "ok"},
        "executions": [], "positions": [], "orders": [], "protections": [], "private_errors": [],
        "max_pending_age_seconds": 1800, "now_ms": NOW,
    }
    values.update(overrides)
    return values


def codes(report):
    return {row["code"] for row in report["items"]}


def test_successful_trader_run_does_not_hide_live_protection_gap():
    positions = [{"contract": "BTC_USDT", "size": "2", "mark_price": "50000", "initial_margin": "20"}]
    report = collect_anomalies(**healthy_inputs(
        positions=positions,
        decision=complete_decision(management=[{"contract": "BTC_USDT", "action": "HOLD"}]),
        safety_snapshot={"checked_at_ms": NOW - 30_000, "safe_for_new_risk": False},
    ))
    assert "protection_gap" in codes(report)
    item = next(row for row in report["items"] if row["code"] == "protection_gap")
    assert item["severity"] == "critical"
    assert item["contract"] == "BTC_USDT"


def test_execution_error_and_unprotected_fill_are_both_reported():
    report = collect_anomalies(**healthy_inputs(executions=[{
        "client_id": "t-gate-1", "contract": "ETH_USDT", "status": "manual_review",
        "filled_size": "4", "protected_size": "0", "last_error": "protection submit timeout",
        "updated_at_ms": NOW - 30_000,
    }]))
    assert {"execution_last_error", "execution_manual_review", "execution_protection_incomplete"} <= codes(report)


def test_internal_lifecycle_error_is_visible_even_when_job_succeeded():
    decision = complete_decision()
    decision["safety_status"] = {"lifecycle_actions": [{
        "action": "CLOSE_ABSOLUTE_MAX_AGE", "contract": "BTC_USDT",
        "error": "Gate API POSITION_DUAL_MODE", "category": "unknown",
    }]}
    report = collect_anomalies(**healthy_inputs(decision=decision))
    assert "lifecycle_action_failed" in codes(report)


def test_old_failed_job_is_not_current_after_newer_success():
    report = collect_anomalies(**healthy_inputs(job_runs=[
        {"id": 2, "job_name": "trader", "status": "success", "started_at": "2033-05-18 03:32:00", "return_code": 0},
        {"id": 1, "job_name": "trader", "status": "failed", "started_at": "2033-05-18 03:17:00", "return_code": 1, "detail": "old failure"},
    ]))
    assert "trader_run_failed" not in codes(report)


def test_stale_closed_safety_snapshot_is_distinguished_from_live_safe_state():
    report = collect_anomalies(**healthy_inputs(
        safety_snapshot={"checked_at_ms": NOW - 30 * 60 * 1000, "safe_for_new_risk": False},
    ))
    assert "safety_snapshot_stale" in codes(report)
    assert "safety_snapshot_mismatch" in codes(report)


def test_open_position_without_ai_management_is_visible():
    positions = [{"contract": "SOL_USDT", "size": "1", "mark_price": "100", "initial_margin": "10"}]
    protections = [
        {"initial": {"contract": "SOL_USDT", "size": "-1", "reduce_only": True}, "trigger": {"rule": 1}},
        {"initial": {"contract": "SOL_USDT", "size": "-1", "reduce_only": True}, "trigger": {"rule": 2}},
    ]
    report = collect_anomalies(**healthy_inputs(positions=positions, protections=protections))
    assert "position_management_missing" in codes(report)
    assert "protection_gap" not in codes(report)


def test_wrong_side_and_duplicate_full_close_protections_are_visible():
    positions = [{"contract": "BTC_USDT", "size": "2", "mark_price": "50000", "initial_margin": "20"}]
    protections = [
        {"order_type": "close-long-position", "initial": {"contract": "BTC_USDT", "size": 0, "auto_size": "close_long"}, "trigger": {"rule": 1, "price": "49000"}},
        {"order_type": "close-long-position", "initial": {"contract": "BTC_USDT", "size": 0, "auto_size": "close_long"}, "trigger": {"rule": 1, "price": "55000"}},
        {"order_type": "close-long-position", "initial": {"contract": "BTC_USDT", "size": 0, "auto_size": "close_long"}, "trigger": {"rule": 2, "price": "48000"}},
    ]
    report = collect_anomalies(**healthy_inputs(
        positions=positions, protections=protections,
        decision=complete_decision(management=[{"contract": "BTC_USDT", "action": "HOLD"}]),
    ))
    assert {"protection_price_wrong_side", "duplicate_full_close_protection"} <= codes(report)


def test_anomaly_history_preserves_first_seen_and_marks_recovery(tmp_path):
    path = tmp_path / "history.json"
    first = collect_anomalies(**healthy_inputs(private_errors=["Gate private API timed out"]))
    saved = persist_anomaly_history(path, first)
    assert {"private_api_unavailable", "safety_snapshot_mismatch"} <= {row["code"] for row in saved["items"]}
    first_seen = next(row for row in saved["items"] if row["code"] == "private_api_unavailable")["first_seen_ms"]

    recovered = collect_anomalies(**healthy_inputs(now_ms=NOW + 60_000))
    saved = persist_anomaly_history(path, recovered)
    assert saved["active_total"] == 0
    assert saved["resolved_total"] >= 2
    api_record = next(row for row in saved["history"] if row["code"] == "private_api_unavailable")
    assert api_record["first_seen_ms"] == first_seen
    assert api_record["status"] == "resolved"
    disk_record = next(row for row in json.loads(path.read_text(encoding="utf-8")) if row["code"] == "private_api_unavailable")
    assert disk_record["resolved_at_ms"] == NOW + 60_000
