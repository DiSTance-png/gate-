from __future__ import annotations

import os
import json
import socket
import time
import datetime as dt
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=True)
STARTED_AT = time.time()
BACKEND_LOG = ROOT / "logs" / "gate_backend.log"

def _backend_log(message: str) -> None:
    BACKEND_LOG.parent.mkdir(parents=True, exist_ok=True)
    with BACKEND_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")

from .client import GateFuturesClient
from .config import load_settings
from .risk import RiskLimits
from .service import GateTradingService, protection_coverage_status
from .store import EventStore
from .proxy_tunnel import gate_tunnel
from .risk_profiles import get_risk_profile, profile_catalog
from .safety import order_age_seconds
from r20_gateway.supervisor import start_supervisor as start_gateway_supervisor, stop_supervisor as stop_gateway_supervisor
from r20_gateway.publisher import DB_PATH as GATEWAY_DB_PATH
from r20_gateway.store import GatewayStore
from r20_gateway.scheduler import scheduler_snapshot
from r20_gateway.supervisor import current_pid, _worker_lock_held
from r20_gateway.agents import agent_statuses
from r20_gateway.secrets import status as secret_store_status

DATA_DIR = ROOT / "data"
LOG_SOURCES = {"trader": "gate_trader.log", "backend": "gate_backend.log", "scheduler": "r20_gateway.log"}

def _read_json_file(name: str, default):
    try:
        import json
        return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _safety_status() -> dict[str, Any]:
    return _read_json_file("gate_safety_status.json", {"safe_for_new_risk": False, "reason": "no safety check recorded"})


def _heartbeat_status() -> dict[str, Any]:
    heartbeat = _read_json_file("gate_trader_heartbeat.json", {})
    timestamp_ms = float(heartbeat.get("timestamp_ms") or 0)
    age_seconds = max(0, int(time.time() - timestamp_ms / 1000)) if timestamp_ms else None
    return {**heartbeat, "age_seconds": age_seconds, "fresh": bool(age_seconds is not None and age_seconds <= 20 * 60)}

def _tail_log(name: str, lines: int = 100) -> str:
    path = ROOT / "logs" / name
    if not path.exists():
        return "暂无日志"
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, min(lines, 200)):])

def _file_health(name: str, expected_interval: int) -> dict[str, Any]:
    path = DATA_DIR / name
    if not path.exists():
        return {"name": name, "exists": False, "age_seconds": None, "fresh": False, "bytes": 0}
    age = max(0, int(time.time() - path.stat().st_mtime))
    return {"name": name, "exists": True, "age_seconds": age, "fresh": age <= expected_interval * 2, "bytes": path.stat().st_size}

def _trader_log() -> str:
    history = _read_json_file("ai_decision_history.json", [])
    if not isinstance(history, list) or not history:
        return _tail_log("gate_trader.log")
    action_text = {"BUY_LONG": "做多", "SELL_SHORT": "做空", "WAIT": "观望"}
    blocks = []
    for cycle in reversed(history[-12:]):
        if not isinstance(cycle, dict):
            continue
        stamp = cycle.get("generated_at_ms", 0)
        try:
            from datetime import datetime
            timestamp = datetime.fromtimestamp(float(stamp) / 1000).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            timestamp = "时间未记录"
        trade = cycle.get("trade") or {}
        trade_label = {"not_submitted": "未下单", "submitted_testnet": "已提交测试网订单", "submitted_live": "已提交实盘订单", "blocked_by_strategy_interceptor": "被策略拦截", "protection_failed_flatten_attempted": "保护单失败，已尝试平仓"}.get(str(trade.get("status")), str(trade.get("status") or "未下单"))
        rows = [f"【交易巡检】{timestamp}", f"巡检状态：正常完成", f"执行结果：{trade_label}"]
        for symbol, decision in (cycle.get("decisions") or {}).items():
            if not isinstance(decision, dict):
                continue
            action = action_text.get(str(decision.get("action")), str(decision.get("action") or "未知"))
            confidence = float(decision.get("confidence") or 0)
            reason = decision.get("rejection_reason") or decision.get("summary_reason") or "未提供原因"
            rows.append(f"{str(symbol).replace('_USDT', '/USDT')}：{action}，信心度 {confidence:.0f}%；说明：{reason}")
        blocks.append("\n".join(rows))
    return "\n\n────────────────────────\n\n".join(blocks) or _tail_log("gate_trader.log")


