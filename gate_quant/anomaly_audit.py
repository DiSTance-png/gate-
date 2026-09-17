from __future__ import annotations

import hashlib
import json
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .safety import atomic_json, reconcile_exchange_state
from .service import protection_coverage_status


SEVERITY_ORDER = {"critical": 0, "error": 1, "warning": 2, "info": 3}
TERMINAL_EXECUTION_STATUSES = {"completed", "cancelled", "rolled_back", "failed"}


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _fingerprint(code: str, contract: str = "", client_id: str = "") -> str:
    raw = "|".join((code, contract.upper(), client_id))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def finding(
    code: str, *, severity: str, category: str, title: str, detail: str,
    contract: str = "", client_id: str = "", evidence: dict[str, Any] | None = None,
    suggestion: str = "只读核对后再处理，禁止盲目重试交易。",
) -> dict[str, Any]:
    return {
        "fingerprint": _fingerprint(code, contract, client_id),
        "code": code,
        "severity": severity,
        "category": category,
        "title": title,
        "detail": str(detail)[:2000],
        "contract": contract.upper(),
        "client_id": client_id,
        "evidence": evidence or {},
        "suggestion": suggestion,
    }


def _timestamp_ms(payload: dict[str, Any], *keys: str) -> int:
    for key in keys:
        try:
            value = int(float(payload.get(key) or 0))
        except (TypeError, ValueError):
            continue
        if value and value < 10_000_000_000:
            value *= 1000
        if value:
            return value
    return 0


def _job_findings(job_runs: list[dict[str, Any]], now_ms: int) -> list[dict[str, Any]]:
    result = []
    latest_success_id = max(
        (int(row.get("id") or 0) for row in job_runs if row.get("job_name") == "trader" and row.get("status") == "success"),
        default=0,
    )
    for row in job_runs:
        if row.get("job_name") != "trader":
            continue
        status = str(row.get("status") or "")
        if status == "success":
            continue
        if status != "running" and int(row.get("id") or 0) < latest_success_id:
            continue
        started_ms = 0
        try:
            from datetime import datetime
            started_ms = int(datetime.fromisoformat(str(row.get("started_at") or "")).timestamp() * 1000)
        except (TypeError, ValueError, OSError):
            pass
        age_seconds = max(0, int((now_ms - started_ms) / 1000)) if started_ms else None
        if status == "running" and age_seconds is not None and age_seconds <= 20 * 60:
            continue
        code = "trader_run_timeout" if status == "running" else "trader_run_failed"
        result.append(finding(
            code, severity="critical" if status == "running" else "error", category="decision",
            title="AI 决策任务超时" if status == "running" else "AI 决策任务异常退出",
            detail=str(row.get("detail") or "任务没有留下错误摘要"),
            client_id=f"job-{row.get('id')}", evidence={"run_id": row.get("id"), "return_code": row.get("return_code"), "age_seconds": age_seconds},
            suggestion="检查 Gateway 与 Trader 日志；不得直接重跑旧交易信号。",
        ))
    return result


def _heartbeat_findings(name: str, heartbeat: dict[str, Any], max_age_seconds: int, now_ms: int) -> list[dict[str, Any]]:
    timestamp_ms = _timestamp_ms(heartbeat, "timestamp_ms", "generated_at_ms")
    age = max(0, int((now_ms - timestamp_ms) / 1000)) if timestamp_ms else None
    if age is not None and age <= max_age_seconds:
        return []
    label = "AI Trader" if name == "trader" else "执行对账器"
    return [finding(
        f"{name}_heartbeat_stale", severity="critical", category="runtime", title=f"{label}心跳过期",
        detail=f"{label}最近心跳距今 {age if age is not None else '未知'} 秒。",
        evidence={"age_seconds": age, "last_status": heartbeat.get("status")},
        suggestion=f"检查 {label} 进程和日志，恢复前保持新增风险关闭。",
    )]


