from __future__ import annotations

from decimal import Decimal

from gate_quant.config import GateSettings
from gate_quant.execution_journal import ExecutionJournal
from gate_quant.execution_reconciler import reconcile_intent


def settings() -> GateSettings:
    return GateSettings(
        environment="testnet",
        api_key="key",
        api_secret="secret",
        public_market_environment="testnet",
        testnet_execute_trades=True,
        max_position_notional_usd=10_000,
        max_total_margin_usd=1_000,
        max_order_margin_usd=100,
    )


class FakeGate:
    def __init__(self, *, size="10", left="6", position_size="4", baseline="0"):
        self.order_row = {"id": "101", "text": "t-gate-ai-1000", "status": "open", "size": size, "left": left}
        self.position = {"contract": "BTC_USDT", "size": position_size, "mark_price": "100"}
        self.protections: list[dict] = []
        self.created_rules: list[int] = []
        self.reductions: list[dict] = []
        self.cancelled = False
        self.fail_rule: int | None = None

    def order(self, order_id, contract=None):
        return self.order_row

    def find_by_client_id(self, client_id, contract):
        return self.order_row

    def positions(self, contract=None):
        return self.position

    def protection_orders(self, contract=None):
        return list(self.protections)

    def find_protection_by_client_id(self, client_id, contract):
        return next((row for row in self.protections if row["initial"]["text"] == client_id), None)

    def create_protection_order(self, *, contract, size, trigger_price, rule, client_id, **kwargs):
        self.created_rules.append(rule)
        if self.fail_rule == rule:
            raise RuntimeError(f"protection rule {rule} rejected")
        row = {
            "id": str(200 + len(self.protections)),
            "status": "open",
            "initial": {"contract": contract, "size": float(size), "text": client_id, "is_reduce_only": True},
            "trigger": {"price": trigger_price, "rule": rule},
        }
        self.protections.append(row)
        return row

    def cancel_order(self, order_id, contract=None):
        self.cancelled = True
        self.order_row.update({"status": "finished", "left": "0", "finish_as": "cancelled"})
        return {"id": order_id, "status": "finished"}

    def open_orders(self, contract=None):
        return [] if self.cancelled else ([self.order_row] if self.order_row["status"] == "open" else [])

    def create_order(self, **kwargs):
        self.reductions.append(kwargs)
        return {"id": "reduce-1", "status": "finished", "left": "0", **kwargs}


def prepared(journal: ExecutionJournal, *, baseline="0", requested="10") -> dict:
    return journal.prepare({
        "client_id": "t-gate-ai-1000",
        "environment": "testnet",
        "contract": "BTC_USDT",
        "requested_size": requested,
        "baseline_position_size": baseline,
        "entry_price": "99",
        "take_profit_price": "110",
        "stop_loss_price": "90",
        "created_at_ms": 1000,
    })


def test_partial_fill_is_protected_first_and_extended_after_more_fills(tmp_path):
    journal = ExecutionJournal(tmp_path / "execution.db")
    intent = prepared(journal)
    gate = FakeGate()

    first = reconcile_intent(gate, journal, intent, settings(), now_ms=2000)

    assert first["status"] == "partially_filled"
    assert first["filled_size"] == "4"
    assert gate.created_rules == [2, 1]
    assert [Decimal(str(row["initial"]["size"])) for row in gate.protections] == [Decimal("-4"), Decimal("-4")]

    gate.order_row.update({"status": "finished", "left": "0", "finish_as": "filled"})
    gate.position["size"] = "10"
    second = reconcile_intent(gate, journal, journal.get("t-gate-ai-1000"), settings(), now_ms=3000)

    assert second["status"] == "filled_protected"
    assert second["filled_size"] == "10"
    assert gate.created_rules == [2, 1, 2, 1]
    assert sum(abs(Decimal(str(row["initial"]["size"]))) for row in gate.protections if row["trigger"]["rule"] == 2) == Decimal("10")
    assert sum(abs(Decimal(str(row["initial"]["size"]))) for row in gate.protections if row["trigger"]["rule"] == 1) == Decimal("10")


def test_restart_recovers_prepared_intent_by_client_order_id_without_resubmit(tmp_path):
    journal = ExecutionJournal(tmp_path / "execution.db")
    prepared(journal)
    restarted_journal = ExecutionJournal(tmp_path / "execution.db")
    gate = FakeGate(size="4", left="0", position_size="4")

    result = reconcile_intent(gate, restarted_journal, restarted_journal.get("t-gate-ai-1000"), settings(), now_ms=2000)

    assert result["status"] == "filled_protected"
    assert result["order_id"] == "101"
    assert not gate.reductions


def test_missing_stop_cancels_remainder_and_reduces_only_new_add_on(tmp_path):
    journal = ExecutionJournal(tmp_path / "execution.db")
    intent = prepared(journal, baseline="10", requested="4")
    gate = FakeGate(size="4", left="0", position_size="14")
    gate.fail_rule = 2

    result = reconcile_intent(gate, journal, intent, settings(), now_ms=2000)

    assert result["status"] == "stop_failed_flatten_attempted"
    assert len(gate.reductions) == 1
    assert Decimal(str(gate.reductions[0]["size"])) == Decimal("-4")
    assert gate.reductions[0]["reduce_only"] is True
    assert gate.reductions[0]["close"] is False


def test_take_profit_failure_keeps_confirmed_stop_and_retries_later(tmp_path):
    journal = ExecutionJournal(tmp_path / "execution.db")
    intent = prepared(journal, requested="4")
    gate = FakeGate(size="4", left="0", position_size="4")
    gate.fail_rule = 1

    result = reconcile_intent(gate, journal, intent, settings(), now_ms=2000)

    assert result["status"] == "stop_protected_tp_pending"
    assert len(gate.protections) == 1
    assert gate.protections[0]["trigger"]["rule"] == 2
    assert not gate.reductions


def test_unconfirmed_prepared_entry_is_abandoned_not_retried(tmp_path):
    journal = ExecutionJournal(tmp_path / "execution.db")
    intent = prepared(journal)
    gate = FakeGate()
    gate.order_row = None

    result = reconcile_intent(gate, journal, intent, settings(), now_ms=62_000)

    assert result["status"] == "abandoned_unconfirmed"
    assert not gate.reductions


def test_filled_order_waits_for_eventually_consistent_position(tmp_path):
    journal = ExecutionJournal(tmp_path / "execution.db")
    intent = prepared(journal, requested="4")
    gate = FakeGate(size="4", left="0", position_size="0")

    waiting = reconcile_intent(gate, journal, intent, settings(), now_ms=20_000)

    assert waiting["status"] == "position_pending"
    assert journal.active("testnet")[0]["client_id"] == "t-gate-ai-1000"
    gate.position["size"] = "4"
    recovered = reconcile_intent(gate, journal, journal.get("t-gate-ai-1000"), settings(), now_ms=30_000)
    assert recovered["status"] == "filled_protected"
