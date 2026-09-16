from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv
from .risk_profiles import get_risk_profile

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)


@dataclass(frozen=True)
class GateSettings:
    environment: str = "testnet"
    api_key: str = ""
    api_secret: str = ""
    settle: str = "usdt"
    proxy_url: str | None = None
    timeout_seconds: float = 10.0
    live_trading_enabled: bool = False
    testnet_execute_trades: bool = False
    leverage: float = 3.0
    risk_profile: str = "standard"
    max_entries_per_cycle: int = 1
    max_position_notional_usd: float = 0.0
    max_total_margin_usd: float = 0.0
    max_order_margin_usd: float = 0.0
    public_market_environment: str = "live"
    require_proxy: bool = False
    initial_capital_usd: float = 0.0
    max_pending_order_age_seconds: int = 1800
    max_position_age_seconds: int = 57600
    stop_cooldown_seconds: int = 1800
    max_daily_loss_usd: float = 100.0
    max_daily_loss_ratio: float = 0.05
    testnet_base_url: str = "https://api-testnet.gateapi.io/api/v4"
    live_base_url: str = "https://fx-api.gateio.ws/api/v4"

    @property
    def base_url(self) -> str:
        # Gate documents Testnet on the general API v4 host. The fx-api host is
        # the documented Futures-only Live alternative, not a Testnet mirror.
        return self.testnet_base_url if self.environment == "testnet" else self.live_base_url

    def validate(self) -> None:
        if self.environment not in {"testnet", "live"}:
            raise ValueError("GATE_ENVIRONMENT must be testnet or live")
        if self.environment == "live" and (self.api_key and not self.api_secret or self.api_secret and not self.api_key):
            raise ValueError("Live credentials must be provided as a pair")
        if self.environment == "live" and self.live_trading_enabled and (not self.api_key or not self.api_secret):
            raise ValueError("Live credentials are required before live trading can be enabled")
        if self.environment == "live" and self.live_trading_enabled and self.public_market_environment != "live":
            raise ValueError("Live trading requires GATE_PUBLIC_MARKET_ENV=live")
        if self.environment == "testnet" and (self.api_key and not self.api_secret or self.api_secret and not self.api_key):
            raise ValueError("Testnet credentials must be provided as a pair")
        if self.environment == "testnet" and self.testnet_execute_trades and (not self.api_key or not self.api_secret):
            raise ValueError("Testnet credentials are required before Testnet automatic trading can be enabled")
        if not 1 <= self.leverage <= 100:
            raise ValueError("GATE_LEVERAGE must be between 1 and 100")
        profile = get_risk_profile(self.risk_profile)
        if self.leverage > profile.max_leverage:
            raise ValueError(f"GATE_LEVERAGE cannot exceed {profile.max_leverage:g}x for risk profile {profile.key}")
        if not 0 <= self.max_entries_per_cycle <= 2:
            raise ValueError("GATE_MAX_ENTRIES_PER_CYCLE must be between 0 and 2")
        if self.max_entries_per_cycle > profile.max_entries_per_cycle:
            raise ValueError(f"GATE_MAX_ENTRIES_PER_CYCLE cannot exceed {profile.max_entries_per_cycle} for risk profile {profile.key}")
        for name, value in (("max_position_notional_usd", self.max_position_notional_usd), ("max_total_margin_usd", self.max_total_margin_usd), ("max_order_margin_usd", self.max_order_margin_usd)):
            if value < 0:
                raise ValueError(f"{name} cannot be negative")
        if self.public_market_environment not in {"testnet", "live"}:
            raise ValueError("GATE_PUBLIC_MARKET_ENV must be testnet or live")
        if self.require_proxy and not self.proxy_url:
            raise ValueError("Gate network access is fail-closed: GATE_PROXY_URL is required")
        if self.initial_capital_usd < 0:
            raise ValueError("GATE_INITIAL_CAPITAL_USD cannot be negative")
        if min(self.max_pending_order_age_seconds, self.max_position_age_seconds, self.stop_cooldown_seconds) < 0:
            raise ValueError("Gate lifecycle time limits cannot be negative")
        if self.max_daily_loss_usd <= 0 or not 0 < self.max_daily_loss_ratio <= 1:
            raise ValueError("Gate daily loss limits must be configured fail-closed")
        for name, value in (("GATE_TESTNET_BASE_URL", self.testnet_base_url), ("GATE_LIVE_BASE_URL", self.live_base_url)):
            if not value.startswith(("https://", "http://")) or not value.rstrip("/").endswith("/api/v4"):
                raise ValueError(f"{name} must be an http(s) Gate API v4 base URL")


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def load_settings() -> GateSettings:
    try:
        from r20_gateway.secrets import load_secrets
        encrypted = load_secrets()
    except Exception:
        encrypted = {}
    environment = os.getenv("GATE_ENVIRONMENT", "testnet").strip().lower()
    prefix = "GATE_TESTNET" if environment == "testnet" else "GATE_LIVE"
    s = GateSettings(
        environment=environment,
        api_key=str(encrypted.get(f"{prefix}_API_KEY") or os.getenv(f"{prefix}_API_KEY", "")),
        api_secret=str(encrypted.get(f"{prefix}_API_SECRET") or os.getenv(f"{prefix}_API_SECRET", "")),
        settle=os.getenv("GATE_SETTLE", "usdt"),
        proxy_url=os.getenv("GATE_PROXY_URL") or None,
        timeout_seconds=float(os.getenv("GATE_TIMEOUT_SECONDS", "10")),
        live_trading_enabled=_bool("GATE_LIVE_TRADING_ENABLED"),
        testnet_execute_trades=_bool("GATE_TESTNET_EXECUTE_TRADES"),
        leverage=float(os.getenv("GATE_LEVERAGE", "3")),
        risk_profile=os.getenv("GATE_RISK_PROFILE", "standard").strip().lower(),
        max_entries_per_cycle=int(os.getenv("GATE_MAX_ENTRIES_PER_CYCLE", "1")),
        max_position_notional_usd=float(os.getenv("GATE_MAX_POSITION_NOTIONAL_USD", "0")),
        max_total_margin_usd=float(os.getenv("GATE_MAX_TOTAL_MARGIN_USD", "0")),
        max_order_margin_usd=float(os.getenv("GATE_MAX_ORDER_MARGIN_USD", "0")),
        public_market_environment=os.getenv("GATE_PUBLIC_MARKET_ENV", "live").strip().lower(),
        require_proxy=_bool("GATE_REQUIRE_PROXY"),
        initial_capital_usd=float(os.getenv("GATE_INITIAL_CAPITAL_USD", "0")),
        max_pending_order_age_seconds=int(os.getenv("GATE_MAX_PENDING_ORDER_AGE_SECONDS", "1800")),
        max_position_age_seconds=int(os.getenv("GATE_MAX_POSITION_AGE_SECONDS", "57600")),
        stop_cooldown_seconds=int(os.getenv("GATE_STOP_COOLDOWN_SECONDS", "1800")),
        max_daily_loss_usd=float(os.getenv("GATE_MAX_DAILY_LOSS_USD", "100")),
        max_daily_loss_ratio=float(os.getenv("GATE_MAX_DAILY_LOSS_RATIO", "0.05")),
        testnet_base_url=os.getenv("GATE_TESTNET_BASE_URL", "https://api-testnet.gateapi.io/api/v4").rstrip("/"),
        live_base_url=os.getenv("GATE_LIVE_BASE_URL", "https://fx-api.gateio.ws/api/v4").rstrip("/"),
    )
    s.validate()
    return s