@asynccontextmanager
async def lifespan(_: FastAPI):
    _backend_log("Gate control-plane starting")
    gate_tunnel.start()
    gateway_enabled = os.getenv("R20_GATEWAY_WORKER_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
    if gateway_enabled:
        start_gateway_supervisor()
    try:
        yield
    finally:
        if gateway_enabled:
            stop_gateway_supervisor()
        gate_tunnel.stop()
        _backend_log("Gate control-plane stopped")


app = FastAPI(title="Gate Quantum Trading System", version="0.2.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")
if (ROOT / "frontend" / "dist" / "assets").exists():
    app.mount("/assets", StaticFiles(directory=ROOT / "frontend" / "dist" / "assets"), name="frontend-assets")

@app.middleware("http")
async def no_cache_frontend_assets(request: Request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith(("/assets/", "/static/")):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response
store = EventStore(ROOT / "runtime" / "gate_quant.db")


class GateAdminConfig(BaseModel):
    environment: str = Field(pattern="^(testnet|live)$")
    testnet_api_key: str | None = None
    testnet_api_secret: str | None = None
    live_api_key: str | None = None
    live_api_secret: str | None = None
    live_trading_enabled: bool = False
    testnet_execute_trades: bool = False
    leverage: float = Field(default=3, ge=1, le=100)
    risk_profile: str = Field(default="standard", pattern="^(observe|conservative|standard|active|aggressive)$")
    proxy_url: str | None = None
    max_position_notional_usd: float = Field(gt=0)
    max_total_margin_usd: float = Field(gt=0)
    max_order_margin_usd: float = Field(gt=0)


class GateOrderRequest(BaseModel):
    contract: str = Field(pattern=r"^[A-Z0-9]+_[A-Z0-9]+$")
    size: float
    price: str = "0"
    tif: str = Field(default="ioc", pattern=r"^(gtc|ioc|poc)$")
    client_id: str = Field(min_length=1, max_length=28, pattern=r"^t-[A-Za-z0-9_.-]+$")
    leverage: float | None = Field(default=None, gt=0, le=100)
    reduce_only: bool = False
    close: bool = False


class GateProtectionRequest(BaseModel):
    contract: str = Field(pattern=r"^[A-Z0-9]+_[A-Z0-9]+$")
    size: float
    trigger_price: str
    rule: Literal[1, 2]
    client_id: str = Field(min_length=1, max_length=28, pattern=r"^t-[A-Za-z0-9_.-]+$")
    reduce_only: bool = True
    close: bool = False
    expiration: int = Field(default=86400, ge=60, le=2592000)


def _require_control_admin(token: str | None):
    from r20_backend.app import admin_auth
    user = admin_auth.validate_session(token or "")
    if not user:
        raise HTTPException(401, "管理员会话无效或已过期")
    return user


def _update_env(values: dict[str, str]) -> None:
    path = ROOT / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(values)
    output = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in pending:
            output.append(f"{key}={pending.pop(key)}")
        else:
            output.append(line)
    output.extend(f"{key}={value}" for key, value in pending.items())
    temp = path.with_suffix(".tmp")
    temp.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    temp.replace(path)
    os.environ.update(values)


def client() -> GateFuturesClient:
    return GateFuturesClient(load_settings())


def trading_service(c: GateFuturesClient | None = None) -> GateTradingService:
    s = load_settings()
    return GateTradingService(c or GateFuturesClient(s), RiskLimits(s.max_position_notional_usd, s.max_total_margin_usd, s.max_order_margin_usd))


def _order_risk(c: GateFuturesClient, *, contract: str, size: float, leverage: float) -> dict[str, Any]:
    s = load_settings()
    account = c.account()
    contract_meta = c.contracts(contract)
    ticker_rows = market_client().tickers(contract)
    ticker = ticker_rows[0] if isinstance(ticker_rows, list) and ticker_rows else ticker_rows
    mark_price = float((ticker or {}).get("mark_price") or (ticker or {}).get("last") or 0)
    multiplier = float((contract_meta or {}).get("quanto_multiplier") or 0)
    if mark_price <= 0 or multiplier <= 0:
        raise RuntimeError("Gate contract mark price or quanto multiplier is unavailable")
    order_notional = abs(size) * multiplier * mark_price
    current_notional = sum(abs(float(p.get("size") or 0)) * float(p.get("quanto_multiplier") or multiplier) * float(p.get("mark_price") or 0) for p in c.positions())
    current_margin = float(account.get("position_margin") or account.get("cross_position_margin") or 0) + float(account.get("order_margin") or account.get("cross_order_margin") or 0)
    return {"order_margin_usd": order_notional / leverage, "current_margin_usd": current_margin, "current_position_notional_usd": current_notional, "order_notional_usd": order_notional, "environment": s.environment, "live_enabled": s.live_trading_enabled}


def market_client() -> GateFuturesClient:
    s = load_settings()
    if s.public_market_environment == s.environment:
        return GateFuturesClient(s)
    from dataclasses import replace
    # Public market data is read-only; using the Live market hostname must not unlock private trading.
    return GateFuturesClient(replace(s, environment=s.public_market_environment, api_key="public-readonly", api_secret="public-readonly", live_trading_enabled=True))


def safe_private(call, fallback):
    try:
        return call(), None
    except PermissionError:
        return fallback, "Gate credentials are not configured"
    except Exception as exc:
        store.add("gate.private.error", {"error": str(exc)[:300]}, "ERROR")
        return fallback, str(exc)


def _account_book_stats(rows: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Aggregate Gate's exchange-side account ledger for the current BJ day."""
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    day_start = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    fees = funding = realized = 0.0
    for row in rows or []:
        try:
            if int(float(row.get("time") or 0)) < day_start:
                continue
            amount = float(row.get("change") or 0)
        except (TypeError, ValueError):
            continue
        kind = str(row.get("type") or "").lower()
        if kind == "fee":
            fees += amount
        elif kind == "fund":
            funding += amount
        elif kind == "pnl":
            realized += amount
    return {"realized_gross": round(realized, 8), "fees_paid": round(fees, 8), "funding_paid": round(funding, 8), "net_realized": round(realized + fees + funding, 8), "total_pnl": round(realized + fees + funding, 8), "win_trades": 0, "loss_trades": 0, "win_rate": None, "source": "Gate Futures account_book", "timezone": "Asia/Shanghai"}


@app.get("/")
def index():
    dist_index = ROOT / "frontend" / "dist" / "index.html"
    response = FileResponse(dist_index if dist_index.exists() else ROOT / "web" / "index.html")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.get("/api/v1/health")
def health() -> dict[str, Any]:
    s = load_settings()
    tunnel = gate_tunnel.status()
    try:
        with socket.create_connection((tunnel.local_host, tunnel.local_port), timeout=0.4):
            shared_tunnel_ready = True
    except OSError:
        shared_tunnel_ready = False
    tunnel_ready = tunnel.running or shared_tunnel_ready
    return {"service": "gate-quant", "version": app.version, "status": "ok" if not tunnel.enabled or tunnel_ready else "degraded", "timestamp": int(time.time()), "environment": s.environment, "base_url": s.base_url, "public_market_environment": s.public_market_environment, "credentials_configured": bool(s.api_key and s.api_secret), "testnet_execute_trades": bool(s.environment == "testnet" and s.testnet_execute_trades), "live_trading_enabled": bool(s.environment == "live" and s.live_trading_enabled), "proxy_configured": bool(s.proxy_url), "proxy_required": s.require_proxy, "tunnel_running": tunnel_ready, "risk": {"leverage": s.leverage, "max_position_notional_usd": s.max_position_notional_usd, "max_total_margin_usd": s.max_total_margin_usd, "max_order_margin_usd": s.max_order_margin_usd}}


@app.get("/api/v1/market/tickers")
def tickers(contracts: str = "BTC_USDT,ETH_USDT,SOL_USDT,DOGE_USDT"):
    wanted = {v.strip().upper() for v in contracts.split(",") if v.strip()}
    try:
        rows = market_client().tickers()
        return {"items": [row for row in rows if row.get("contract") in wanted], "source": "Gate Futures REST"}
    except Exception as exc:
        store.add("gate.market.error", {"error": str(exc)[:300]}, "ERROR")
        raise HTTPException(502, f"Gate market request failed: {exc}") from exc


@app.get("/api/v1/dashboard")
def dashboard():
    c = client()
    account, account_error = safe_private(c.account, {})
    positions, position_error = safe_private(c.positions, [])
    orders, order_error = safe_private(c.open_orders, [])
    protections, protection_error = safe_private(c.protection_orders, [])
    errors = [e for e in (account_error, position_error, order_error, protection_error) if e]
    return {"account": account, "positions": positions, "orders": orders, "protections": protections, "private_available": not errors, "errors": sorted(set(errors)), "events": store.recent()}


@app.get("/api/all")
def all_dashboard():
    """Unified Gate-native payload for the migrated control console."""
    s = load_settings()
    c = client()
    try:
        ticker_rows = market_client().tickers()
    except Exception as exc:
        ticker_rows = []
        store.add("gate.market.error", {"error": str(exc)[:300]}, "ERROR")
    account, account_error = safe_private(c.account, {})
    positions_raw, position_error = safe_private(c.positions, [])
    orders_raw, order_error = safe_private(c.open_orders, [])
    protections_raw, protection_error = safe_private(c.protection_orders, [])
    account_book, account_book_error = safe_private(lambda: c.account_book(from_time=int(time.time()) - 172800, to_time=int(time.time()), limit=1000), [])
    positions = []
    for p in positions_raw or []:
        size = float(p.get("size") or 0)
        if not size:
            continue
        contract = str(p.get("contract") or "")
        # Gate returns leverage="0" for cross margin; the effective value is
        # exposed as `lever` (and cross_leverage_limit). Likewise, initial_margin
        # is the native occupied margin for an open position.
        effective_leverage = p.get("lever") or p.get("cross_leverage_limit") or p.get("leverage") or "--"
        occupied_margin = p.get("initial_margin") or p.get("margin") or "--"
        side = "long" if size > 0 else "short"
        contract_protections = [row for row in (protections_raw or []) if str(row.get("initial", {}).get("contract") or row.get("contract") or "") == contract]
        stop_rule = 2 if side == "long" else 1
        tp_rule = 1 if side == "long" else 2
        stop_rows = [row for row in contract_protections if int((row.get("trigger") or {}).get("rule") or 0) == stop_rule]
        tp_rows = [row for row in contract_protections if int((row.get("trigger") or {}).get("rule") or 0) == tp_rule]
        stop_price = ((stop_rows[0].get("trigger") or {}).get("price") if stop_rows else None)
        tp_price = ((tp_rows[0].get("trigger") or {}).get("price") if tp_rows else None)
        coverage = protection_coverage_status(contract_protections, size)
        fully_protected = bool(coverage["fully_protected"])
        effective_covered_size = min(coverage["take_profit"], coverage["stop_loss"])
        positions.append({"instId": contract, "name": contract, "side": side, "pos": str(abs(size)), "lever": str(effective_leverage), "margin": str(occupied_margin), "margin_usdt": str(occupied_margin), "avgPx": str(p.get("entry_price") or "--"), "last": str(p.get("mark_price") or "--"), "markPx": str(p.get("mark_price") or "--"), "upl": str(p.get("unrealised_pnl") or "0"), "uplRatio": "--", "displayStop": float(stop_price) if stop_price not in (None, "") else None, "takeProfitPx": float(tp_price) if tp_price not in (None, "") else None, "protectionStatus": "fully_protected" if fully_protected else ("partially_protected" if contract_protections else "unprotected"), "protectionCoveragePct": min(100.0, float(effective_covered_size) / abs(size) * 100) if size else 0.0, "cloud_oco_verified": fully_protected})
    pending = []
    for o in orders_raw or []:
        age = order_age_seconds(o)
        pending.append({"ordId": str(o.get("id")), "instId": o.get("contract"), "name": o.get("contract"), "side": "buy" if float(o.get("size") or 0) > 0 else "sell", "posSide": "long" if float(o.get("size") or 0) > 0 else "short", "px": str(o.get("price") or "0"), "sz": str(abs(float(o.get("size") or 0))), "state": o.get("status", "open"), "cTime": str(o.get("create_time_ms") or o.get("create_time") or ""), "text": o.get("text", ""), "age_seconds": int(age) if age is not None else None, "expires_in_seconds": max(0, s.max_pending_order_age_seconds - int(age)) if age is not None else None})
    dashboard_contracts = {"BTC_USDT", "ETH_USDT", "SOL_USDT", "DOGE_USDT", "SUI_USDT", "XRP_USDT"}
    account["initial_capital"] = s.initial_capital_usd or None
    factor_snapshot = _read_json_file("factor_library_snapshot.json", {})
    factor_instruments = factor_snapshot.get("instruments", []) if isinstance(factor_snapshot, dict) else []
    factor_map = {str(row.get("name") or row.get("instId", "")).replace("-USDT-SWAP", "_USDT"): row for row in factor_instruments if isinstance(row, dict)}
    latest_decisions = _read_json_file("ai_brain_decisions.json", {})
    decision_history = _read_json_file("ai_decision_history.json", [])
    if not isinstance(decision_history, list):
        decision_history = []
    profile = get_risk_profile(s.risk_profile)
    risk_snapshot = profile.snapshot(leverage=s.leverage, environment=s.environment)
    factors = []
    for t in ticker_rows or []:
        contract_name = str(t.get("contract") or "")
        if contract_name not in dashboard_contracts:
            continue
        snapshot = factor_map.get(contract_name, {})
        envelope = latest_decisions.get(contract_name, {}) if isinstance(latest_decisions, dict) else {}
        decision = envelope.get("decision", {}) if isinstance(envelope, dict) else {}
        calculus = snapshot.get("calculus", {}) if isinstance(snapshot, dict) else {}
        factors.append({"instId": contract_name, "name": contract_name, "type": "Gate Futures", "price": float(t.get("last") or 0), "chg24h": float(t.get("change_percentage") or 0), "high24h": float(t.get("high_24h") or 0), "low24h": float(t.get("low_24h") or 0), "vol24h": float(t.get("volume_24h_quote") or 0), "atr1h": snapshot.get("atr1h") or snapshot.get("atr_1h") or snapshot.get("atr"), "c_1h_ret": snapshot.get("c_1h_ret"), "trend_direction": snapshot.get("structure_1h", "NEUTRAL"), "adx_1h": snapshot.get("adx_1h"), "smart_money": snapshot.get("smart_money", {}), "calculus": {"velocity_1h": calculus.get("velocity"), "accel_1h": calculus.get("acceleration"), "jerk_1h": calculus.get("max_abs_jerk"), "impulse_1h": calculus.get("impulse"), "energy_1h": (calculus.get("definite_integrals") or {}).get("energy_integral"), "action_area_1h": (calculus.get("definite_integrals") or {}).get("deviation_area_integral"), "state_1h": calculus.get("regime")}, "decision": decision})
    errors = [e for e in (account_error, position_error, order_error, protection_error, account_book_error) if e]
    book_stats = _account_book_stats(account_book)
    history_view = []
    for row in reversed(decision_history[-200:]):
        stamp = int(row.get("generated_at_ms") or 0) if isinstance(row, dict) else 0
        history_view.append({**row, "time": dt.datetime.fromtimestamp(stamp / 1000).strftime("%Y-%m-%d %H:%M:%S") if stamp else "时间未记录", "macro_assessment": f"{(row.get('risk_snapshot') or {}).get('label', profile.label)}档位 · Gate {(row.get('environment') or s.environment).upper()} · {(row.get('trade') or {}).get('status', 'not_submitted')}"})
    return {"timestamp": str(int(time.time() * 1000)), "is_stale": bool(errors), "account": {"total_eq": float(account.get("total") or 0), "avail_eq": float(account.get("available") or 0), "upl": float(account.get("unrealised_pnl") or 0), "currency": s.settle.upper(), "margin_usage_pct": 0, "initial_capital": s.initial_capital_usd or None}, "positions_summary": {"total_count": len(positions), "long_count": sum(p["side"] == "long" for p in positions), "short_count": sum(p["side"] == "short" for p in positions), "items": positions}, "pending_orders": pending, "factors": factors, "factor_library": factor_snapshot, "macro_assessment": "Gate Futures 原生行情、因子与账户数据巡检中", "llm_runtime": {"model": os.getenv("LLM_MODEL", "Gate AI Worker"), "provider_name": "Gate-native", "reasoning_effort": os.getenv("LLM_REASONING_EFFORT", "high"), "api_format": "openai_chat"}, "logs": [f"Gate {s.environment.upper()} · {profile.label} · {s.leverage:g}x · 私有数据{'可用' if not errors else '未配置或不可用'}"], "trades": [], "today_stats": book_stats, "news_intelligence": [], "protection_orders": protections_raw or [], "ai_brain_history": history_view, "risk_snapshot": risk_snapshot, "safety_status": _safety_status(), "gate_environment": s.environment, "gate_public_market_environment": s.public_market_environment, "errors": sorted(set(errors))}


@app.get("/api/v1/contracts/{contract}")
def contract(contract: str):
    try:
        return client().contracts(contract.upper())
    except Exception as exc:
        raise HTTPException(502, f"Gate contract request failed: {exc}") from exc


@app.get("/api/v1/orders/{contract}/{order_id}")
def get_order(contract: str, order_id: str, x_gate_session: str | None = Header(default=None, alias="X-R20-Session")):
    _require_control_admin(x_gate_session)
    try:
        return client().order(order_id, contract.upper())
    except Exception as exc:
        raise HTTPException(502, f"Gate order query failed: {exc}") from exc


@app.post("/api/v1/orders")
def place_order(payload: GateOrderRequest, x_gate_session: str | None = Header(default=None, alias="X-R20-Session")):
    actor = _require_control_admin(x_gate_session)
    if payload.size == 0:
        raise HTTPException(400, "Gate order size cannot be zero")
    c = client()
    try:
        settings = load_settings()
        profile = get_risk_profile(settings.risk_profile)
        writes_enabled = settings.testnet_execute_trades if settings.environment == "testnet" else settings.live_trading_enabled
        if not writes_enabled or not profile.execution_allowed:
            raise PermissionError(f"Gate {settings.environment} order writes are disabled by the execution switch or risk profile")
        if payload.leverage is not None and abs(payload.leverage - settings.leverage) > 1e-9:
            raise ValueError(f"Order leverage must match configured Gate leverage {settings.leverage:g}x")
        risk = _order_risk(c, contract=payload.contract, size=payload.size, leverage=settings.leverage)
        try:
            position = c.positions(payload.contract) or {}
        except RuntimeError as exc:
            if "POSITION_NOT_FOUND" not in str(exc):
                raise
            position = {}
        cross_margin = str(position.get("pos_margin_mode") or "cross").lower() == "cross" or float(position.get("leverage") or 0) == 0
        c.update_position_leverage(contract=payload.contract, leverage=settings.leverage, cross_margin=cross_margin)
        result = trading_service(c).place_order(contract=payload.contract, size=payload.size, price=payload.price, tif=payload.tif, client_id=payload.client_id, reduce_only=payload.reduce_only, close=payload.close, risk=risk)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Gate order failed: {exc}") from exc
    store.add("gate.order.created", {"actor": actor.get("username"), "contract": payload.contract, "client_id": payload.client_id})
    return result


@app.delete("/api/v1/orders/{contract}/{order_id}")
def cancel_order(contract: str, order_id: str, x_gate_session: str | None = Header(default=None, alias="X-R20-Session")):
    actor = _require_control_admin(x_gate_session)
    try:
        result = client().cancel_order(order_id, contract.upper())
    except Exception as exc:
        raise HTTPException(502, f"Gate cancel failed: {exc}") from exc
    store.add("gate.order.cancelled", {"actor": actor.get("username"), "contract": contract.upper(), "order_id": order_id})
    return result


@app.get("/api/v1/protections/{contract}")
def protection_orders(contract: str, x_gate_session: str | None = Header(default=None, alias="X-R20-Session")):
    _require_control_admin(x_gate_session)
    try:
        return client().protection_orders(contract.upper())
    except Exception as exc:
        raise HTTPException(502, f"Gate protection query failed: {exc}") from exc


@app.post("/api/v1/protections")
def create_protection(payload: GateProtectionRequest, x_gate_session: str | None = Header(default=None, alias="X-R20-Session")):
    actor = _require_control_admin(x_gate_session)
    if payload.size == 0 and not payload.close:
        raise HTTPException(400, "Protection size cannot be zero unless close=true")
    try:
        result = client().create_protection_order(**payload.model_dump())
    except Exception as exc:
        raise HTTPException(502, f"Gate protection order failed: {exc}") from exc
    store.add("gate.protection.created", {"actor": actor.get("username"), "contract": payload.contract, "client_id": payload.client_id})
    return result


@app.get("/api/v1/protections/{contract}/coverage")
def protection_coverage(contract: str, position_size: float, x_gate_session: str | None = Header(default=None, alias="X-R20-Session")):
    _require_control_admin(x_gate_session)
    try:
        covered = trading_service().verify_protection_coverage(contract.upper(), position_size)
    except Exception as exc:
        raise HTTPException(502, f"Gate protection coverage check failed: {exc}") from exc
    if not covered:
        raise HTTPException(409, "Gate native protection orders do not fully cover the position")
    return {"contract": contract.upper(), "position_size": position_size, "covered": True}


@app.delete("/api/v1/protections/{order_id}")
def cancel_protection(order_id: str, x_gate_session: str | None = Header(default=None, alias="X-R20-Session")):
    actor = _require_control_admin(x_gate_session)
    try:
        result = client().cancel_protection_order(order_id)
    except Exception as exc:
        raise HTTPException(502, f"Gate protection cancel failed: {exc}") from exc
    store.add("gate.protection.cancelled", {"actor": actor.get("username"), "order_id": order_id})
    return result


@app.get("/api/v1/market/{symbol}/candles")
def candles(symbol: str, bar: str = "1H", limit: int = 150):
    contract_name = symbol.upper().replace("-USDT-SWAP", "_USDT").replace("-", "_")
    intervals = {"1M": "1m", "5M": "5m", "15M": "15m", "30M": "30m", "1H": "1h", "4H": "4h", "1D": "1d"}
    interval = intervals.get(bar.upper(), "1h")
    try:
        raw = market_client().candlesticks(contract_name, interval, max(1, min(limit, 1000)))
        rows = []
        for item in raw or []:
            rows.append({"ts": int(float(item.get("t") or 0)) * 1000, "open": float(item.get("o") or 0), "high": float(item.get("h") or 0), "low": float(item.get("l") or 0), "close": float(item.get("c") or 0), "vol": float(item.get("v") or 0)})
        return {"instId": contract_name, "bar": bar, "candles": rows, "source": "Gate Futures REST"}
    except Exception as exc:
        raise HTTPException(502, f"Gate candle request failed: {exc}") from exc


@app.get("/api/v1/admin/gate/runtime")
def gate_admin_runtime(x_gate_session: str | None = Header(default=None, alias="X-R20-Session")):
    _require_control_admin(x_gate_session)
    s = load_settings()
    tunnel = gate_tunnel.status()
    shared_tunnel_ready = False
    if tunnel.enabled:
        try:
            with socket.create_connection((tunnel.local_host, tunnel.local_port), timeout=0.4):
                shared_tunnel_ready = True
        except OSError:
            pass
    tunnel_payload = {**tunnel.__dict__, "running": bool(tunnel.running or shared_tunnel_ready), "pid": tunnel.pid or None}
    profile = get_risk_profile(s.risk_profile)
    snapshot = profile.snapshot(leverage=s.leverage, environment=s.environment)
    return {"exchange": "gate", "environment": s.environment, "ready": bool(s.api_key and s.api_secret), "base_url": s.base_url, "public_market_environment": s.public_market_environment, "proxy_configured": bool(s.proxy_url), "proxy_url": s.proxy_url or "", "proxy_required": s.require_proxy, "tunnel": tunnel_payload, "testnet_execute_trades": bool(s.testnet_execute_trades), "live_trading_enabled": bool(s.environment == "live" and s.live_trading_enabled), "credentials": {"testnet_configured": bool(os.getenv("GATE_TESTNET_API_KEY") and os.getenv("GATE_TESTNET_API_SECRET")), "live_configured": bool(os.getenv("GATE_LIVE_API_KEY") and os.getenv("GATE_LIVE_API_SECRET"))}, "risk_profiles": profile_catalog(), "risk_snapshot": snapshot, "risk": {"risk_profile": s.risk_profile, "leverage": s.leverage, "max_position_notional_usd": s.max_position_notional_usd, "max_total_margin_usd": s.max_total_margin_usd, "max_order_margin_usd": s.max_order_margin_usd}}


@app.get("/api/v1/admin/runtime")
def gate_admin_runtime_overview(x_gate_session: str | None = Header(default=None, alias="X-R20-Session")):
    _require_control_admin(x_gate_session)
    decisions = _read_json_file("ai_brain_decisions.json", {})
    history = _read_json_file("ai_decision_history.json", [])
    if not isinstance(history, list):
        history = []
    full = []
    if isinstance(decisions, dict):
        for symbol, envelope in decisions.items():
            if not isinstance(envelope, dict) or not isinstance(envelope.get("decision"), dict):
                continue
            d = envelope["decision"]
            full.append({"instId": envelope.get("instId", symbol), "action": d.get("action", "WAIT"), "confidence": d.get("confidence", 0), "timestamp": envelope.get("decision_timestamp_ms", decisions.get("generated_at_ms", 0)), "reason": d.get("rejection_reason") or d.get("summary_reason", "")})
    settings = load_settings()
    health_files = [_file_health("ai_brain_decisions.json", 15 * 60), _file_health("factor_library_snapshot.json", 60), _file_health("news_sentiment.json", 10 * 60)]
    try:
        from r20_backend.llm_manager import get_active_llm_runtime
        raw_llm = get_active_llm_runtime()
        llm_runtime = {key: raw_llm.get(key) for key in ("model", "name", "provider_name", "provider_id", "api_format", "reasoning_effort", "reasoning_type", "thinking_timeout")}
    except Exception:
        llm_runtime = {"model": os.getenv("LLM_MODEL", ""), "provider_name": "Gate AI Worker", "reasoning_effort": os.getenv("LLM_REASONING_EFFORT", "high")}
    configuration = {"Gate 当前环境": settings.environment.upper(), "Gate API 凭证": "已配置" if settings.api_key and settings.api_secret else "未配置", "AI 风险档位": get_risk_profile(settings.risk_profile).label, "Gate Testnet 自动交易": "已启用" if settings.testnet_execute_trades else "关闭", "Gate Live 交易开关": "显式启用" if settings.environment == "live" and settings.live_trading_enabled else "关闭 (FAIL-CLOSED)", "Gate VPS 独立代理": "已配置" if settings.proxy_url else "未配置", "执行杠杆": f"{settings.leverage:g}x", "原生保护单覆盖": "Gate price_orders 双保护校验", "总持仓名义敞口上限": f"{settings.max_position_notional_usd:.2f} USDT", "总保证金上限": f"{settings.max_total_margin_usd:.2f} USDT", "单笔保证金上限": f"{settings.max_order_margin_usd:.2f} USDT"}
    gateway_store = GatewayStore(GATEWAY_DB_PATH)
    gateway_pid = current_pid()
    return {"service": {"version": app.version, "pid": os.getpid(), "uptime_seconds": int(time.time() - STARTED_AT)}, "exchange": "gate", "environment": settings.environment, "credentials": {"gate": bool(settings.api_key and settings.api_secret), "llm": bool(os.getenv("LLM_API_KEY"))}, "configuration": configuration, "data_health": {"overall": "LIVE" if all(item["fresh"] for item in health_files) else "STALE", "files": health_files}, "safety_status": _safety_status(), "trader_heartbeat": _heartbeat_status(), "gateway": {"running": bool(gateway_pid or _worker_lock_held()), "pid": gateway_pid or None, "scheduler": scheduler_snapshot(gateway_store)}, "full_decisions": full, "decisions": full, "decision_history": list(reversed(history)), "decision_history_total": len(history), "recent_logs": [], "logs": {"trader": _tail_log("gate_trader.log", 18), "backend": _tail_log("gate_backend.log", 18), "scheduler": _tail_log("r20_gateway.log", 18)}, "llm_runtime": llm_runtime}


@app.get("/api/v1/admin/history")
def gate_admin_history(page: int = 1, page_size: int = 20, query: str = "", x_gate_session: str | None = Header(default=None, alias="X-R20-Session")):
    _require_control_admin(x_gate_session)
    settings = load_settings()
    c = client()
    history = _read_json_file("ai_decision_history.json", [])
    if not isinstance(history, list):
        history = []
    # Keep the full AI decision ledger available to the UI; filter client-side by timestamp/symbol/reason.
    decision_items = []
    needle = query.strip().lower()
    for cycle in reversed(history):
        if not isinstance(cycle, dict):
            continue
        text = json.dumps(cycle, ensure_ascii=False).lower()
        if needle and needle not in text:
            continue
        decision_items.append(cycle)
    start = max(0, (max(1, page) - 1) * max(1, min(page_size, 100)))
    size = max(1, min(page_size, 100))
    decision_page = decision_items[start:start + size]

    records: list[dict] = []
    try:
        for status in ("open", "finished"):
            for order in c.list_orders(status=status, limit=100):
                if not isinstance(order, dict) or not order.get("id"):
                    continue
                occurred = int(float(order.get("finish_time_ms") or order.get("create_time_ms") or time.time() * 1000))
                records.append({"environment": settings.environment, "record_type": "order", "external_id": str(order["id"]), "occurred_at": occurred, "contract": order.get("contract"), "status": order.get("status"), "detail": order})
    except Exception as exc:
        _backend_log(f"history order sync failed: {exc}")
    try:
        for row in c.account_book(from_time=int(time.time()) - 90 * 86400, to_time=int(time.time()), limit=1000):
            if not isinstance(row, dict):
                continue
            external = str(row.get("id") or row.get("trade_id") or f"{row.get('time')}-{row.get('text')}")
            occurred = int(float(row.get("time_ms") or row.get("time") or time.time() * 1000))
            records.append({"environment": settings.environment, "record_type": "account_book", "external_id": external, "occurred_at": occurred, "contract": row.get("contract"), "status": row.get("type"), "detail": row})
    except Exception as exc:
        _backend_log(f"history account-book sync failed: {exc}")
    store.upsert_trade_records(records)
    trade_page = store.trade_history(environment=settings.environment, page=page, page_size=page_size, query=query)
    return {"decision_history": decision_page, "decision_total": len(decision_items), "decision_pages": max(1, (len(decision_items) + size - 1) // size), "trade_history": trade_page}


@app.get("/api/v1/admin/config")
def gate_admin_control_config(x_gate_session: str | None = Header(default=None, alias="X-R20-Session")):
    _require_control_admin(x_gate_session)
    settings = load_settings()
    return {"authentication_mode": "account-password", "configuration": {"Gate 当前环境": settings.environment.upper(), "Gate API 凭证": "已配置" if settings.api_key and settings.api_secret else "未配置", "Gate Live 交易开关": "显式启用" if settings.environment == "live" and settings.live_trading_enabled else "关闭 (FAIL-CLOSED)", "Gate VPS 独立代理": "已配置" if settings.proxy_url else "未配置"}}


@app.get("/api/v1/admin/agents")
def gate_admin_agents(x_gate_session: str | None = Header(default=None, alias="X-R20-Session")):
    _require_control_admin(x_gate_session)
    gateway_store = GatewayStore(GATEWAY_DB_PATH)
    return {"agents": agent_statuses(gateway_store.job_runs(100)), "model_stats": gateway_store.model_stats(), "model_calls": gateway_store.model_calls(50), "prompt_policy": "Gate 交易主脑由 Python 直接构造提示词；仅保存调用时延、Token 和状态，不保存提示词或回复正文。", "secret_store": secret_store_status()}


@app.get("/api/v1/admin/logs")
def gate_admin_logs(source: str = "trader", lines: int = 100, x_gate_session: str | None = Header(default=None, alias="X-R20-Session")):
    _require_control_admin(x_gate_session)
    filename = LOG_SOURCES.get(source)
    if not filename:
        raise HTTPException(400, f"日志来源仅支持：{', '.join(LOG_SOURCES)}")
    content = _trader_log() if source == "trader" else _tail_log(filename, lines)
    return {"source": source, "file": filename, "content": content}


@app.get("/api/v1/admin/gateway")
def gate_gateway_status(limit: int = 50, x_gate_session: str | None = Header(default=None, alias="X-R20-Session")):
    _require_control_admin(x_gate_session)
    store = GatewayStore(GATEWAY_DB_PATH)
    pid = current_pid()
    running = bool(pid or _worker_lock_held())
    return {"version": "gate-0.4.0", "running": running, "pid": pid or None, "stats": store.stats(), "event_health": store.event_health(), "deliveries": store.recent(limit), "scheduler": scheduler_snapshot(store)}


@app.post("/api/v1/admin/gateway/activate")
def gate_gateway_activate(x_gate_session: str | None = Header(default=None, alias="X-R20-Session")):
    _require_control_admin(x_gate_session)
    start_gateway_supervisor()
    pid = current_pid()
    return {"ok": True, "running": bool(pid or _worker_lock_held()), "pid": pid or None, "message": "Gate Gateway 已请求激活，请刷新状态确认"}


@app.put("/api/v1/admin/gate/config")
def gate_admin_config(payload: GateAdminConfig, x_gate_session: str | None = Header(default=None, alias="X-R20-Session")):
    actor = _require_control_admin(x_gate_session)
    if (payload.testnet_api_key or payload.testnet_api_secret) and not (payload.testnet_api_key and payload.testnet_api_secret):
        raise HTTPException(400, "Testnet API Key 和 API Secret 必须成对填写；不能只更新其中一个")
    if (payload.live_api_key or payload.live_api_secret) and not (payload.live_api_key and payload.live_api_secret):
        raise HTTPException(400, "Live API Key 和 Live API Secret 必须成对填写；不能只更新其中一个")
    profile = get_risk_profile(payload.risk_profile)
    if payload.leverage > profile.max_leverage:
        raise HTTPException(400, f"{profile.label}档位的杠杆上限为 {profile.max_leverage:g}x")
    if payload.live_trading_enabled and payload.environment != "live":
        raise HTTPException(400, "Gate Live 开关只能在已选择 Live 环境时显式启用")
    testnet_key = (payload.testnet_api_key or os.getenv("GATE_TESTNET_API_KEY") or "").strip()
    testnet_secret = (payload.testnet_api_secret or os.getenv("GATE_TESTNET_API_SECRET") or "").strip()
    live_key = (payload.live_api_key or os.getenv("GATE_LIVE_API_KEY") or "").strip()
    live_secret = (payload.live_api_secret or os.getenv("GATE_LIVE_API_SECRET") or "").strip()
    if payload.environment == "testnet" and payload.testnet_execute_trades and not (testnet_key and testnet_secret):
        raise HTTPException(400, "启用 Gate Testnet 自动交易前必须配置完整 Testnet Key/Secret")
    if payload.environment == "live" and payload.live_trading_enabled and not (live_key and live_secret):
        raise HTTPException(400, "启用 Gate Live 实盘前必须配置完整 Live Key/Secret")
    managed_proxy = os.getenv("GATE_PROXY_URL", "") if gate_tunnel.enabled else (payload.proxy_url or "")
    values = {"GATE_ENVIRONMENT": payload.environment, "GATE_TESTNET_EXECUTE_TRADES": str(payload.testnet_execute_trades).lower(), "GATE_LIVE_TRADING_ENABLED": str(payload.live_trading_enabled).lower(), "GATE_RISK_PROFILE": payload.risk_profile, "GATE_LEVERAGE": str(payload.leverage), "GATE_PROXY_URL": managed_proxy, "GATE_MAX_POSITION_NOTIONAL_USD": str(payload.max_position_notional_usd), "GATE_MAX_TOTAL_MARGIN_USD": str(payload.max_total_margin_usd), "GATE_MAX_ORDER_MARGIN_USD": str(payload.max_order_margin_usd)}
    # Keep market data, credentials and order writes on the selected cluster.
    # In particular, a Live order must never be priced from Testnet data.
    values["GATE_PUBLIC_MARKET_ENV"] = payload.environment
    for env_key, value in (("GATE_TESTNET_API_KEY", payload.testnet_api_key), ("GATE_TESTNET_API_SECRET", payload.testnet_api_secret), ("GATE_LIVE_API_KEY", payload.live_api_key), ("GATE_LIVE_API_SECRET", payload.live_api_secret)):
        if value:
            values[env_key] = value.strip()
    _update_env(values)
    load_settings().validate()
    updated = load_settings()
    snapshot = get_risk_profile(updated.risk_profile).snapshot(leverage=updated.leverage, environment=updated.environment)
    store.add("gate.config.updated", {"actor": actor.get("username"), "environment": payload.environment, "risk_snapshot": snapshot})
    return {"updated": True, "environment": payload.environment, "risk_snapshot": snapshot}


@app.get("/api/v1/admin/gate/check")
def gate_admin_check(x_gate_session: str | None = Header(default=None, alias="X-R20-Session")):
    """Read-only Gate credential and connectivity check; never places an order."""
    _require_control_admin(x_gate_session)
    s = load_settings()
    if not s.api_key or not s.api_secret:
        return {"ok": False, "environment": s.environment, "detail": "当前环境尚未配置 Gate API Key/Secret"}
    try:
        account = client().account()
        return {"ok": True, "environment": s.environment, "detail": "Gate 私有 API 读取成功", "account": {"total": account.get("total"), "available": account.get("available")}}
    except Exception as exc:
        store.add("gate.credentials.check_failed", {"environment": s.environment, "error": str(exc)[:300]}, "WARN")
        return {"ok": False, "environment": s.environment, "detail": f"Gate 私有 API 检测失败：{exc}"}


@app.get("/api/v1/admin/gate/account-snapshot")
def gate_account_snapshot(x_gate_session: str | None = Header(default=None, alias="X-R20-Session")):
    _require_control_admin(x_gate_session)
    c = client()
    try:
        account = c.account()
        positions = c.positions()
        orders = c.open_orders()
        protections = c.protection_orders()
    except PermissionError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Gate Testnet 账户读取失败：{exc}") from exc
    minimums = []
    try:
        s = load_settings()
        profile = get_risk_profile(s.risk_profile)
        effective_cap = s.max_order_margin_usd * profile.margin_ratio
        m = market_client()
        ticker_map = {str(row.get("contract")): row for row in (m.tickers() or []) if isinstance(row, dict)}
        for contract_name in ("BTC_USDT", "ETH_USDT", "SOL_USDT", "DOGE_USDT", "SUI_USDT", "XRP_USDT"):
            meta = m.contracts(contract_name) or {}
            ticker = ticker_map.get(contract_name, {})
            price = float(ticker.get("mark_price") or ticker.get("last") or 0)
            min_size = float(meta.get("order_size_min") or 1)
            multiplier = float(meta.get("quanto_multiplier") or 0)
            min_margin = min_size * multiplier * price / s.leverage if price > 0 and multiplier > 0 else None
            minimums.append({"contract": contract_name, "enable_decimal": bool(meta.get("enable_decimal")), "order_size_min": meta.get("order_size_min"), "quanto_multiplier": meta.get("quanto_multiplier"), "minimum_margin_usdt": round(min_margin, 6) if min_margin is not None else None, "effective_margin_cap_usdt": round(effective_cap, 6), "executable_under_current_cap": bool(min_margin is not None and min_margin <= effective_cap)})
    except Exception as exc:
        minimums = [{"error": str(exc)}]
    return {"account": account, "positions": positions, "orders": orders, "protections": protections, "contract_minimums": minimums, "captured_at_ms": int(time.time() * 1000)}


# The migrated control plane handles /admin and all exchange-independent R20 modules.
# Gate-native routes above take precedence; the mounted app cannot override them.
from r20_backend.app import admin_auth as control_admin_auth
from r20_backend.app import app as control_plane_app

control_admin_auth.initialize_from_legacy(os.getenv("GATE_ADMIN_PASSWORD", ""))
app.mount("/", control_plane_app)
