"""Small native OKX REST client; public endpoints work without credentials."""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request
from .config import settings
from scripts.okx_runtime import okx_urlopen


class OKXClient:
    def __init__(self) -> None:
        self.base_url = settings.okx_base_url.rstrip("/")

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        method = method.upper()
        query = urlencode(params) if method == "GET" else ""
        request_path = path + (f"?{query}" if query else "")
        url = f"{self.base_url}{request_path}"
        body = json.dumps(params, separators=(",", ":")).encode("utf-8") if method != "GET" else None
        headers = {"User-Agent": "R20-Standalone/6.6.2"}
        if body:
            headers["Content-Type"] = "application/json"
        if settings.okx_api_key and settings.okx_secret_key and settings.okx_passphrase:
            timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            prehash = timestamp + method + request_path + (body.decode("utf-8") if body else "")
            digest = hmac.new(settings.okx_secret_key.encode(), prehash.encode(), hashlib.sha256).digest()
            headers.update({
                "OK-ACCESS-KEY": settings.okx_api_key,
                "OK-ACCESS-SIGN": base64.b64encode(digest).decode(),
                "OK-ACCESS-TIMESTAMP": timestamp,
                "OK-ACCESS-PASSPHRASE": settings.okx_passphrase,
            })
            if settings.okx_simulated:
                headers["x-simulated-trading"] = "1"
        req = Request(url, data=body, headers=headers, method=method)
        with okx_urlopen(req, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("code") not in (None, "0", 0):
            raise RuntimeError(payload.get("msg", "OKX request failed"))
        return payload.get("data", payload)

    def ticker(self, inst_id: str) -> Any:
        return self._request("GET", "/api/v5/market/ticker", {"instId": inst_id})

    def candles(self, inst_id: str, bar: str = "1H", limit: int = 100) -> Any:
        return self._request("GET", "/api/v5/market/candles", {"instId": inst_id, "bar": bar, "limit": limit})

    def instruments(self, inst_type: str = "SWAP", inst_id: str | None = None) -> Any:
        params = {"instType": inst_type}
        if inst_id:
            params["instId"] = inst_id
        return self._request("GET", "/api/v5/public/instruments", params)

    def balance(self) -> Any:
        return self._request("GET", "/api/v5/account/balance")

    def positions(self) -> Any:
        return self._request("GET", "/api/v5/account/positions", {"instType": "SWAP"})

    def close_position(self, inst_id: str, pos_side: str) -> Any:
        if pos_side not in {"long", "short"}:
            raise ValueError("pos_side must be long or short")
        return self._request(
            "POST",
            "/api/v5/trade/close-position",
            {"instId": inst_id, "mgnMode": "cross", "posSide": pos_side, "autoCxl": "true"},
        )
