"""
get_ohlcv — Fetch latest BTCUSDT 1h candle from Binance
Writes output to ohlcv/YYYY-MM-DD-HH-MM.json under project root.

Usage:
    python3 backend/scripts/get_ohlcv.py
    python3 backend/scripts/get_ohlcv.py --symbol ETHUSDT --limit 1
"""

import argparse
import json
import os
from datetime import datetime, timezone

import requests

from common import project_root

BASE_URL = "https://api.binance.com/api/v3/klines"


def fetch_klines(symbol: str, interval: str, limit: int) -> list:
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    response = requests.get(BASE_URL, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def extract(candle: list) -> dict:
    open_time_ms = candle[0]
    dt = datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc)
    return {
        "time": dt.strftime("%Y-%m-%d %H:%M"),
        "open": float(candle[1]),
        "high": float(candle[2]),
        "low": float(candle[3]),
        "close": float(candle[4]),
        "volume": float(candle[5]),
    }


def main():
    parser = argparse.ArgumentParser(description="Fetch latest OHLCV candle from Binance")
    parser.add_argument("--symbol", default="BTCUSDT", help="Trading pair (default: BTCUSDT)")
    parser.add_argument("--interval", default="1h", help="Candle interval (default: 1h)")
    parser.add_argument("--limit", type=int, default=4, help="Number of candles to fetch (default: 4)")
    args = parser.parse_args()

    candles = fetch_klines(args.symbol, args.interval, args.limit)
    extracted = [extract(c) for c in candles]

    out_dir = os.path.join(project_root, "ohlcv")
    os.makedirs(out_dir, exist_ok=True)

    # Use the latest candle's timestamp for the filename
    latest_time = extracted[-1]["time"]
    dt = datetime.strptime(latest_time, "%Y-%m-%d %H:%M")
    filename = dt.strftime("%Y-%m-%d-%H-%M") + ".json"
    out_path = os.path.join(out_dir, filename)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(extracted, f, ensure_ascii=False, indent=2)

    print(f"Wrote {len(extracted)} candles (latest: {latest_time}) → {out_path}")


if __name__ == "__main__":
    main()
