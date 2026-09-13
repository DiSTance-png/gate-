"""Gate Futures environment selection with fail-closed live trading guards."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]


def _values(values: Mapping[str, str] | None = None) -> dict[str, str]:
    result = dict(os.environ)
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                result[key.strip()] = value.strip().strip('"').strip("'")
    if values:
        result.update(values)
    try:
        from r20_gateway.secrets import load_secrets
        result.update(load_secrets())
    except Exception:
        pass
    return result


@dataclass(frozen=True)
class GateEnvironment:
    mode: str
    api_key: str
    secret_key: str
    base_url: str
    proxy_url: str = ""
    live_trading_enabled: bool = False

    @property
    def simulated(self) -> bool:
        return self.mode == "testnet"

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.secret_key)

    @property
    def identity(self) -> str:
        fingerprint = hashlib.sha256(self.api_key.encode()).hexdigest()[:12] if self.api_key else "unconfigured"
        return f"gate:{self.mode}:{fingerprint}"

    @property
    def can_trade(self) -> bool:
        return self.configured and (self.mode == "testnet" or self.live_trading_enabled)


def selected_environment(values: Mapping[str, str] | None = None) -> GateEnvironment:
    env = _values(values)
    mode = str(env.get("R20_GATE_ENV", "testnet")).strip().lower()
    if mode not in {"testnet", "live"}:
        raise ValueError("R20_GATE_ENV 只能是 testnet 或 live")
    prefix = "GATE_TESTNET" if mode == "testnet" else "GATE_LIVE"
    api_key = str(env.get(f"{prefix}_API_KEY") or "")
    secret_key = str(env.get(f"{prefix}_SECRET_KEY") or "")
    default_url = "https://fx-api-testnet.gateio.ws" if mode == "testnet" else "https://api.gateio.ws"
    base_url = str(env.get(f"{prefix}_BASE_URL") or default_url).rstrip("/")
    if not base_url.startswith("https://"):
        raise ValueError("Gate Base URL 必须使用 HTTPS")
    proxy_url = str(env.get("R20_GATE_PROXY") or "").strip()
    live_enabled = str(env.get("R20_GATE_LIVE_TRADING_ENABLED", "0")).lower() in {"1", "true", "yes"}
    return GateEnvironment(mode, api_key, secret_key, base_url, proxy_url, live_enabled)


def selected_exchange(values: Mapping[str, str] | None = None) -> str:
    """Read the shared exchange selector using the same precedence as Gate config."""
    return str(_values(values).get("R20_EXCHANGE", "okx")).strip().lower() or "okx"
