import importlib
import os
import sys
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from gate_quant.client import GateFuturesClient
from gate_quant.config import GateSettings, load_settings
from gate_quant.web import _account_book_stats


class CaptureSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response_payload = self.response

        class Response:
            content = b"{}"

            def raise_for_status(self):
                return None

            def json(self):
                return response_payload

        return Response()


def configured_settings(**overrides):
    values = {
        "environment": "testnet",
        "api_key": "test-key",
        "api_secret": "test-secret",
        "max_position_notional_usd": 1000,
        "max_total_margin_usd": 100,
        "max_order_margin_usd": 25,
    }
    values.update(overrides)
    return GateSettings(**values)


def test_credentials_are_selected_only_for_active_environment(monkeypatch):
    import r20_gateway.secrets as secret_store

    monkeypatch.setattr(secret_store, "load_secrets", lambda: {})
    monkeypatch.setenv("GATE_TESTNET_API_KEY", "test-key")
    monkeypatch.setenv("GATE_TESTNET_API_SECRET", "test-secret")
    monkeypatch.setenv("GATE_LIVE_API_KEY", "live-key")
    monkeypatch.setenv("GATE_LIVE_API_SECRET", "live-secret")
    monkeypatch.setenv("GATE_ENVIRONMENT", "testnet")
    monkeypatch.setenv("GATE_LIVE_TRADING_ENABLED", "false")
    selected = load_settings()
    assert (selected.api_key, selected.api_secret) == ("test-key", "test-secret")

    monkeypatch.setenv("GATE_ENVIRONMENT", "live")
    monkeypatch.setenv("GATE_LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("GATE_PUBLIC_MARKET_ENV", "live")
    selected = load_settings()
    assert (selected.api_key, selected.api_secret) == ("live-key", "live-secret")


def test_encrypted_gate_credentials_override_plain_environment(monkeypatch):
    import r20_gateway.secrets as secret_store

    monkeypatch.setenv("GATE_ENVIRONMENT", "live")
    monkeypatch.setenv("GATE_PUBLIC_MARKET_ENV", "live")
    monkeypatch.setenv("GATE_LIVE_API_KEY", "plain-key")
    monkeypatch.setenv("GATE_LIVE_API_SECRET", "plain-secret")
    monkeypatch.setattr(secret_store, "load_secrets", lambda: {
        "GATE_LIVE_API_KEY": "encrypted-key",
        "GATE_LIVE_API_SECRET": "encrypted-secret",
    })

    selected = load_settings()

    assert (selected.api_key, selected.api_secret) == ("encrypted-key", "encrypted-secret")


def test_account_book_stats_reports_gate_pnl_win_loss_counts(monkeypatch):
    import gate_quant.web as web
    now = 1_800_000_000
    monkeypatch.setattr(web.dt, "datetime", web.dt.datetime)
    rows = [
        {"time": now, "type": "pnl", "change": "12.5"},
        {"time": now, "type": "pnl", "change": "-2.5"},
        {"time": now, "type": "fee", "change": "-0.1"},
        {"time": now, "type": "fund", "change": "-0.2"},
    ]
    stats = _account_book_stats(rows)
    assert stats["win_trades"] == 1
    assert stats["loss_trades"] == 1
    assert stats["closed_trades"] == 2
    assert stats["win_rate"] == 50.0


def test_native_gate_order_and_price_order_paths():
    session = CaptureSession({"id": "1"})
    client = GateFuturesClient(configured_settings(), session)
    client.create_order(contract="BTC_USDT", size=1, client_id="t-offline-order")
    client.create_protection_order(contract="BTC_USDT", size=-1, trigger_price="50000", rule=2, client_id="t-offline-stop")

    assert session.calls[0][1].endswith("/api/v4/futures/usdt/orders")
    assert session.calls[1][1].endswith("/api/v4/futures/usdt/price_orders")
    assert '"text":"t-offline-order"' in session.calls[0][2]["data"]
    assert '"trigger":{"price":"50000","rule":2' in session.calls[1][2]["data"]


def test_gate_limit_order_and_cross_leverage_use_native_fields():
    session = CaptureSession({"id": "1"})
    client = GateFuturesClient(configured_settings(), session)
    client.update_position_leverage(contract="SOL_USDT", leverage=4, cross_margin=True)
    client.create_order(contract="SOL_USDT", size=7, price="103.5", tif="gtc", client_id="t-limit")

    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url.endswith("/api/v4/futures/usdt/positions/SOL_USDT/leverage")
    assert kwargs["params"] == {"leverage": "0", "cross_leverage_limit": "4"}
    payload = session.calls[1][2]["data"]
    assert '"size":7' in payload
    assert '"price":"103.5"' in payload
    assert '"tif":"gtc"' in payload


def test_gate_decimal_entry_uses_native_number_but_protection_uses_full_close_int64_form():
    session = CaptureSession({"id": "1"})
    client = GateFuturesClient(configured_settings(), session)
    client.create_order(contract="SOL_USDT", size=0.4, price="103.5", tif="gtc", client_id="t-decimal")
    client.create_protection_order(contract="SOL_USDT", size=-0.4, trigger_price="100", rule=2, client_id="t-decimal-sl")
    assert '"size":0.4' in session.calls[0][2]["data"]
    protection_payload = session.calls[1][2]["data"]
    assert '"size":0' in protection_payload
    assert '"auto_size":"close_long"' in protection_payload
    assert '"order_type":"close-long-position"' in protection_payload
    assert '"size":-0.4' not in protection_payload


def test_position_not_found_is_a_gate_empty_position_condition():
    assert "POSITION_NOT_FOUND" in "Gate API POSITION_NOT_FOUND: Bad Request (HTTP 400)"


def test_gate_admin_routes_require_authentication(monkeypatch):
    monkeypatch.setenv("GATE_SSH_TUNNEL_ENABLED", "false")
    from gate_quant.web import app

    response = TestClient(app).get("/api/v1/admin/gate/runtime")
    assert response.status_code == 401


def test_live_read_only_config_is_allowed_without_trading_switch(monkeypatch):
    import gate_quant.web as web

    monkeypatch.setattr(web, "_require_control_admin", lambda token: {"username": "tester"})
    captured = {}
    monkeypatch.setattr(web, "_update_env", lambda values: captured.update(values))
    monkeypatch.setattr(web, "load_settings", lambda: configured_settings(environment="live", api_key="live-key", api_secret="live-secret", public_market_environment="live"))
    monkeypatch.setattr(web.store, "add", lambda *args, **kwargs: None)
    payload = web.GateAdminConfig(
        environment="live",
        live_trading_enabled=False,
        max_position_notional_usd=1000,
        max_total_margin_usd=100,
        max_order_margin_usd=25,
    )
    result = web.gate_admin_config(payload, None)
    assert result["environment"] == "live"
    assert captured["GATE_LIVE_TRADING_ENABLED"] == "false"
    assert captured["GATE_PUBLIC_MARKET_ENV"] == "live"


def test_manual_protection_write_requires_environment_execution_switch(monkeypatch):
    import gate_quant.web as web

    monkeypatch.setattr(web, "_require_control_admin", lambda token: {"username": "tester"})
    monkeypatch.setattr(web, "load_settings", lambda: configured_settings(environment="live", live_trading_enabled=False, public_market_environment="live"))
    payload = web.GateProtectionRequest(
        contract="BTC_USDT",
        size=-1,
        trigger_price="50000",
        rule=2,
        client_id="t-manual-stop",
    )
    with pytest.raises(web.HTTPException) as exc:
        web.create_protection(payload, None)
    assert exc.value.status_code == 403


def test_gate_admin_saves_credentials_only_to_encrypted_store(monkeypatch):
    import gate_quant.web as web
    config_values = {}
    encrypted_values = {}
    monkeypatch.setattr(web, "_require_control_admin", lambda token: {"username": "tester"})
    monkeypatch.setattr(web, "_update_env", lambda values: config_values.update(values))
    monkeypatch.setattr(web, "_save_gate_secrets", lambda values: encrypted_values.update(values))
    monkeypatch.setattr(web.store, "add", lambda *args, **kwargs: None)
    monkeypatch.setattr(web, "load_settings", lambda: configured_settings(public_market_environment="testnet"))
    payload = web.GateAdminConfig(
        environment="testnet",
        testnet_api_key="new-key",
        testnet_api_secret="new-secret",
        testnet_execute_trades=True,
        max_position_notional_usd=1000,
        max_total_margin_usd=100,
        max_order_margin_usd=25,
    )

    web.gate_admin_config(payload, None)

    assert "GATE_TESTNET_API_KEY" not in config_values
    assert "GATE_TESTNET_API_SECRET" not in config_values
    assert encrypted_values == {
        "GATE_TESTNET_API_KEY": "new-key",
        "GATE_TESTNET_API_SECRET": "new-secret",
    }


def test_gate_admin_validation_failure_does_not_persist_config(monkeypatch):
    import gate_quant.web as web

    persisted = {}
    monkeypatch.setattr(web, "_require_control_admin", lambda token: {"username": "tester"})
    monkeypatch.setattr(web, "_update_env", lambda values: persisted.update(values))
    monkeypatch.setattr(web, "_save_gate_secrets", lambda values: persisted.update(values))
    monkeypatch.setattr(web, "gate_tunnel", SimpleNamespace(enabled=False))
    monkeypatch.setattr(web, "load_settings", lambda: configured_settings(
        require_proxy=True,
        proxy_url="http://127.0.0.1:18080",
        public_market_environment="testnet",
    ))
    payload = web.GateAdminConfig(
        environment="testnet",
        proxy_url=None,
        max_position_notional_usd=1000,
        max_total_margin_usd=100,
        max_order_margin_usd=25,
    )

    with pytest.raises(web.HTTPException, match="GATE_PROXY_URL is required") as exc:
        web.gate_admin_config(payload, None)

    assert exc.value.status_code == 400
    assert persisted == {}


def test_enabling_live_requires_confirmation_and_read_only_probe(monkeypatch):
    import gate_quant.web as web

    persisted = {}
    probes = []
    monkeypatch.setattr(web, "_require_control_admin", lambda token: {"username": "tester"})
    monkeypatch.setattr(web, "_update_env", lambda values: persisted.update(values))
    monkeypatch.setattr(web, "_save_gate_secrets", lambda values: None)
    monkeypatch.setattr(web.store, "add", lambda *args, **kwargs: None)
    monkeypatch.setattr(web, "load_settings", lambda: configured_settings(environment="live", public_market_environment="live", live_trading_enabled=False))

    class Probe:
        def __init__(self, settings):
            probes.append(settings.environment)
        def account(self):
            return {"total": "1"}

    monkeypatch.setattr(web, "GateFuturesClient", Probe)
    payload = web.GateAdminConfig(environment="live", live_api_key="live-key", live_api_secret="live-secret", live_trading_enabled=True, max_position_notional_usd=1000, max_total_margin_usd=100, max_order_margin_usd=25)
    with pytest.raises(web.HTTPException, match="ENABLE GATE LIVE"):
        web.gate_admin_config(payload, None)
    assert persisted == {}

    payload.live_confirmation = "ENABLE GATE LIVE"
    web.gate_admin_config(payload, None)
    assert probes == ["live"]
    assert persisted["GATE_LIVE_TRADING_ENABLED"] == "true"
    assert persisted["GATE_TESTNET_EXECUTE_TRADES"] == "false"


def test_environment_switch_is_blocked_when_current_environment_has_risk(monkeypatch):
    import gate_quant.web as web

    monkeypatch.setattr(web, "_require_control_admin", lambda token: {"username": "tester"})
    monkeypatch.setattr(web, "load_settings", lambda: configured_settings(environment="testnet", public_market_environment="testnet"))
    monkeypatch.setattr(web, "_environment_risk_present", lambda environment: True)
    payload = web.GateAdminConfig(environment="live", live_trading_enabled=False, max_position_notional_usd=1000, max_total_margin_usd=100, max_order_margin_usd=25)
    with pytest.raises(web.HTTPException, match="停止 Testnet 自动交易并平仓") as exc:
        web.gate_admin_config(payload, None)
    assert exc.value.status_code == 409


def test_testnet_stop_and_flatten_orders_actions_and_confirms_zero(monkeypatch):
    import gate_quant.web as web

    config_updates = {}
    monkeypatch.setattr(web, "_require_control_admin", lambda token: {"username": "tester"})
    monkeypatch.setattr(web, "_update_env", lambda values: config_updates.update(values))
    monkeypatch.setattr(web, "load_settings", lambda: configured_settings(environment="testnet", public_market_environment="testnet", testnet_execute_trades=False))
    monkeypatch.setattr(web, "gate_write_lock", lambda: nullcontext())
    monkeypatch.setattr(web.store, "add", lambda *args, **kwargs: None)
    monkeypatch.setattr(web.time, "sleep", lambda seconds: None)

    class FakeGate:
        def __init__(self):
            self.entry_open = True
            self.trigger_entry_open = True
            self.position_open = True
            self.protections = [{"id": "p1", "initial": {"text": "t-gate-sl-1"}}]
            self.actions = []
        def open_orders(self, contract=None):
            return ([{"id": "o1", "contract": "BTC_USDT", "reduce_only": False, "close": False}] if self.entry_open else [])
        def cancel_order(self, order_id, contract=None):
            self.actions.append(("cancel", contract, order_id)); self.entry_open = False; return {"id": order_id}
        def trigger_entry_orders(self, contract=None, status="open"):
            return ([{"id": "plan-1", "initial": {"contract": "BTC_USDT", "text": "t-gate-entry-1"}}] if self.trigger_entry_open else [])
        def cancel_trigger_entry_order(self, order_id):
            self.actions.append(("cancel_trigger_entry", order_id)); self.trigger_entry_open = False; return {"id": order_id}
        def positions(self, contract=None):
            return ([{"contract": "BTC_USDT", "size": "2"}] if self.position_open else [])
        def close_position(self, contract, client_id):
            self.actions.append(("close", contract, client_id)); self.position_open = False; return {"id": "close-1"}
        def find_by_client_id(self, client_id, contract):
            return None
        def protection_orders(self, contract=None):
            return list(self.protections)
        def cancel_protection_order(self, order_id):
            self.actions.append(("cancel_protection", order_id)); self.protections = []; return {"id": order_id}

    fake = FakeGate()
    monkeypatch.setattr(web, "GateFuturesClient", lambda settings: fake)
    result = web.stop_and_flatten_testnet(web.GateTestnetShutdownRequest(confirmation="STOP TESTNET AND FLATTEN"), None)
    assert result["ok"] is True
    assert config_updates == {"GATE_TESTNET_EXECUTE_TRADES": "false"}
    assert [action[0] for action in fake.actions] == ["cancel", "cancel_trigger_entry", "close", "cancel_protection"]
    close_client_id = next(action[2] for action in fake.actions if action[0] == "close")
    assert len(close_client_id) <= 28


def test_testnet_stop_and_flatten_wrong_phrase_never_disables_or_writes(monkeypatch):
    import gate_quant.web as web

    touched = []
    monkeypatch.setattr(web, "_require_control_admin", lambda token: {"username": "tester"})
    monkeypatch.setattr(web, "_update_env", lambda values: touched.append(values))
    with pytest.raises(web.HTTPException, match="STOP TESTNET AND FLATTEN") as exc:
        web.stop_and_flatten_testnet(web.GateTestnetShutdownRequest(confirmation="wrong"), None)
    assert exc.value.status_code == 400
    assert touched == []


def test_testnet_stop_failure_keeps_trading_disabled_and_protections(monkeypatch):
    import gate_quant.web as web

    updates = {}
    monkeypatch.setattr(web, "_require_control_admin", lambda token: {"username": "tester"})
    monkeypatch.setattr(web, "_update_env", lambda values: updates.update(values))
    monkeypatch.setattr(web, "load_settings", lambda: configured_settings(environment="testnet", public_market_environment="testnet"))
    monkeypatch.setattr(web, "gate_write_lock", lambda: nullcontext())
    monkeypatch.setattr(web.store, "add", lambda *args, **kwargs: None)
    monkeypatch.setattr(web.time, "sleep", lambda seconds: None)

    class StuckGate:
        def open_orders(self, contract=None): return []
        def trigger_entry_orders(self, contract=None, status="open"): return []
        def positions(self, contract=None): return [{"contract": "ETH_USDT", "size": "1"}]
        def close_position(self, contract, client_id): return {"id": "close-unknown"}
        def find_by_client_id(self, client_id, contract): return None
        def protection_orders(self, contract=None): raise AssertionError("protection cleanup must wait for confirmed zero positions")

    monkeypatch.setattr(web, "GateFuturesClient", lambda settings: StuckGate())
    with pytest.raises(web.HTTPException, match="持仓仍未确认归零") as exc:
        web.stop_and_flatten_testnet(web.GateTestnetShutdownRequest(confirmation="STOP TESTNET AND FLATTEN"), None)
    assert exc.value.status_code == 502
    assert updates == {"GATE_TESTNET_EXECUTE_TRADES": "false"}


def test_gate_import_does_not_start_legacy_dashboard_worker():
    sys.modules.pop("dashboard.app", None)
    module = importlib.reload(importlib.import_module("gate_quant.web"))
    assert module.app.title == "Gate Quantum Trading System"
    assert "dashboard.app" not in sys.modules


def test_required_proxy_fails_closed():
    with pytest.raises(ValueError, match="GATE_PROXY_URL is required"):
        configured_settings(proxy_url=None, require_proxy=True).validate()


def test_api_v4_base_url_override_is_validated():
    custom = configured_settings(testnet_base_url="https://fx-api-testnet.gateio.ws/api/v4")
    custom.validate()
    with pytest.raises(ValueError, match="GATE_TESTNET_BASE_URL"):
        configured_settings(testnet_base_url="https://example.com").validate()


def test_account_book_uses_gate_native_endpoint():
    session = CaptureSession([])
    client = GateFuturesClient(configured_settings(), session)
    client.account_book(from_time=100, to_time=200, limit=1000)
    method, url, kwargs = session.calls[0]
    assert url.endswith("/api/v4/futures/usdt/account_book")
    assert kwargs["params"] == {"limit": 1000, "from": 100, "to": 200}


def test_position_close_uses_gate_native_endpoint():
    session = CaptureSession([])
    client = GateFuturesClient(configured_settings(), session)
    client.position_close(contract="BTC_USDT", limit=25)
    assert session.calls[0][1].endswith("/api/v4/futures/usdt/position_close")
    assert session.calls[0][2]["params"] == {"limit": 25, "contract": "BTC_USDT"}


def test_order_list_uses_official_offset_pagination():
    session = CaptureSession([])
    client = GateFuturesClient(configured_settings(), session)
    client.list_orders(status="finished", limit=25, page=3)
    assert session.calls[0][2]["params"] == {"status": "finished", "limit": 25, "offset": 50}


def test_gate_runtime_fields_are_persisted_by_settings_store(monkeypatch, tmp_path):
    from r20_backend import settings_store

    env_file = tmp_path / ".env"
    env_file.write_text("GATE_ENVIRONMENT=testnet\nGATE_LIVE_TRADING_ENABLED=false\n", encoding="utf-8")
    monkeypatch.setattr(settings_store, "ENV_FILE", env_file)
    monkeypatch.setattr(settings_store, "refresh_settings", lambda: None)
    values = {
        "GATE_ENVIRONMENT": "live",
        "GATE_PUBLIC_MARKET_ENV": "live",
        "GATE_TESTNET_EXECUTE_TRADES": "false",
        "GATE_LIVE_TRADING_ENABLED": "false",
        "GATE_PROXY_URL": "",
    }

    settings_store.update_env(values)

    persisted = env_file.read_text(encoding="utf-8")
    for key, value in values.items():
        assert f"{key}={value}" in persisted
        assert os.environ[key] == value


def test_gate_check_distinguishes_uninitialized_futures_account(monkeypatch):
    import gate_quant.web as web

    monkeypatch.setattr(web, "_require_control_admin", lambda token: {"username": "tester"})
    monkeypatch.setattr(web, "load_settings", lambda: configured_settings(environment="live", api_key="key", api_secret="secret", public_market_environment="live"))

    class UninitializedFuturesClient:
        def account(self):
            raise RuntimeError("Gate API USER_NOT_FOUND: please transfer funds first to create futures account (HTTP 400)")

    monkeypatch.setattr(web, "client", lambda: UninitializedFuturesClient())
    monkeypatch.setattr(web.store, "add", lambda *args, **kwargs: None)

    result = web.gate_admin_check(None)

    assert result["ok"] is False
    assert result["authenticated"] is True
    assert "尚未创建 USDT Futures 账户" in result["detail"]
