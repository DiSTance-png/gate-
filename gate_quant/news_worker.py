"""Gate-owned news input with an explicit no-data fail-closed state.

The old OKX news CLI is deliberately not called.  A missing verified feed must
remain visible to the strategy instead of being represented as neutral news.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "news_sentiment.json"


def run() -> dict:
    payload = {
        "timestamp": int(time.time()),
        "exchange": "gate",
        "macro_sentiment": "UNVERIFIED",
        "latest_news": [],
        "circuit_breaker": {"active": False, "reason": "no verified Gate news feed configured"},
        "data_quality": "unavailable",
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False))
