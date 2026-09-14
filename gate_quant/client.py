from __future__ import annotations

import json
from decimal import Decimal
from dataclasses import dataclass
from urllib.parse import urlencode
import requests

from .config import GateSettings
from .exchange_write_lock import gate_write_lock
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
    def _api_size(size: int | float | Decimal) -> int | float:
        value = Decimal(str(size))
        return int(value) if value == value.to_integral_value() else float(value)

    def create_order(self, *, contract: str, size: int | float | Decimal, price: str = "0", tif: str = "ioc", client_id: str, reduce_only: bool = False, close: bool = False):
        payload = {"contract": contract, "size": self._api_size(size), "price": price, "tif": tif, "text": client_id, "reduce_only": reduce_only, "close": close}
        try:
            return self._request("POST", f"/futures/{self.settings.settle}/orders", payload=payload, private=True)
        except AmbiguousOrderError:
            raise

    def close_position(self, *, contract: str, client_id: str):
        # Gate close-orders use size=0 and close=true; an opposite size is a
        # separate order and is rejected when close=true.
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
        payload = {"initial": {"contract": contract, "size": self._api_size(size), "price": "0", "tif": "ioc", "reduce_only": reduce_only, "close": close, "text": client_id}, "trigger": {"price": trigger_price, "rule": rule, "expiration": expiration, "strategy_type": 0}}
        try:
            return self._request("POST", f"/futures/{self.settings.settle}/price_orders", payload=payload, private=True)
        except AmbiguousOrderError:
            found = self.find_protection_by_client_id(client_id, contract)
            if found:
                return {"reconciled": True, "order": found}
            raise
    def protection_orders(self, contract: str | None = None): return self._request("GET", f"/futures/{self.settings.settle}/price_orders", params={"status": "open", **({"contract": contract} if contract else {})}, private=True)
    def cancel_protection_order(self, order_id: str): return self._request("DELETE", f"/futures/{self.settings.settle}/price_orders/{order_id}", private=True)
