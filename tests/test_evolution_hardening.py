import json

import pytest

from scripts import evolution_shield
from scripts import self_improvement_engine as evolution
from scripts.evolution.observability import classify_trade, summarize_trades


def _memory_paths(monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(evolution_shield, "DATA_DIR", data)
    monkeypatch.setattr(evolution_shield, "STRUCTURED_MEMORY_FILE", data / "structured_trading_memory.json")
    monkeypatch.setattr(evolution_shield, "AI_MEMORY_MD_FILE", data / "AI_TRADING_MEMORY.md")


def test_normalize_model_schema_drift_and_keep_collection_separate():
    result = evolution.normalize_evolution_response({
        "change_status": "NO_CHANGE",
        "diagnosis_insights": [{"analysis": "手续费占比偏高"}],
        "evolution_actions": [
            {"action": "COLLECT_EVIDENCE", "reason": "继续收集样本"},
            {"action": "ADD_MEMORY", "rule_text": "多个独立样本支持避免高位追价入场", "evidence_refs": ["gate_closed_1", "gate_closed_2"]},
        ],
        "ai_long_term_memory": [{"observation": "普通观察，不是规则"}],
    })
    assert result["diagnosis_insights"] == ["手续费占比偏高"]
    assert result["change_status"] == "ADD"
    assert result["normalized_proposal_count"] == 1
    assert result["memory_operations"][0]["action"] == "ADD"
    assert result["dropped_proposal_count"] == 1


def test_normalize_legacy_op_content_action_with_evidence():
    result = evolution.normalize_evolution_response({
        "evolution_actions": [{
            "op": "ADD", "content": "多个独立样本支持把手续费纳入期望评估",
            "evidence_refs": ["trade_1", "trade_2"], "reason": "成本数据可观测",
        }],
        "ai_long_term_memory": [],
    })
    assert result["normalized_proposal_count"] == 1
    assert result["memory_operations"][0]["rule_text"] == "多个独立样本支持把手续费纳入期望评估"


def test_baseline_initialization_is_explicit_and_idempotent(monkeypatch, tmp_path):
    _memory_paths(monkeypatch, tmp_path)
    snapshot = evolution_shield.initialize_baseline_memory()
    assert snapshot["exists"] is True
    assert len(snapshot["lessons"]) == len(evolution_shield.BASELINE_LESSONS)
    assert (tmp_path / "data" / "AI_TRADING_MEMORY.md").exists()
    with pytest.raises(evolution_shield.MemoryConflictError):
        evolution_shield.initialize_baseline_memory(expected_version="missing")


def test_operations_preserve_baseline_and_invalidate_only_non_baseline(monkeypatch, tmp_path):
    _memory_paths(monkeypatch, tmp_path)
    snapshot = evolution_shield.initialize_baseline_memory()
    assert evolution_shield.publish_operations([
        {"action": "ADD", "rule_text": "多个独立样本支持在低流动性时减少追价", "target_id": ""},
    ], expected_version=snapshot["version"], sample_size=3)
    current = evolution_shield.read_memory_snapshot()
    added = [item for item in current["lessons"] if not item.get("is_baseline")][0]
    assert evolution_shield.publish_operations([
        {"action": "INVALIDATE", "target_id": added["id"], "rule_text": ""},
    ], expected_version=current["version"], sample_size=3)
    final = evolution_shield.read_memory_snapshot()
    assert all(item["enabled"] for item in final["lessons"] if item.get("is_baseline"))
    assert any(item["id"] == added["id"] and not item["enabled"] for item in final["lessons"])


def test_memory_candidate_does_not_write_asset_multipliers(monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    candidates = tmp_path / "candidates"
    candidates.mkdir()
    monkeypatch.setattr(evolution, "DATA_DIR", str(data))
    monkeypatch.setattr(evolution, "EVOLUTION_CANDIDATE_DIR", str(candidates))
    candidate = {
        "id": "ev_hardening", "status": "pending", "expected_memory_version": "memory-v1",
        "sample_size": 3, "change_status": "ADD", "proposed_memory": ["足够长的测试心法用于通过人工审核发布流程"],
        "asset_multipliers": {"BTC_USDT": 1.4},
    }
    evolution.atomic_write_json(str(candidates / "ev_hardening.json"), candidate)
    monkeypatch.setattr(evolution_shield, "publish_review", lambda *args, **kwargs: True)
    monkeypatch.setattr(evolution_shield, "sync_markdown_mirror", lambda: True)
    result = evolution.apply_evolution_candidate("ev_hardening", "memory-v1")
    assert result["asset_multiplier_status"] == "not_applied_memory_only"
    assert not (data / "asset_multipliers.json").exists()


def test_observability_is_host_determined():
    observed, missing = classify_trade({field: 1 for field in ("regime", "structure_1h", "velocity", "acceleration", "jerk", "impulse", "energy_integral", "deviation_area_integral", "continuation_probability", "breakdown_probability", "var", "cvar", "atr", "risk_reward")}, {"close_time": "2026-01-01", "net_pnl": 1})
    partial, _ = classify_trade({"atr": 1, "entry_price": 100}, {"close_time": "2026-01-01", "net_pnl": 1})
    assert observed == "OBSERVED" and not missing
    assert partial == "PARTIAL"
    assert summarize_trades([{"snapshot_observability": observed}, {"snapshot_observability": partial}])["usable_for_causal"] == 1


def test_offline_evolution_cycle_generates_review_candidate_without_network(monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    candidates = data / "evolution_candidates"
    candidates.mkdir()
    monkeypatch.setattr(evolution, "DATA_DIR", str(data))
    monkeypatch.setattr(evolution, "REPORT_JSON_FILE", str(data / "self_improvement_report.json"))
    monkeypatch.setattr(evolution, "REPORT_ARCHIVE_DIR", str(data / "history"))
    monkeypatch.setattr(evolution, "EVOLUTION_CANDIDATE_DIR", str(candidates))
    monkeypatch.setattr(evolution, "EVOLUTION_LOCK_FILE", str(data / ".self_improvement.lock"))
    monkeypatch.setattr(evolution_shield, "DATA_DIR", data)
    monkeypatch.setattr(evolution_shield, "STRUCTURED_MEMORY_FILE", data / "structured_trading_memory.json")
    monkeypatch.setattr(evolution_shield, "AI_MEMORY_MD_FILE", data / "AI_TRADING_MEMORY.md")
    evolution_shield.initialize_baseline_memory()
    monkeypatch.setattr(evolution, "current_target_instruments", lambda: ["BTC_USDT"])
    monkeypatch.setattr(evolution, "load_closed_trades", lambda **kwargs: ([{
        "inst": "BTC_USDT", "side": "long", "time": "2026-09-27 10:00:00", "open_time": "2026-09-27 09:00:00",
        "strategy": "test", "margin": 10, "gross_pnl": 5, "fee": 1, "net_pnl": 4,
        "funding_fee": None, "slippage_bps": None, "exit_reason": "TP", "entry_snapshot": {"atr": 1},
        "snapshot_observability": "PARTIAL", "missing_evidence_fields": ["velocity"],
    }], {"accepted": 1}))
    monkeypatch.setattr(evolution, "call_llm_evolution_review", lambda *args, **kwargs: {
        "change_status": "NO_CHANGE", "diagnosis_insights": [{"analysis": "证据不足"}],
        "evolution_actions": [{"action": "ADD_MEMORY", "rule_text": "多个独立样本支持低流动性时减少追价", "reason": "样例"}],
        "ai_long_term_memory": [],
    })
    result = evolution.run_self_evolution(force=True)
    assert result["publication_status"] == "WAITING_REVIEW"
    assert result["normalized_proposal_count"] == 1
    assert list(candidates.glob("*.json"))
