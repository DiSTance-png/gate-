#!/usr/bin/env python3
"""Offline calculator for causal calculus snapshots and historical replay."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from calculus_engine import calculate_multi_timeframe


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/snapshots.json", help="JSON candle map or snapshot file")
    parser.add_argument("--output", default="data/calculus_snapshot.json")
    args = parser.parse_args()
    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)
    instruments = data.get("instruments", data) if isinstance(data, dict) else data
    result = {"engine": "causal-calculus-v1", "instruments": [], "skipped": []}
    if isinstance(instruments, list) and instruments and isinstance(instruments[0], dict) and "total_eq" in instruments[0]:
        result["skipped"].append({"reason": "account_equity_snapshots_are_not_ohlc_candles", "input": args.input})
        instruments = []
    for item in instruments if isinstance(instruments, list) else []:
        name = item.get("name", item.get("instId", "UNKNOWN"))
        candles = item.get("candles", item.get("timeframes"))
        if not isinstance(candles, dict):
            result["skipped"].append({"name": name, "reason": "missing_timeframe_ohlc_map"})
            continue
        try:
            features = calculate_multi_timeframe(candles)
        except (TypeError, ValueError, KeyError) as exc:
            result["skipped"].append({"name": name, "reason": str(exc)})
            continue
        result["instruments"].append({"name": name, "instId": item.get("instId", name), "calculus": features})
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
