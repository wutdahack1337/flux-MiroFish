#!/usr/bin/env python3
"""Aggregate 1-min ohlcv CSVs from marketdata/ into a single resampled JSON.

Usage:
  Aggregate 1-minute market data into 5-minute candles for 2026-04-05:
    python3 research/aggregate_ohlcv.py --interval 5m --startday 2026-04-05 --endday 2026-04-05

  Aggregate into 1-hour candles for a date range:
    python3 research/aggregate_ohlcv.py --interval 1h --startday 2026-04-01 --endday 2026-04-05

  Aggregate into 4-hour candles with custom rolling windows (1-hour steps):
    python3 research/aggregate_ohlcv.py --interval 4h --roll-step 1h --startday 2026-04-01 --endday 2026-04-05

  Daily candles:
    python3 research/aggregate_ohlcv.py --interval 1d --startday 2026-04-01 --endday 2026-04-05

Supported intervals: 1m, 5m, 15m, 30m, 1h, 4h, 1d

Output:
  JSON files are written to research/data/ohlcv/ with names like:
    2026-04-05-5m.json (contains 5-minute candles for that day)
"""

import argparse
import json
import os
import re
from datetime import datetime, timedelta, timezone

import pandas as pd
from dotenv import load_dotenv

MARKETDATA_DIR = os.path.join(os.path.dirname(__file__), "..", "marketdata")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "data", "ohlcv")

INTERVAL_ALIASES = {
    "1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min",
    "1h": "1h", "4h": "4h", "1d": "1D",
}


def parse_args():
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
    default_interval = os.getenv("INTERVAL", "1h")

    p = argparse.ArgumentParser(description="Aggregate marketdata ohlcv CSVs")
    p.add_argument("--interval", default=default_interval, help=f"Resample interval: 1m,5m,15m,30m,1h,4h,1d (default: {default_interval} from .env)")
    p.add_argument("--roll-step", default=None,
                   help="Rolling step for candle timestamps (e.g. 1h). Default: same as --interval")
    p.add_argument("--startday", required=True, help="Start date YYYY-MM-DD (inclusive)")
    p.add_argument("--endday", required=True, help="End date YYYY-MM-DD (inclusive)")
    return p.parse_args()


def parse_duration(spec: str) -> timedelta:
    m = re.fullmatch(r"(\d+)([mhd])", spec.strip().lower())
    if not m:
        raise ValueError(f"Invalid duration '{spec}'. Use values like 30m, 1h, 4h, 1d")
    n = int(m.group(1))
    unit = m.group(2)
    if unit == "m":
        return timedelta(minutes=n)
    if unit == "h":
        return timedelta(hours=n)
    return timedelta(days=n)


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

    interval_delta = parse_duration(interval)
    roll_step_delta = parse_duration(args.roll_step) if args.roll_step else interval_delta
    expected_rows = int(interval_delta.total_seconds() // 60)

    start = datetime.strptime(args.startday, "%Y-%m-%d").date()
    end = datetime.strptime(args.endday, "%Y-%m-%d").date()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Load all source days once, including tail days needed for forward rolling windows.
    source_frames = []
    loaded_days = set()
    source_day = start
    source_end = end + interval_delta
    while source_day <= source_end:
        d = source_day.strftime("%Y-%m-%d")
        if d not in loaded_days:
            df = load_day(d)
            if df is not None:
                source_frames.append(df)
            else:
                print(f"  [warn] no data for {d}")
            loaded_days.add(d)
        source_day += timedelta(days=1)

    all_df = pd.concat(source_frames).sort_index() if source_frames else pd.DataFrame(
        columns=["open", "high", "low", "close", "volume"]
    )

    day = start
    while day <= end:
        date_str = day.strftime("%Y-%m-%d")
        day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        day_end = day_start + timedelta(days=1) - timedelta(minutes=1)

        records = []
        anchors = pd.date_range(start=day_start, end=day_end, freq=roll_step_delta)
        for ts in anchors:
            window_end = ts + interval_delta
            window = all_df[(all_df.index >= ts) & (all_df.index < window_end)]
            if len(window) < expected_rows:
                continue

            records.append({
                "time": ts.strftime("%Y-%m-%d %H:%M"),
                "open": round(float(window.iloc[0]["open"]), 5),
                "high": round(float(window["high"].max()), 5),
                "low": round(float(window["low"].min()), 5),
                "close": round(float(window.iloc[-1]["close"]), 5),
                "volume": round(float(window["volume"].sum()), 5),
            })

        out_name = f"{date_str}-{interval}.json"
        out_path = os.path.join(OUTPUT_DIR, out_name)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2)

        print(f"Wrote {len(records)} candles -> {out_path}")
        day += timedelta(days=1)


if __name__ == "__main__":
    main()
