import json
from datetime import datetime, timedelta, timezone

from gate_quant import web
from r20_backend.policy_snapshot import GATE_POLICY_ENV_KEYS, capture_full_strategy_package, generate_policy_snapshot
from scripts import evolution_shield
from scripts import self_improvement_engine as evolution
from r20_backend.version import __version__
from r20_gateway import __version__ as gateway_version
from r20_gateway.store import GatewayStore


def _snapshot(tmp_path):
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "instrument_pool.json").write_text('[{"name":"BTC_USDT"}]', encoding="utf-8")
    return generate_policy_snapshot(
        root_dir=tmp_path,
        prompt_profile={"id": "stable", "name": "stable", "editor_mode": "simple", "simple_policy": {}},
        memory_snapshot={"version": "v1", "lessons": []},
        interceptor_plugins=[], council_config={"enabled": False, "roles": {}},
    )


def test_gate_product_uses_independent_unified_version():
    assert __version__ == "1.0.0"
    assert gateway_version == __version__
    assert web.app.version == __version__


def test_gateway_recovers_stale_runs_and_prunes_only_finished_rows(tmp_path):
    store = GatewayStore(tmp_path / "gateway.db")
    stale = (datetime.now(timezone(timedelta(hours=8))) - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
    with store.connect() as connection:
        connection.execute("INSERT INTO job_runs(job_name,status,started_at) VALUES ('stale-running','running',?)", (stale,))
        connection.execute("INSERT INTO job_runs(job_name,status,started_at,finished_at) VALUES ('old-done','success',?,?)", (stale, stale))
    assert store.recover_stale_job_runs() == 1
    result = store.prune_job_runs(keep_days=7, vacuum=False)
    assert result["deleted"] == 2
    assert store.job_runs(10) == []


def test_gate_strategy_values_change_policy_hash(monkeypatch, tmp_path):
    monkeypatch.setenv("GATE_LEVERAGE", "3")
    first = _snapshot(tmp_path)
    monkeypatch.setenv("GATE_LEVERAGE", "5")
    second = _snapshot(tmp_path)
    assert first["policy_hash"] != second["policy_hash"]
    assert second["units"]["gate_execution"]["values"]["GATE_LEVERAGE"] == "5"


def test_policy_package_excludes_credentials_proxy_and_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("GATE_TESTNET_API_KEY", "must-not-archive")
    monkeypatch.setenv("GATE_TESTNET_API_SECRET", "must-not-archive")
    monkeypatch.setenv("GATE_PROXY_URL", "socks5://private")
    monkeypatch.setenv("GATE_ENVIRONMENT", "live")
    package = capture_full_strategy_package(root_dir=tmp_path)["package"]["gate_policy"]
    assert set(package) == set(GATE_POLICY_ENV_KEYS)
    assert "must-not-archive" not in json.dumps(package)
    assert "private" not in json.dumps(package)
    assert "GATE_ENVIRONMENT" not in package


def test_policy_package_captures_instrument_pool(tmp_path):
    (tmp_path / "data").mkdir(exist_ok=True)
    expected = [{"name": "BTC_USDT"}, {"name": "ETH_USDT"}]
    (tmp_path / "data" / "instrument_pool.json").write_text(json.dumps(expected), encoding="utf-8")
    package = capture_full_strategy_package(root_dir=tmp_path)["package"]
    assert package["instrument_pool"] == expected


def test_evolution_candidate_requires_manual_apply(monkeypatch, tmp_path):
    monkeypatch.setattr(evolution, "EVOLUTION_CANDIDATE_DIR", str(tmp_path))
    monkeypatch.setattr(evolution, "DATA_DIR", str(tmp_path))
    candidate = {
        "id": "ev_test", "status": "pending", "expected_memory_version": "memory-v1",
        "sample_size": 3, "change_status": "ADD", "proposed_memory": ["足够长的测试心法用于通过人工审核发布流程"],
        "asset_multipliers": {"BTC_USDT": 1.1}, "created_at": "2026-09-14T00:00:00+08:00",
    }
    evolution.atomic_write_json(str(tmp_path / "ev_test.json"), candidate)
    calls = []
    monkeypatch.setattr(evolution_shield, "publish_review", lambda texts, **kwargs: calls.append((texts, kwargs)) or True)
    monkeypatch.setattr(evolution_shield, "sync_markdown_mirror", lambda: True)
    applied = evolution.apply_evolution_candidate("ev_test", "memory-v1")
    assert applied["status"] == "applied"
    assert len(calls) == 1
    assert json.loads((tmp_path / "ev_test.json").read_text(encoding="utf-8"))["status"] == "applied"


def test_auto_rollback_is_forbidden_in_live(monkeypatch):
    monkeypatch.setenv("GATE_AUTO_ROLLBACK_ENABLED", "true")
    monkeypatch.setenv("GATE_ENVIRONMENT", "live")
    monkeypatch.setenv("GATE_AUTO_ROLLBACK_POLICY_HASH", "deadbeef")
    result = evolution.evaluate_testnet_auto_rollback({"max_drawdown_usd": 999}, 100, 0.1)
    assert result == {"enabled": True, "environment": "live", "triggered": False, "reason": "live_forbidden"}


def test_performance_snapshot_marks_missing_exchange_fields_unavailable():
    snapshot = evolution.build_performance_snapshot([
        {"inst": "BTC_USDT", "net_pnl": 10, "fee": 1},
        {"inst": "BTC_USDT", "net_pnl": -4, "fee": 0.5},
    ])
    assert snapshot["net_pnl"] == 6
    assert snapshot["fees"] == 1.5
    assert snapshot["max_drawdown_usd"] == 4
    assert snapshot["funding_fee"] is None
    assert set(snapshot["unavailable_fields"]) == {"funding_fee", "slippage_bps"}


def test_evolution_loads_gate_contract_names_from_current_pool(monkeypatch, tmp_path):
    ledger = tmp_path / "trading_ledger.json"
    ledger.write_text(json.dumps([
        {"inst": "BTC_USDT", "status": "closed", "close_time": "2026-09-18 08:00:00", "pnl": 3.5},
        {"inst": "XRP_USDT", "status": "closed", "close_time": "2026-09-18 09:00:00", "pnl": -1.0},
        {"inst": "LINK_USDT", "status": "closed", "close_time": "2026-09-18 10:00:00", "pnl": 2.0},
    ]), encoding="utf-8")
    monkeypatch.setattr(evolution, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(evolution, "LEDGER_JSON_FILE", str(ledger))
    monkeypatch.setattr(evolution, "load_instruments", lambda: [
        {"name": "BTC", "instId": "BTC-USDT-SWAP"},
        {"name": "XRP", "instId": "XRP-USDT-SWAP"},
    ])

    trades, diagnostics = evolution.load_closed_trades(return_diagnostics=True)

    assert [row["inst"] for row in trades] == ["BTC_USDT", "XRP_USDT"]
    assert diagnostics["ledger_rows"] == 3
    assert diagnostics["accepted"] == 2
    assert diagnostics["filtered_outside_pool"] == 1
    assert diagnostics["snapshot_missing"] == 2


def test_evolution_matches_first_post_fill_snapshot_within_twenty_minutes(monkeypatch, tmp_path):
    ledger = tmp_path / "trading_ledger.json"
    ledger.write_text(json.dumps([{
        "inst": "BTC_USDT", "status": "closed", "open_time": "2026-09-18 08:00:00",
        "close_time": "2026-09-18 10:00:00", "side": "多", "pnl": 1,
    }]), encoding="utf-8")
    (tmp_path / "signal_journal.json").write_text(json.dumps([{
        "inst": "BTC-USDT-SWAP", "side": "long", "entryTime": "2026-09-18 08:19:59",
        "snapshot": {"price": 100, "velocity": 0.1},
    }]), encoding="utf-8")
    monkeypatch.setattr(evolution, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(evolution, "LEDGER_JSON_FILE", str(ledger))
    monkeypatch.setattr(evolution, "load_instruments", lambda: [{"name": "BTC", "instId": "BTC-USDT-SWAP"}])

    trades, diagnostics = evolution.load_closed_trades(return_diagnostics=True, start_time_override="2026-09-18")

    assert trades[0]["entry_snapshot"]["velocity"] == 0.1
    assert diagnostics["snapshot_matched"] == 1


def test_trader_heartbeat_reports_freshness(monkeypatch):
    monkeypatch.setattr(web.time, "time", lambda: 2_000.0)
    monkeypatch.setattr(web, "_read_json_file", lambda name, default: {"timestamp_ms": 1_900_000, "pid": 42})
    status = web._heartbeat_status()
    assert status["age_seconds"] == 100
    assert status["fresh"] is True


def test_dashboard_protection_orders_classify_linked_and_orphan():
    protections = [
        {"id_string": "1", "status": "open", "create_time": 100, "trigger": {"rule": 1, "price": "3000"}, "initial": {"contract": "ETH_USDT", "size": -4, "is_reduce_only": True, "text": "t-gate-tp-1"}},
        {"id_string": "2", "status": "open", "create_time": 101, "trigger": {"rule": 2, "price": "2000"}, "initial": {"contract": "ETH_USDT", "size": -4, "is_reduce_only": True, "text": "t-gate-sl-1"}},
        {"id_string": "90071992547409930", "status": "open", "create_time": 102, "trigger": {"rule": 2, "price": "70000"}, "initial": {"contract": "BTC_USDT", "size": -10, "is_reduce_only": True, "text": "t-gate-sl-2"}},
    ]
    rows = web._dashboard_protection_orders(protections, [{"contract": "ETH_USDT", "size": 4}])
    assert [(row["kind"], row["relation"]) for row in rows] == [
        ("take_profit", "linked"), ("stop_loss", "linked"), ("stop_loss", "orphan")
    ]
    assert rows[2]["order_id"] == "90071992547409930"
