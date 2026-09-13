"""Native Gate.io USDT perpetual client.

The client deliberately has no automatic retry for POST requests. A caller must
reconcile by client order id after a timeout before attempting anything again.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Any

from .gate_runtime import GateEnvironment, selected_environment


def _decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


class GateClient:
    def __init__(self, environment: GateEnvironment | None = None):
        self.environment = environment or selected_environment()

    def _sign(self, method: str, path: str, query: str, body: str, timestamp: str) -> str:
        body_hash = hashlib.sha512(body.encode()).hexdigest()
        payload = "\n".join([method.upper(), path, query, body_hash, timestamp])
        return hmac.new(self.environment.secret_key.encode(), payload.encode(), hashlib.sha512).hexdigest()

    def request(self, method: str, path: str, params: dict[str, Any] | None = None, *, private: bool = False, timeout: float = 15) -> Any:
        method = method.upper(); params = {k: v for k, v in (params or {}).items() if v is not None}
        query = urllib.parse.urlencode(params, doseq=True) if method in {"GET", "DELETE"} else ""
        body = "" if method in {"GET", "DELETE"} else json.dumps(params, separators=(",", ":"), ensure_ascii=False)
        request_path = path + (f"?{query}" if query else "")
        headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "R20-Gate/1.0"}
        if private:
            if not self.environment.configured:
                raise RuntimeError("Gate API 凭证未配置")
            timestamp = str(int(time.time()))
            headers.update({"KEY": self.environment.api_key, "Timestamp": timestamp, "SIGN": self._sign(method, path, query, body, timestamp)})
        request = urllib.request.Request(self.environment.base_url + request_path, data=body.encode() if body else None, headers=headers, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": self.environment.proxy_url, "https": self.environment.proxy_url})) if self.environment.proxy_url else urllib.request.build_opener()
        try:
            with opener.open(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8") or "null")
        except Exception as exc:
            raise RuntimeError(f"Gate 网络请求失败：{type(exc).__name__}: {exc}") from exc
        if isinstance(payload, dict) and payload.get("label"):
            raise RuntimeError(f"Gate {payload.get('label')}: {payload.get('message') or '请求失败'}")
        return payload

    def contracts(self, contract: str | None = None) -> Any:
        return self.request("GET", "/api/v4/futures/usdt/contracts", {"contract": contract} if contract else {})

    def ticker(self, contract: str) -> Any:
        return self.request("GET", "/api/v4/futures/usdt/tickers", {"contract": contract})

    def candles(self, contract: str, interval: str = "1h", limit: int = 100) -> Any:
        return self.request("GET", "/api/v4/futures/usdt/candlesticks", {"contract": contract, "interval": interval, "limit": limit})

    def balance(self) -> Any:
        return self.request("GET", "/api/v4/futures/usdt/accounts", private=True)

    def positions(self, contract: str | None = None) -> Any:
        return self.request("GET", "/api/v4/futures/usdt/positions", {"contract": contract} if contract else {}, private=True)

    def pending_orders(self, contract: str | None = None) -> Any:
        return self.request("GET", "/api/v4/futures/usdt/orders", {"status": "open", "contract": contract} if contract else {"status": "open"}, private=True)

    def order_by_text(self, contract: str, text: str) -> Any:
        return self.request("GET", "/api/v4/futures/usdt/orders", {"contract": contract, "text": text}, private=True)

    def cancel_order(self, order_id: str, contract: str) -> Any:
        return self.request("DELETE", f"/api/v4/futures/usdt/orders/{urllib.parse.quote(str(order_id))}", {"contract": contract}, private=True)

    def create_order(self, contract: str, size: int, price: str | float | None = None, *, reduce_only: bool = False, text: str | None = None, tif: str = "gtc", close: bool = False) -> Any:
        if not self.environment.can_trade:
            raise RuntimeError("Gate 实盘下单已阻止：必须显式设置 R20_GATE_LIVE_TRADING_ENABLED=1；当前仅允许 testnet")
        if not size:
            raise ValueError("Gate order size 不能为 0")
        payload: dict[str, Any] = {"contract": contract, "size": int(size), "reduce_only": bool(reduce_only), "tif": tif}
        if price is not None: payload["price"] = str(price)
        if text: payload["text"] = text[:28]
        if close: payload["close"] = True
        return self.request("POST", "/api/v4/futures/usdt/orders", payload, private=True)

    def request_okx_compat(self, method: str, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Translate the subset of OKX control-plane calls used by R20 strategies."""
        params = params or {}
        if path == "/api/v5/account/positions":
            rows = self.positions(params.get("instId"))
            result = []
            for row in rows if isinstance(rows, list) else []:
                size = int(row.get("size") or 0)
                if not size: continue
                result.append({"instId": str(row.get("contract", "")).replace("_USDT", "-USDT-SWAP"), "pos": str(abs(size)), "posSide": "long" if size > 0 else "short", "avgPx": row.get("entry_price"), "markPx": row.get("mark_price"), "lever": row.get("leverage"), "mgnMode": "cross", "posId": row.get("contract")})
            return result
        if path == "/api/v5/trade/orders-pending":
            rows = self.pending_orders(params.get("instId"))
            result = []
            for row in rows if isinstance(rows, list) else []:
                result.append({"ordId": row.get("id"), "instId": str(row.get("contract", "")).replace("_USDT", "-USDT-SWAP"), "side": "buy" if int(row.get("size") or 0) > 0 else "sell", "sz": str(abs(int(row.get("size") or 0))), "px": row.get("price"), "reduceOnly": row.get("reduce_only")})
            return result
        if path == "/api/v5/trade/order" and method.upper() == "GET":
            contract = self.normalize_contract(str(params.get("instId", "")))
            rows = self.order_by_text(contract, str(params.get("clOrdId"))) if params.get("clOrdId") else self.pending_orders(contract)
            if isinstance(rows, dict): rows = [rows]
            result = []
            for row in rows if isinstance(rows, list) else []:
                result.append({"ordId": row.get("id"), "clOrdId": row.get("text"), "state": row.get("status"), "sCode": "0"})
            return result
        if path == "/api/v5/account/balance":
            balance = self.balance()
            if isinstance(balance, dict):
                return [{"ccy": "USDT", "availBal": balance.get("available", balance.get("available_balance")), "cashBal": balance.get("total", balance.get("total_balance")), "eqUsd": balance.get("total", balance.get("total_balance"))}]
            return balance
        if path == "/api/v5/trade/order" and method.upper() == "POST":
            if params.get("attachAlgoOrds"):
                raise RuntimeError("Gate 保护单尚未映射：拒绝提交无止盈止损保护的订单")
            contract = self.normalize_contract(str(params.get("instId", "")))
            side = str(params.get("side", "buy")).lower()
            size = self.normalize_size(params.get("sz", 0)) * (1 if side == "buy" else -1)
            response = self.create_order(contract, size, params.get("px"), reduce_only=str(params.get("reduceOnly", "false")).lower() == "true", text=params.get("clOrdId"))
            order_id = response.get("id") if isinstance(response, dict) else None
            return [{"ordId": str(order_id or ""), "clOrdId": params.get("clOrdId", ""), "sCode": "0"}]
        if path == "/api/v5/trade/cancel-order" and method.upper() == "POST":
            result = self.cancel_order(str(params.get("ordId")), self.normalize_contract(str(params.get("instId", ""))))
            return [result] if isinstance(result, dict) else []
        if path == "/api/v5/trade/close-position" and method.upper() == "POST":
            contract = self.normalize_contract(str(params.get("instId", "")))
            positions = self.positions(contract)
            rows = positions if isinstance(positions, list) else []
            target = next((row for row in rows if int(row.get("size") or 0)), None)
            if not target:
                return [{"sCode": "0", "sMsg": "already closed"}]
            size = -int(target.get("size") or 0)
            result = self.create_order(contract, size, None, reduce_only=True, text=f"r20close{int(time.time())}", close=True)
            return [result] if isinstance(result, dict) else []
        raise RuntimeError(f"Gate 兼容层暂不支持 {method.upper()} {path}")

    @staticmethod
    def normalize_contract(symbol: str) -> str:
        value = symbol.upper().replace("-USDT-SWAP", "").replace("_USDT", "").replace("-USDT", "")
        return f"{value}_USDT"

    @staticmethod
    def normalize_size(value: Any, multiplier: Any = 1) -> int:
        return int(abs(_decimal(value) / (_decimal(multiplier) or Decimal(1))))
