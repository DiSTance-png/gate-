from __future__ import annotations

import json
from decimal import Decimal
from dataclasses import dataclass
from urllib.parse import urlencode
import requests

from .config import GateSettings
from .exchange_write_lock import gate_write_lock

PRICE_ORDER_EXPIRATION_QUANTUM_SECONDS = 86400
from .signing import gate_signature


class AmbiguousOrderError(RuntimeError):
    """Transport timeout after submission; caller must reconcile by client order id."""


@dataclass
class GateFuturesClient:
    settings: GateSettings
    session: requests.Session | None = None

    def __post_init__(self):
        self.settings.validate()
        self.session = self.session or requests.Session()
        if self.settings.proxy_url:
            self.session.proxies.update({"http": self.settings.proxy_url, "https": self.settings.proxy_url})

    def _request(self, method: str, endpoint: str, *, params: dict | None = None, payload: dict | None = None, private: bool = False):
        if private and method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            with gate_write_lock():
                return self._request_unlocked(method, endpoint, params=params, payload=payload, private=private)
        return self._request_unlocked(method, endpoint, params=params, payload=payload, private=private)

    def _request_unlocked(self, method: str, endpoint: str, *, params: dict | None = None, payload: dict | None = None, private: bool = False):
        body = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
        query = urlencode(params or {})
        path = "/api/v4" + endpoint
        headers = {"Accept": "application/json", "Content-Type": "application/json", "X-Gate-Size-Decimal": "1"}
        if private:
            if not self.settings.api_key or not self.settings.api_secret:
                raise PermissionError("Gate credentials are not configured")
            sig, ts = gate_signature(self.settings.api_secret, method, path, query, body)
            headers.update({"KEY": self.settings.api_key, "SIGN": sig, "Timestamp": ts})
        try:
            response = self.session.request(method, self.settings.base_url.replace("/api/v4", "") + path, params=params, data=body or None, headers=headers, timeout=self.settings.timeout_seconds)
        except requests.Timeout as exc:
            if method.upper() == "POST" and (endpoint.endswith("/orders") or endpoint.endswith("/price_orders")):
                raise AmbiguousOrderError("Gate order request timed out; reconcile by client order id before retrying") from exc
            raise RuntimeError(f"Gate {method.upper()} {endpoint} request timed out") from exc
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            try:
                error = response.json()
            except ValueError:
                error = {}
            label = str(error.get("label") or "HTTP_ERROR")
            message = str(error.get("message") or response.reason or "Gate request failed")
            raise RuntimeError(f"Gate API {label}: {message} (HTTP {response.status_code})") from exc
        return response.json() if response.content else None

    def tickers(self, contract: str | None = None): return self._request("GET", f"/futures/{self.settings.settle}/tickers", params={"contract": contract} if contract else None)
    def candlesticks(self, contract: str, interval: str = "1h", limit: int = 150): return self._request("GET", f"/futures/{self.settings.settle}/candlesticks", params={"contract": contract, "interval": interval, "limit": limit})
    def contracts(self, contract: str | None = None): return self._request("GET", f"/futures/{self.settings.settle}/contracts/{contract}" if contract else f"/futures/{self.settings.settle}/contracts")
    def account(self): return self._request("GET", f"/futures/{self.settings.settle}/accounts", private=True)
    def account_book(self, *, from_time: int | None = None, to_time: int | None = None, limit: int = 100):
        params = {"limit": max(1, min(int(limit), 1000))}
        if from_time is not None:
            params["from"] = int(from_time)
        if to_time is not None:
            params["to"] = int(to_time)
        return self._request("GET", f"/futures/{self.settings.settle}/account_book", params=params, private=True)
    def position_close(self, *, contract: str | None = None, limit: int = 100):
        params = {"limit": max(1, min(int(limit), 100))}
        if contract:
            params["contract"] = contract
        return self._request("GET", f"/futures/{self.settings.settle}/position_close", params=params, private=True)
    def positions(self, contract: str | None = None): return self._request("GET", f"/futures/{self.settings.settle}/positions/{contract}" if contract else f"/futures/{self.settings.settle}/positions", private=True)
    def list_orders(self, *, status: str = "open", contract: str | None = None, limit: int = 100, page: int = 1):
        if status not in {"open", "finished"}:
            raise ValueError("Gate order status must be open or finished")
        # Gate Futures API accepts at most 100 rows per order-list request.
        bounded_limit = max(1, min(int(limit), 100))
        params = {"status": status, "limit": bounded_limit, "offset": (max(1, int(page)) - 1) * bounded_limit}
        if contract:
            params["contract"] = contract
        return self._request("GET", f"/futures/{self.settings.settle}/orders", params=params, private=True)

    def open_orders(self, contract: str | None = None):
        return self.list_orders(status="open", contract=contract)
    def order(self, order_id: str, contract: str | None = None): return self._request("GET", f"/futures/{self.settings.settle}/orders/{order_id}", private=True)
    def find_by_client_id(self, client_id: str, contract: str):
        # Gate accepts the custom `text` value as order_id while the order is
        # open and briefly after completion. Fall back to native lists because
        # that lookup window is intentionally short.
        try:
            found = self.order(client_id)
            if isinstance(found, dict) and str(found.get("text") or "") == client_id:
                return found
        except RuntimeError:
            pass
        for status in ("open", "finished"):
            rows = self.list_orders(status=status, contract=contract, limit=100) or []
            for row in rows if isinstance(rows, list) else []:
                if str(row.get("text") or "") == client_id:
                    return row
        return None
    def find_protection_by_client_id(self, client_id: str, contract: str):
        rows = self.protection_orders(contract) or []
        for row in rows if isinstance(rows, list) else []:
            if str((row.get("initial") or {}).get("text") or "") == client_id:
                return row
        return None
    @staticmethod
    def _is_protection_price_order(row: dict) -> bool:
        initial = row.get("initial") or {}
        order_type = str(row.get("order_type") or "").lower()
        return bool(
            initial.get("reduce_only") or initial.get("is_reduce_only")
            or initial.get("close") or initial.get("is_close")
            or initial.get("auto_size")
            or order_type.startswith(("close-", "plan-close-"))
        )

    def price_orders(self, *, status: str = "open", contract: str | None = None):
        if status not in {"open", "finished"}:
            raise ValueError("Gate price-order status must be open or finished")
        return self._request(
            "GET",
            f"/futures/{self.settings.settle}/price_orders",
            params={"status": status, **({"contract": contract} if contract else {})},
            private=True,
        )

    def price_order(self, order_id: str):
        return self._request("GET", f"/futures/{self.settings.settle}/price_orders/{order_id}", private=True)

    def trigger_entry_orders(self, contract: str | None = None, *, status: str = "open"):
        rows = self.price_orders(status=status, contract=contract) or []
        return [row for row in rows if isinstance(row, dict) and not self._is_protection_price_order(row)]

    def find_trigger_entry_by_client_id(self, client_id: str, contract: str):
        for status in ("open", "finished"):
            for row in self.trigger_entry_orders(contract, status=status):
                if str((row.get("initial") or {}).get("text") or "") == client_id:
                    return row
        return None
    @staticmethod
    def _api_size(size: int | float | Decimal) -> int | float:
        value = Decimal(str(size))
        return int(value) if value == value.to_integral_value() else float(value)

    def create_order(self, *, contract: str, size: int | float | Decimal, price: str = "0", tif: str = "ioc", client_id: str, reduce_only: bool = False, close: bool = False, auto_size: str | None = None):
        payload = {"contract": contract, "size": self._api_size(size), "price": price, "tif": tif, "text": client_id, "reduce_only": reduce_only, "close": close}
        if auto_size is not None:
            if auto_size not in {"close_long", "close_short"}:
                raise ValueError("Gate dual-mode auto_size must be close_long or close_short")
            if Decimal(str(size)) != 0 or close:
                raise ValueError("Gate dual-mode auto_size requires size=0 and close=false")
            payload["auto_size"] = auto_size
        try:
            return self._request("POST", f"/futures/{self.settings.settle}/orders", payload=payload, private=True)
        except AmbiguousOrderError:
            raise

    def close_position(self, *, contract: str, client_id: str, position_mode: str | None = None):
        mode = str(position_mode or "").lower()
        if not mode:
            rows = self.positions(contract) or []
            rows = rows if isinstance(rows, list) else [rows]
            active = [row for row in rows if Decimal(str(row.get("size") or 0)) != 0]
            if len(active) != 1:
                raise RuntimeError(f"Gate close requires exactly one active {contract} position side; found {len(active)}")
            mode = str(active[0].get("mode") or "").lower()
        if mode in {"dual_long", "dual_short"}:
            side = "long" if mode == "dual_long" else "short"
            return self.create_order(
                contract=contract, size=0, price="0", tif="ioc", client_id=client_id,
                reduce_only=False, close=False, auto_size=f"close_{side}",
            )
        # Single-position mode uses Gate's size=0, close=true semantic.
        return self.create_order(contract=contract, size=0, price="0", tif="ioc", client_id=client_id, reduce_only=True, close=True)
    def update_position_leverage(self, *, contract: str, leverage: float, cross_margin: bool = True):
        if leverage < 1:
            raise ValueError("Gate leverage must be at least 1")
        value = format(float(leverage), "g")
        params = {"leverage": "0", "cross_leverage_limit": value} if cross_margin else {"leverage": value}
        return self._request("POST", f"/futures/{self.settings.settle}/positions/{contract}/leverage", params=params, private=True)
    def cancel_order(self, order_id: str, contract: str | None = None): return self._request("DELETE", f"/futures/{self.settings.settle}/orders/{order_id}", private=True)
    def create_protection_order(self, *, contract: str, size: int | float | Decimal, trigger_price: str, rule: int, client_id: str, reduce_only: bool = True, close: bool = False, expiration: int = 86400):
        if rule not in {1, 2}:
            raise ValueError("Gate trigger rule must be 1 (>=) or 2 (<=)")
        requested_size = Decimal(str(size))
        initial = {"contract": contract, "size": self._api_size(requested_size), "price": "0", "tif": "ioc", "reduce_only": reduce_only, "close": close, "text": client_id}
        payload = {"initial": initial, "trigger": {"price": trigger_price, "rule": rule, "expiration": expiration, "strategy_type": 0}}
        if requested_size != requested_size.to_integral_value():
            # Gate Futures accepts decimal entry sizes for enabled contracts,
            # while FuturesInitialOrder.size in /price_orders is int64. The
            # documented hedge-mode full-close form protects the complete
            # decimal position without sending an invalid fractional size.
            side = "long" if requested_size < 0 else "short"
            initial.update({"size": 0, "auto_size": f"close_{side}", "close": False})
            payload["order_type"] = f"close-{side}-position"
        try:
            return self._request("POST", f"/futures/{self.settings.settle}/price_orders", payload=payload, private=True)
        except AmbiguousOrderError:
            found = self.find_protection_by_client_id(client_id, contract)
            if found:
                return {"reconciled": True, "order": found}
            raise
    def create_trigger_entry_order(self, *, contract: str, size: int | float | Decimal,
                                   trigger_price: str, execution_price: str, rule: int,
                                   client_id: str, expiration: int):
        requested_size = Decimal(str(size))
        if requested_size == 0 or requested_size != requested_size.to_integral_value():
            raise ValueError("Gate Futures price-triggered entry size must be a non-zero integer contract count")
        if rule not in {1, 2}:
            raise ValueError("Gate trigger rule must be 1 (>=) or 2 (<=)")
        if int(expiration) < PRICE_ORDER_EXPIRATION_QUANTUM_SECONDS or int(expiration) % PRICE_ORDER_EXPIRATION_QUANTUM_SECONDS:
            raise ValueError("Gate Futures trigger expiration must be a whole number of days (86400 seconds)")
        payload = {
            "initial": {
                "contract": contract,
                "size": self._api_size(requested_size),
                "price": execution_price,
                "tif": "ioc",
                "reduce_only": False,
                "close": False,
                "text": client_id,
            },
            "trigger": {
                "price": trigger_price,
                "rule": rule,
                "expiration": int(expiration),
                "strategy_type": 0,
                "price_type": 0,
            },
        }
        try:
            return self._request("POST", f"/futures/{self.settings.settle}/price_orders", payload=payload, private=True)
        except AmbiguousOrderError:
            found = self.find_trigger_entry_by_client_id(client_id, contract)
            if found:
                return {"reconciled": True, "order": found}
            raise

    def protection_orders(self, contract: str | None = None):
        rows = self.price_orders(status="open", contract=contract) or []
        return [row for row in rows if isinstance(row, dict) and self._is_protection_price_order(row)]
    def cancel_protection_order(self, order_id: str): return self._request("DELETE", f"/futures/{self.settings.settle}/price_orders/{order_id}", private=True)
    def cancel_trigger_entry_order(self, order_id: str): return self._request("DELETE", f"/futures/{self.settings.settle}/price_orders/{order_id}", private=True)