def _decision_findings(decision: dict[str, Any], positions: list[dict[str, Any]], now_ms: int) -> list[dict[str, Any]]:
    result = []
    required = {"BTC_USDT", "ETH_USDT", "SOL_USDT", "DOGE_USDT", "SUI_USDT", "XRP_USDT"}
    generated_ms = _timestamp_ms(decision, "generated_at_ms")
    age = max(0, int((now_ms - generated_ms) / 1000)) if generated_ms else None
    if age is None or age > 20 * 60:
        result.append(finding(
            "decision_snapshot_stale", severity="warning", category="data_quality", title="AI 决策快照过期",
            detail=f"最近完整决策距今 {age if age is not None else '未知'} 秒。", evidence={"age_seconds": age},
            suggestion="检查 Trader 调度、模型调用和决策文件更新时间。",
        ))
    available = {key for key, value in decision.items() if key in required and isinstance(value, dict) and isinstance(value.get("decision"), dict)}
    missing = sorted(required - available)
    if missing:
        result.append(finding(
            "decision_contracts_missing", severity="error", category="decision", title="六合约决策不完整",
            detail="缺少合约决策：" + "、".join(missing), evidence={"missing_contracts": missing},
            suggestion="检查模型 JSON 输出和解析器，不得用旧决策补齐。",
        ))
    management = decision.get("position_management") or []
    managed = {
        str(row.get("contract") or row.get("instId") or "").upper().replace("-USDT-SWAP", "_USDT").replace("-", "_")
        for row in management if isinstance(row, dict)
    }
    active_contracts = {str(row.get("contract") or "").upper() for row in positions if _decimal(row.get("size")) != 0}
    unmanaged = sorted(active_contracts - managed)
    if unmanaged:
        result.append(finding(
            "position_management_missing", severity="warning", category="position", title="持仓缺少 AI 管理结论",
            detail="本轮没有为以下持仓提供 HOLD、UPDATE_SL 或 CLOSE_MARKET：" + "、".join(unmanaged),
            evidence={"contracts": unmanaged, "decision_generated_at_ms": generated_ms},
            suggestion="检查提示词输出契约；执行层不得自行猜测平仓动作。",
        ))
    management_result = decision.get("management_result") or {}
    active_by_contract = {str(row.get("contract") or "").upper(): row for row in positions if _decimal(row.get("size")) != 0}
    for row in management_result.get("positions") or []:
        if isinstance(row, dict) and row.get("error"):
            contract = str(row.get("contract") or "").upper()
            result.append(finding(
                "position_management_failed", severity="critical", category="position", title="持仓管理执行失败",
                detail=str(row.get("error")), contract=contract, evidence={"action": row.get("action")},
                suggestion="按客户端订单号和实时持仓核对，禁止重复提交平仓或保护单。",
            ))
        elif isinstance(row, dict) and str(row.get("action") or "").upper() == "CLOSE_MARKET":
            contract = str(row.get("contract") or "").upper()
            if contract in active_by_contract:
                result.append(finding(
                    "position_close_unconfirmed", severity="critical", category="position", title="主动平仓尚未确认归零",
                    detail="AI 平仓请求已返回，但实时 Gate 仍存在该合约持仓。", contract=contract,
                    evidence={"position_size": active_by_contract[contract].get("size"), "result": row.get("result")},
                    suggestion="按平仓客户端订单号查单并确认仓位；禁止重复提交平仓。",
                ))
    return result


