"""Professional notification publisher bridging durable R20 Gateway events across channels."""
from __future__ import annotations
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from r20_gateway.publisher import publish


def _publish(event_type: str, title: str, message: str, payload: dict | None = None, priority: int = 50) -> bool:
    try:
        publish(event_type, title, message, payload=payload, priority=priority)
        return True
    except Exception:
        return False


def send_qq_message(text: str) -> bool:
    """Compatibility symbol: true means durably queued, not synchronously delivered."""
    return _publish("notification.generic", "📢 【系统状态通知】", text, priority=50)


def notify_trade_open(
    inst: str,
    side: str,
    sz: int,
    px: float,
    strategy: str,
    reason: str,
    tp_px: float | None = None,
    sl_px: float | None = None,
    leverage: int = 3,
) -> bool:
    """Triggered when an order is placed and accepted by OKX with OCO protection."""
    is_long = "多" in side or "BUY" in side.upper()
    direction_emoji = "🟢 多单 BUY" if is_long else "🔴 空单 SELL"
    tp_sl_line = f"• 目标止盈：{tp_px}\n• 云端止损：{sl_px}\n" if tp_px and sl_px else ""
    
    msg_lines = [
        f"⚡ 交易标的：{inst}-USDT-SWAP",
        f"🧭 决策方向：{direction_emoji}（{sz} 张 | {leverage}x 杠杆）",
        f"🎯 触发策略：{strategy}",
        f"💵 挂单点位：{px}",
    ]
    if tp_sl_line:
        msg_lines.append(tp_sl_line.strip())
    if reason:
        msg_lines.append(f"💡 决策逻辑：{reason}")
    
    message = "\n".join(msg_lines)
    return _publish(
        "trade.opened",
        f"🚀 【实盘开仓提醒】{inst} {direction_emoji}",
        message,
        {"instrument": inst, "side": side, "size": sz, "price": px, "strategy": strategy, "tp": tp_px, "sl": sl_px},
        priority=90,
    )


def notify_trade_close(
    inst: str,
    pnl: float,
    stage: str,
    exit_px: float,
    roi_pct: float | None = None,
    duration_str: str | None = None,
) -> bool:
    """Triggered upon position exit, profit lock, or stop loss."""
    is_win = pnl > 0
    is_be = abs(pnl) < 0.05 or "保本" in stage
    if is_be:
        status_tag = "⚖️ 【保本结清】"
        pnl_text = f"±0.00 USDT (保本移损退出，杜绝亏损)"
    elif is_win:
        status_tag = "🎉 【盈利落袋】"
        roi_str = f" (+{roi_pct:.2f}%)" if roi_pct is not None else ""
        pnl_text = f"+{pnl:.4f} USDT{roi_str}"
    else:
        status_tag = "🛡️ 【风控止损】"
        roi_str = f" ({roi_pct:.2f}%)" if roi_pct is not None else ""
        pnl_text = f"{pnl:.4f} USDT{roi_str}"

    msg_lines = [
        f"⚡ 交易标的：{inst}-USDT-SWAP",
        f"📌 平仓类型：{stage}",
        f"🏁 退出价格：{exit_px}",
        f"💰 结算收益：{pnl_text}",
    ]
    if duration_str:
        msg_lines.append(f"⏱️ 持仓时长：{duration_str}")

    message = "\n".join(msg_lines)
    return _publish(
        "trade.closed",
        f"{status_tag} {inst} 盈亏: {pnl:+.2f} U",
        message,
        {"instrument": inst, "pnl": pnl, "stage": stage, "exit_price": exit_px},
        priority=95,
    )


def notify_sl_updated(inst: str, side: str, old_sl: float, new_sl: float, reason: str = "浮盈达标，启动保本移损锁死胜率") -> bool:
    """Triggered when SL is moved to Breakeven or trailed higher."""
    message = (
        f"⚡ 交易标的：{inst}-USDT-SWAP\n"
        f"🧭 持仓方向：{side}\n"
        f"🛡️ 止损上移：{old_sl} ➔ {new_sl}（保本位）\n"
        f"🔒 策略意图：{reason}"
    )
    return _publish(
        "trade.sl_updated",
        f"🛡️ 【保本锁利移损】{inst}",
        message,
        {"instrument": inst, "side": side, "old_sl": old_sl, "new_sl": new_sl},
        priority=85,
    )


def notify_circuit_breaker(macro_event: str, reason: str) -> bool:
    """Triggered when market wide circuit breaker or risk defense triggers."""
    message = (
        f"⚠️ 预警等级：CRITICAL 宏观异动\n"
        f"🌐 触发事件：{macro_event}\n"
        f"🛑 熔断动作：全自动暂停新开仓，开启全闭环防守\n"
        f"📋 详细成因：{reason}"
    )
    return _publish(
        "risk.triggered",
        "🚨 【黑天鹅避险熔断预警】",
        message,
        {"event": macro_event, "reason": reason},
        priority=100,
    )


def notify_daily_summary(summary_text: str) -> bool:
    """Triggered for morning/evening executive reports."""
    return _publish("briefing.ready", "📊 【每日量化执行与资产简报】", summary_text, priority=40)


def notify_evolution_report(winrate: float, total_trades: int, summary: str, top_lesson: str) -> bool:
    """Triggered after self-improvement cycle finishes daily cognitive post-mortem."""
    message = (
        f"🧬 复盘样本：最近 {total_trades} 笔实盘平仓 | 样本胜率: {winrate}%\n"
        f"📈 演进方向：{summary}\n"
        f"💡 提炼心法：{top_lesson}"
    )
    return _publish(
        "evolution.completed",
        f"🧬 【AI 大脑自进化完成】胜率 {winrate}%",
        message,
        {"winrate": winrate, "total_trades": total_trades},
        priority=60,
    )
