import json

import pytest

from gate_quant import instrument_universe as universe
from scripts import instrument_pool


def metadata(contract: str) -> dict:
    return {
        "name": contract,
        "quanto_multiplier": "0.01" if contract == "BTC_USDT" else "0.1",
        "order_price_round": "0.1",
        "order_size_min": "1",
        "leverage_max": "20",
        "enable_decimal": False,
        "in_delisting": False,
    }


class Market:
    def contracts(self, contract):
        return metadata(contract)


class Private:
    def __init__(self):
        self.position_rows = []
        self.order_rows = []
        self.trigger_rows = []
        self.protection_rows = []

    def positions(self): return self.position_rows
    def open_orders(self): return self.order_rows
    def trigger_entry_orders(self): return self.trigger_rows
    def protection_orders(self): return self.protection_rows


class Journal:
    def __init__(self): self.rows = []
    def active(self, environment, limit=1000): return self.rows


@pytest.fixture
def isolated_pool(monkeypatch, tmp_path):
    pool = tmp_path / "instrument_pool.json"
    preview = tmp_path / "preview.json"
    monkeypatch.setattr(instrument_pool, "POOL_FILE", pool)
    monkeypatch.setattr(universe, "PREVIEW_FILE", preview)
    monkeypatch.setattr(instrument_pool, "sync_instruments_state", lambda: None)
    instrument_pool.save_instruments([
        instrument_pool.from_gate_contract(metadata("BTC_USDT")),
        instrument_pool.from_gate_contract(metadata("ETH_USDT")),
    ])
    return pool


def test_preview_rejects_removing_contract_with_live_position(isolated_pool):
    private = Private()
    private.position_rows = [{"contract": "ETH_USDT", "size": "2", "mode": "single"}]
    with pytest.raises(ValueError, match="仍有持仓"):
        universe.create_preview(
            ["BTC"], actor="admin", environment="live",
            market_client=Market(), private_client=private, execution_journal=Journal(),
        )
    assert instrument_pool.configured_gate_contracts() == ["BTC_USDT", "ETH_USDT"]


def test_confirmed_change_updates_gate_pool_atomically(isolated_pool):
    private = Private()
    preview = universe.create_preview(
        ["BTC", "SOL_USDT"], actor="admin", environment="live",
        market_client=Market(), private_client=private, execution_journal=Journal(),
    )
    result = universe.apply_preview(
        preview["token"], preview["confirmation_phrase"],
        actor="admin", environment="live", market_client=Market(),
        private_client=private, execution_journal=Journal(),
    )
    assert result["contracts"] == ["BTC_USDT", "SOL_USDT"]
    assert result["added"] == ["SOL_USDT"]
    assert result["removed"] == ["ETH_USDT"]
    payload = json.loads(isolated_pool.read_text(encoding="utf-8"))
    assert [row["gate_contract"] for row in payload["instruments"]] == ["BTC_USDT", "SOL_USDT"]


def test_apply_rechecks_account_and_preserves_old_pool_on_new_order(isolated_pool):
    private = Private()
    preview = universe.create_preview(
        ["BTC"], actor="admin", environment="live",
        market_client=Market(), private_client=private, execution_journal=Journal(),
    )
    before = isolated_pool.read_bytes()
    private.order_rows = [{"contract": "ETH_USDT", "id": "123", "text": "t-gate-entry"}]
    with pytest.raises(ValueError, match="账户状态发生变化"):
        universe.apply_preview(
            preview["token"], preview["confirmation_phrase"],
            actor="admin", environment="live", market_client=Market(),
            private_client=private, execution_journal=Journal(),
        )
    assert isolated_pool.read_bytes() == before


@pytest.mark.parametrize("blocker", ["trigger", "protection", "execution"])
def test_preview_rejects_every_removed_contract_inflight_state(isolated_pool, blocker):
    private = Private()
    journal = Journal()
    plan = {"id": "501", "initial": {"contract": "ETH_USDT", "text": "t-gate-state"}}
    if blocker == "trigger":
        private.trigger_rows = [plan]
    elif blocker == "protection":
        private.protection_rows = [plan]
    else:
        journal.rows = [{"contract": "ETH_USDT", "client_id": "t-gate-state", "status": "submitted"}]

    with pytest.raises(ValueError, match="未完成执行意图|仍有持仓"):
        universe.create_preview(
            ["BTC"], actor="admin", environment="live",
            market_client=Market(), private_client=private, execution_journal=journal,
        )


def test_pool_limits_and_deduplication():
    assert universe.normalize_contracts(["btc", "BTC_USDT", "eth-usdt-swap"]) == ["BTC_USDT", "ETH_USDT"]
    with pytest.raises(ValueError, match="1 至 6"):
        universe.normalize_contracts([])
    with pytest.raises(ValueError, match="1 至 6"):
        universe.normalize_contracts(["AA", "BB", "CC", "DD", "EE", "FF", "GG"])


def test_gate_metadata_never_produces_zero_max_leverage():
    row = metadata("SOL_USDT")
    row["leverage_max"] = "0"
    assert instrument_pool.from_gate_contract(row)["max_leverage"] == 1
