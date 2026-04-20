#!/usr/bin/env python3
"""Aggregate 1-min ohlcv CSVs from marketdata/ into a single resampled JSON."""

import argparse
import json
import os
from datetime import datetime, timedelta, timezone

import pandas as pd

MARKETDATA_DIR = os.path.join(os.path.dirname(__file__), "..", "marketdata")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "data", "ohlcv")

INTERVAL_ALIASES = {
    "1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min",
    "1h": "1h", "4h": "4h", "1d": "1D",
}


def parse_args():
    p = argparse.ArgumentParser(description="Aggregate marketdata ohlcv CSVs")
    p.add_argument("--interval", required=True, help="Resample interval: 1m,5m,15m,30m,1h,4h,1d")
    p.add_argument("--startday", required=True, help="Start date YYYY-MM-DD (inclusive)")
    p.add_argument("--endday", required=True, help="End date YYYY-MM-DD (inclusive)")
    return p.parse_args()


def load_day(date_str: str) -> pd.DataFrame | None:
    path = os.path.join(MARKETDATA_DIR, date_str, "ohlcv.csv")
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path)
    df["time"] = pd.to_datetime(df["T"], unit="ms", utc=True)
    df = df.set_index("time")[["O", "H", "L", "C", "V"]]
    df.columns = ["open", "high", "low", "close", "volume"]
    return df


def main():
    args = parse_args()
    interval = args.interval
    pandas_freq = INTERVAL_ALIASES.get(interval)
    if pandas_freq is None:
        raise ValueError(f"Unknown interval '{interval}'. Choose from: {list(INTERVAL_ALIASES)}")

    start = datetime.strptime(args.startday, "%Y-%m-%d").date()
    end = datetime.strptime(args.endday, "%Y-%m-%d").date()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    day = start
    while day <= end:
        date_str = day.strftime("%Y-%m-%d")
        df = load_day(date_str)
        if df is None:
            print(f"  [warn] no data for {date_str}")
            day += timedelta(days=1)
            continue

        resampled = df.resample(pandas_freq).agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }).dropna()
        resampled = resampled[resampled.index.date == day]

        records = []
        for ts, row in resampled.iterrows():
            records.append({
                "time": ts.strftime("%Y-%m-%d %H:%M"),
                "open": round(row["open"], 5),
                "high": round(row["high"], 5),
                "low": round(row["low"], 5),
                "close": round(row["close"], 5),
                "volume": round(row["volume"], 5),
            })

        out_name = f"{date_str}-{interval}.json"
        out_path = os.path.join(OUTPUT_DIR, out_name)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2)

        print(f"Wrote {len(records)} candles -> {out_path}")
        day += timedelta(days=1)


if __name__ == "__main__":
    main()