def _cycle_failure_findings(decision: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    safety = decision.get("safety_status") or {}
    for action in safety.get("lifecycle_actions") or []:
        if not isinstance(action, dict) or not action.get("error"):
            continue
        contract = str(action.get("contract") or "").upper()
        action_name = str(action.get("action") or "生命周期动作")
        result.append(finding(
            "lifecycle_action_failed", severity="critical", category="position", title="持仓生命周期动作失败",
            detail=f"{action_name}: {action.get('error')}", contract=contract, client_id=str(action.get("client_id") or action_name),
            evidence={"action": action_name, "category": action.get("category")},
            suggestion="核对实时持仓、订单和保护单；禁止仅因任务返回成功而忽略内部错误。",
        ))
    ambiguous = {
        "order_submission_ambiguous", "breakout_cancel_ambiguous", "breakout_triggered_reconciliation_pending",
        "protection_failed_flatten_attempted", "protection_failed_manual_review",
    }
    for trade in decision.get("trades") or []:
        if not isinstance(trade, dict) or str(trade.get("status") or "") not in ambiguous:
            continue
        status = str(trade.get("status") or "")
        contract = str(trade.get("contract") or "").upper()
        result.append(finding(
            f"trade_{status}", severity="critical", category="execution", title="交易执行状态尚未闭环",
            detail=str(trade.get("reason") or status), contract=contract, client_id=str(trade.get("client_id") or ""),
            evidence={"status": status}, suggestion="由执行对账器按已有客户端订单号继续核对，禁止重发。",
        ))
    return result


def _execution_findings(executions: list[dict[str, Any]], now_ms: int) -> list[dict[str, Any]]:
    result = []
    for row in executions:
        status = str(row.get("status") or "")
        if status in TERMINAL_EXECUTION_STATUSES:
            continue
        contract = str(row.get("contract") or "").upper()
        client_id = str(row.get("client_id") or "")
        age = max(0, int((now_ms - int(row.get("updated_at_ms") or 0)) / 1000)) if row.get("updated_at_ms") else None
        if row.get("last_error"):
            result.append(finding(
                "execution_last_error", severity="critical" if status == "manual_review" else "error", category="execution",
                title="订单执行闭环存在错误", detail=str(row.get("last_error")), contract=contract, client_id=client_id,
                evidence={"status": status, "age_seconds": age, "filled_size": row.get("filled_size"), "protected_size": row.get("protected_size")},
                suggestion="先按客户端订单号查 Gate 订单、持仓和保护单，禁止重新提交。",
            ))
        if status == "manual_review":
            result.append(finding(
                "execution_manual_review", severity="critical", category="execution", title="执行状态需要人工复核",
                detail="执行器无法自动证明订单、成交或保护状态。", contract=contract, client_id=client_id,
                evidence={"status": status, "age_seconds": age},
            ))
        filled, protected = abs(_decimal(row.get("filled_size"))), abs(_decimal(row.get("protected_size")))
        if filled > protected and status not in {"prepared", "submitted", "awaiting_trigger"}:
            result.append(finding(
                "execution_protection_incomplete", severity="critical", category="protection", title="已成交数量尚未完全保护",
                detail=f"成交 {filled}，已保护 {protected}。", contract=contract, client_id=client_id,
                evidence={"status": status, "filled_size": str(filled), "protected_size": str(protected)},
                suggestion="保持安全门关闭，由对账器按既有客户端订单号恢复保护或回滚新增仓位。",
            ))
        if age is not None and age > 20 * 60 and status != "awaiting_trigger":
            result.append(finding(
                "execution_state_stale", severity="error", category="execution", title="执行状态长时间未闭环",
                detail=f"状态 {status} 已 {age} 秒没有更新。", contract=contract, client_id=client_id,
                evidence={"status": status, "age_seconds": age},
            ))
    return result


def _exchange_findings(
    positions: list[dict[str, Any]], orders: list[dict[str, Any]], protections: list[dict[str, Any]],
    *, max_pending_age_seconds: int, now_ms: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    result = []
    reconciliation = reconcile_exchange_state(
        positions, orders, protections, max_pending_age_seconds=max_pending_age_seconds, now=now_ms / 1000,
    )
    for issue in reconciliation.get("issues") or []:
        code = str(issue.get("code") or "exchange_reconciliation_issue")
        contract = str(issue.get("contract") or "").upper()
        titles = {
            "protection_gap": "持仓保护覆盖不足", "orphan_protections": "发现孤立保护单",
            "stale_entry_orders": "入场挂单已经过期", "order_age_unknown": "无法确认挂单年龄",
        }
        result.append(finding(
            code, severity="critical" if code == "protection_gap" else "error", category="protection" if "protection" in code else "execution",
            title=titles.get(code, "交易所状态对账异常"), detail=json.dumps(issue, ensure_ascii=False), contract=contract,
            evidence=issue, suggestion="以 Gate 实时持仓、订单和保护单为准核对；检测器不会自动交易。",
        ))
    active_positions = [row for row in positions if _decimal(row.get("size")) != 0]
    for position in active_positions:
        contract = str(position.get("contract") or "").upper()
        size = _decimal(position.get("size"))
        rows = [row for row in protections if str(((row.get("initial") or {}).get("contract") or row.get("contract") or "")).upper() == contract]
        coverage = protection_coverage_status(rows, size)
        if not coverage["fully_protected"]:
            continue
        mark = _decimal(position.get("mark_price"))
        if mark <= 0:
            result.append(finding(
                "position_mark_price_missing", severity="warning", category="data_quality", title="持仓缺少有效标记价格",
                detail="无法验证保护单价格几何和持仓收益数据。", contract=contract,
            ))
        initial_margin = _decimal(position.get("initial_margin") or position.get("margin"))
        upl = _decimal(position.get("unrealised_pnl"))
        if initial_margin <= 0 and upl != 0:
            result.append(finding(
                "position_return_unverifiable", severity="warning", category="data_quality", title="持仓收益率无法验证",
                detail="存在未实现盈亏，但 Gate 持仓没有有效初始保证金字段。", contract=contract,
                evidence={"unrealised_pnl": str(upl), "initial_margin": str(initial_margin)},
                suggestion="AI 应继续使用真实价格与盈亏，但不得把收益率缺失当作 0%。",
            ))
        expected_side = "long" if size > 0 else "short"
        stop_rule, tp_rule = (2, 1) if size > 0 else (1, 2)
        full_close_by_rule: dict[int, list[dict[str, Any]]] = {stop_rule: [], tp_rule: []}
        for protection in rows:
            initial = protection.get("initial") or {}
            trigger = protection.get("trigger") or {}
            rule = int(trigger.get("rule") or 0)
            auto_size = str(initial.get("auto_size") or "").lower()
            order_type = str(protection.get("order_type") or "").lower()
            if rule in full_close_by_rule and (auto_size == f"close_{expected_side}" or order_type == f"close-{expected_side}-position"):
                full_close_by_rule[rule].append(protection)
            price = _decimal(trigger.get("price"))
            wrong_side = mark > 0 and price > 0 and (
                (size > 0 and ((rule == stop_rule and price >= mark) or (rule == tp_rule and price <= mark)))
                or (size < 0 and ((rule == stop_rule and price <= mark) or (rule == tp_rule and price >= mark)))
            )
            if wrong_side:
                result.append(finding(
                    "protection_price_wrong_side", severity="critical", category="protection", title="保护单价格位于错误一侧",
                    detail=f"标记价 {mark}，保护触发价 {price}，规则 {rule}。", contract=contract,
                    client_id=str(initial.get("text") or protection.get("id_string") or protection.get("id") or ""),
                    evidence={"mark_price": str(mark), "trigger_price": str(price), "rule": rule},
                ))
        for rule, full_close_rows in full_close_by_rule.items():
            if len(full_close_rows) > 1:
                result.append(finding(
                    "duplicate_full_close_protection", severity="warning", category="protection", title="存在重复的全平保护单",
                    detail=f"同一持仓规则 {rule} 存在 {len(full_close_rows)} 个全平保护单。", contract=contract, client_id=f"rule-{rule}",
                    evidence={"rule": rule, "count": len(full_close_rows)},
                    suggestion="核对是否由多次 UPDATE_SL 遗留；确认新保护有效后再撤旧单。",
                ))
    return result, reconciliation


def collect_anomalies(
    *, environment: str, job_runs: list[dict[str, Any]], decision: dict[str, Any], safety_snapshot: dict[str, Any],
    trader_heartbeat: dict[str, Any], reconciler_heartbeat: dict[str, Any], executions: list[dict[str, Any]],
    positions: list[dict[str, Any]], orders: list[dict[str, Any]], protections: list[dict[str, Any]],
    private_errors: list[str] | None = None, max_pending_age_seconds: int = 1800, now_ms: int | None = None,
) -> dict[str, Any]:
    now_ms = int(now_ms or time.time() * 1000)
    findings = []
    findings.extend(_job_findings(job_runs, now_ms))
    findings.extend(_heartbeat_findings("trader", trader_heartbeat, 20 * 60, now_ms))
    findings.extend(_heartbeat_findings("reconciler", reconciler_heartbeat, 120, now_ms))
    findings.extend(_decision_findings(decision, positions, now_ms))
    findings.extend(_cycle_failure_findings(decision))
    findings.extend(_execution_findings(executions, now_ms))
    exchange_findings, live_reconciliation = _exchange_findings(
        positions, orders, protections, max_pending_age_seconds=max_pending_age_seconds, now_ms=now_ms,
    )
    findings.extend(exchange_findings)
    for error in private_errors or []:
        findings.append(finding(
            "private_api_unavailable", severity="critical", category="runtime", title="Gate 私有状态读取失败",
            detail=error, suggestion="无法证明账户状态安全，保持 fail-closed 并检查网络、凭证或 Gate 服务。",
        ))
    safety_checked_ms = _timestamp_ms(safety_snapshot, "checked_at_ms")
    safety_age = max(0, int((now_ms - safety_checked_ms) / 1000)) if safety_checked_ms else None
    if safety_age is None or safety_age > 20 * 60:
        findings.append(finding(
            "safety_snapshot_stale", severity="warning", category="state_consistency", title="安全门快照过期",
            detail=f"安全门快照距今 {safety_age if safety_age is not None else '未知'} 秒。", evidence={"age_seconds": safety_age},
        ))
    snapshot_safe = bool(safety_snapshot.get("safe_for_new_risk"))
    risky_execution = any(str(row.get("status") or "") != "awaiting_trigger" for row in executions)
    daily_loss_tripped = bool((safety_snapshot.get("daily_loss") or {}).get("tripped"))
    live_safe = bool(live_reconciliation.get("safe_for_new_risk")) and not private_errors and not risky_execution and not daily_loss_tripped
    if safety_checked_ms and snapshot_safe != live_safe:
        findings.append(finding(
            "safety_snapshot_mismatch", severity="warning", category="state_consistency", title="安全门快照与实时账户不一致",
            detail=f"快照为 {'开启' if snapshot_safe else '关闭'}，实时只读对账为 {'开启' if live_safe else '关闭'}。",
            evidence={"snapshot_safe": snapshot_safe, "live_safe": live_safe, "snapshot_age_seconds": safety_age},
            suggestion="等待下一轮 Trader 刷新；在此之前以更保守的关闭状态为准。",
        ))
    for row in findings:
        row["environment"] = environment
        row["fingerprint"] = _fingerprint(f"{environment}:{row['code']}", row.get("contract", ""), row.get("client_id", ""))
    unique = {row["fingerprint"]: row for row in findings}
    items = sorted(unique.values(), key=lambda row: (SEVERITY_ORDER.get(row["severity"], 9), row["category"], row["contract"], row["code"]))
    return {
        "environment": environment,
        "checked_at_ms": now_ms,
        "items": items,
        "summary": {severity: sum(row["severity"] == severity for row in items) for severity in SEVERITY_ORDER},
        "live_reconciliation": live_reconciliation,
    }


def persist_anomaly_history(path: Path, report: dict[str, Any], *, max_records: int = 1000) -> dict[str, Any]:
    now_ms = int(report.get("checked_at_ms") or time.time() * 1000)
    environment = str(report.get("environment") or "").lower()
    try:
        saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        if not isinstance(saved, list):
            saved = []
    except (OSError, json.JSONDecodeError):
        saved = []
    previous = {str(row.get("fingerprint") or ""): row for row in saved if isinstance(row, dict) and row.get("fingerprint")}
    active_ids = set()
    for current in report.get("items") or []:
        fingerprint = str(current["fingerprint"])
        active_ids.add(fingerprint)
        old = previous.get(fingerprint) or {}
        previous[fingerprint] = {
            **old, **current, "status": "active",
            "first_seen_ms": int(old.get("first_seen_ms") or now_ms),
            "last_seen_ms": now_ms, "resolved_at_ms": None,
            "occurrences": int(old.get("occurrences") or 0) + 1,
        }
    for fingerprint, old in list(previous.items()):
        if fingerprint not in active_ids and old.get("status") == "active" and str(old.get("environment") or "").lower() == environment:
            previous[fingerprint] = {**old, "status": "resolved", "resolved_at_ms": now_ms}
    records = sorted(previous.values(), key=lambda row: int(row.get("last_seen_ms") or 0), reverse=True)[:max_records]
    atomic_json(path, records)
    return {
        **report,
        "items": [row for row in records if row.get("status") == "active" and str(row.get("environment") or "").lower() == environment],
        "history": records,
        "active_total": sum(row.get("status") == "active" and str(row.get("environment") or "").lower() == environment for row in records),
        "resolved_total": sum(row.get("status") == "resolved" and str(row.get("environment") or "").lower() == environment for row in records),
    }
