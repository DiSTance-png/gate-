from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from gate_quant import ai_worker
from gate_quant.config import GateSettings


@pytest.mark.parametrize("remaining", [False, True])
def test_execution_refresh_rechecks_completed_intent(monkeypatch, remaining):
    rows = [{"client_id": "t-old", "contract": "ETH_USDT", "status": "pending_fill"}]
    calls = []
    journal = SimpleNamespace(active=lambda env: list(rows), get=lambda cid: dict(rows[0]))
    @contextmanager
    def lock():
        yield
    monkeypatch.setattr(ai_worker, "gate_write_lock", lock)
    def reconcile(*args):
        calls.append("reconcile")
        if not remaining:
            rows.clear()
    monkeypatch.setattr(ai_worker, "reconcile_intent", reconcile)
    monkeypatch.setattr(ai_worker, "daily_loss_state", lambda *a, **kw: {"tripped": False})
    client = SimpleNamespace(account=lambda: {"total": 100}, positions=lambda: [],
        open_orders=lambda: [], trigger_entry_orders=lambda: [], protection_orders=lambda: [])
    context, pending, safety = ai_worker._refresh_entry_safety(client, journal, GateSettings(),
        {"safe_for_new_risk": False, "cooldown": {"active": False},
         "reconciliation": {"issues": [{"code": "execution_reconciliation_pending"}]}})
    assert calls == ["reconcile"]
    assert safety["safe_for_new_risk"] is (not remaining)
    assert bool(pending) is remaining
    assert context["account"]["total"] == 100


@pytest.mark.parametrize("problem", ["manual_review", "protection_gap", "daily_loss", "timeout", "untracked_trigger"])
def test_refresh_never_bypasses_real_blockers(monkeypatch, problem):
    rows = [{"client_id": "t-old", "contract": "ETH_USDT", "status": "manual_review"}] if problem == "manual_review" else []
    journal = SimpleNamespace(active=lambda env: rows)
    @contextmanager
    def lock():
        yield
    monkeypatch.setattr(ai_worker, "gate_write_lock", lock)
    monkeypatch.setattr(ai_worker, "reconcile_intent", lambda *a: pytest.fail("manual review must not be retried"))
    monkeypatch.setattr(ai_worker, "daily_loss_state", lambda *a, **kw: {"tripped": problem == "daily_loss"})
    def account():
        if problem == "timeout":
            raise TimeoutError("offline timeout")
        return {"total": 100}
    client = SimpleNamespace(account=account,
        positions=lambda: [{"contract": "ETH_USDT", "size": 1}] if problem == "protection_gap" else [],
        open_orders=lambda: [], protection_orders=lambda: [],
        trigger_entry_orders=lambda: [{"contract": "ETH_USDT", "id": 123}] if problem == "untracked_trigger" else [])
    if problem == "timeout":
        with pytest.raises(TimeoutError):
            ai_worker._refresh_entry_safety(client, journal, GateSettings(), {})
    else:
        _, _, safety = ai_worker._refresh_entry_safety(client, journal, GateSettings(), {})
        assert not safety["safe_for_new_risk"]
