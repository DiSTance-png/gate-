"""Read-only registry of Gate-managed agents and workers."""
from __future__ import annotations
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

AGENTS = (
    {"id": "gate_trading_brain", "name": "Gate 交易主脑", "role": "Gate 全市场决策与持仓裁决", "job": "trader", "output": "ai_brain_decisions.json", "prompt_transport": "python-direct"},
    {"id": "gate_factor_engine", "name": "Gate 因子引擎", "role": "Gate 多因子与微积分状态生成", "job": "factor_library", "output": "factor_library_snapshot.json", "prompt_transport": "none"},
    {"id": "gate_news_engine", "name": "Gate 新闻情绪引擎", "role": "Gate 宏观新闻与事件风险", "job": "news", "output": "news_sentiment.json", "prompt_transport": "none"},
)


def agent_statuses(job_runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest_by_job: dict[str, dict[str, Any]] = {}
    for run in job_runs:
        latest_by_job.setdefault(str(run.get("job_name")), run)
    result = []
    for agent in AGENTS:
        output = ROOT / "data" / agent["output"] if agent["output"] else None
        age = max(0, int(time.time() - output.stat().st_mtime)) if output and output.exists() else None
        run = latest_by_job.get(agent["job"], {})
        health = "healthy"
        if run.get("status") == "failed":
            health = "degraded"
        if agent["output"] and age is None:
            health = "cold"
        result.append({**agent, "health": health, "output_age_seconds": age, "last_run_status": run.get("status", "not-run"), "last_run_at": run.get("started_at", "")})
    return result
