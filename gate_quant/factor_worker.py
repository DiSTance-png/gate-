"""Gate-native factor snapshot worker used by the isolated scheduler."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .ai_worker import SYMBOLS, _features, _strategy_package
from .client import GateFuturesClient
from .config import load_settings

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "factor_library_snapshot.json"


def run() -> dict:
    settings = load_settings()
    market = GateFuturesClient(settings)
    tickers = market.tickers() or []
    by_symbol = {str(row.get("contract")): row for row in tickers if isinstance(row, dict)}
    packages = []
    for symbol in SYMBOLS:
        feature = _features(market, symbol, by_symbol.get(symbol))
        packages.append(_strategy_package(feature, by_symbol.get(symbol)))
    payload = {
        "timestamp": int(time.time()),
        "exchange": "gate",
        "environment": settings.environment,
        "instruments": packages,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False))
