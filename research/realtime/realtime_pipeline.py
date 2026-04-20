# research/pipeline.py
"""
MiroFish Realtime Pipeline
Fetches OHLCV + tweets → seed → simulation → metrics → predictions CSV.

Usage:
    python3 research/pipeline.py [--latest-time YYYY-MM-DD-HH-MM]
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from get_realtime_ohlcv import fetch_klines, extract as extract_candle
from get_realtime_tweets import fetch_tweets, build_query, ACCOUNTS, QUERY
from get_realtime_tweets import extract as extract_tweet
from ..gen_seed import format_ohlcv, format_tweets, format_agents, load_agents

BACKEND_PYTHON = os.path.join(project_root, "backend", ".venv", "bin", "python3")
RUN_TRADE      = os.path.join(project_root, "backend", "scripts", "run_trade.py")
CALC_METRICS   = os.path.join(project_root, "backend", "scripts", "calc_metrics.py")


# ── Pure helpers ──────────────────────────────────────────────────────────────

def interval_to_seconds(interval: str) -> int:
    if interval.endswith("h"):
        return int(interval[:-1]) * 3600
    if interval.endswith("m"):
        return int(interval[:-1]) * 60
    raise ValueError(f"Unknown interval: {interval}")


def floor_to_interval(dt: datetime, interval_secs: int) -> datetime:
    ts = int(dt.timestamp())
    floored = (ts // interval_secs) * interval_secs
    return datetime.fromtimestamp(floored, tz=timezone.utc)


def compute_latest_time(interval: str, now: datetime = None) -> datetime:
    if now is None:
        now = datetime.now(timezone.utc)
    secs = interval_to_seconds(interval)
    floored = floor_to_interval(now, secs)
    return floored - timedelta(seconds=2 * secs)


def dt_to_filename(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d-%H-%M")


def tweet_time_window(latest_time: datetime, interval_secs: int) -> int:
    """Return until_time as Unix seconds: end of the latest seed candle."""
    return int((latest_time + timedelta(seconds=interval_secs)).timestamp())


def split_candles(candles: list, limit: int):
    """Split limit+1 fetched candles into (seed_candles, actual_candle, prev_mid).

    seed_candles  = candles[:limit]    written to ohlcv file + seed
    actual_candle = candles[-1]        prediction target (actual low/high)
    prev_mid      = mid of candles[limit-2]  T-1 candle, used for DA metric
    """
    seed_candles  = candles[:limit]
    actual_candle = candles[-1]
    t_minus_1     = candles[limit - 2]
    prev_mid      = (t_minus_1["low"] + t_minus_1["high"]) / 2
    return seed_candles, actual_candle, prev_mid


def prepend_config(csv_path: str, latest_time: datetime, interval: str, limit: int) -> None:
    with open(csv_path, "r", encoding="utf-8") as f:
        original = f.read()
    config = (
        f"# latest_time={dt_to_filename(latest_time)}\n"
        f"# interval={interval}\n"
        f"# limit={limit}\n"
    )
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write(config + original)


def insert_summary_before_b_lines(csv_path: str, total_runtime_mins: float, mae: str, mda: str) -> None:
    """Insert total_runtime_mins/MAE/MDA before b_low/b_high lines written by calc_metrics."""
    with open(csv_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    b_idx   = next((i for i, l in enumerate(lines) if l.strip().startswith("b_low")), len(lines))
    summary = [
        f"\ntotal_runtime_mins,{total_runtime_mins:.1f}\n",
        f"MAE,{mae}\n",
        f"MDA,{mda}\n",
    ]
    lines = lines[:b_idx] + summary + lines[b_idx:]
    with open(csv_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


# ── Pipeline steps ────────────────────────────────────────────────────────────

def step_fetch_ohlcv(interval: str, limit: int, latest_time: datetime) -> list:
    """Fetch limit+1 candles pinned to latest_time (seed) + one actual candle."""
    raw = fetch_klines("BTCUSDT", interval, limit + 2)
    candles = [extract_candle(c) for c in raw]
    latest_str = latest_time.strftime("%Y-%m-%d %H:%M")
    seed_end = next((i for i, c in enumerate(candles) if c["time"] == latest_str), None)
    if seed_end is None:
        raise ValueError(f"latest_time {latest_str} not found in fetched candles")
    return candles[seed_end - limit + 1 : seed_end + 2]


def step_write_ohlcv(seed_candles: list, latest_time: datetime, interval: str) -> str:
    out_dir  = os.path.join(project_root, "research", "data", "ohlcv")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{dt_to_filename(latest_time)}-{interval}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(seed_candles, f, ensure_ascii=False, indent=2)
    return out_path


def step_fetch_tweets(latest_time: datetime, interval_secs: int) -> str:
    until      = tweet_time_window(latest_time, interval_secs)
    full_query = build_query(ACCOUNTS, QUERY, until)
    tweets_raw   = fetch_tweets(full_query)
    tweets       = [extract_tweet(t) for t in tweets_raw]

    out_dir  = os.path.join(project_root, "research", "data", "tweets")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{dt_to_filename(latest_time)}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(tweets, f, ensure_ascii=False, indent=2)
    return out_path


def step_gen_seed(seed_candles: list, tweets_path: str, latest_time: datetime,
                  interval: str, limit: int) -> str:
    with open(tweets_path, encoding="utf-8") as f:
        tweets = json.load(f)

    chart_time   = latest_time.strftime("%Y-%m-%d %H:%M")
    latest_price = seed_candles[-1]["close"]
    agents       = load_agents(os.path.join(project_root, "research", "agents.json"))

    tweet_limit  = int(os.getenv("TWEET_LIMIT", "5"))
    actual_tweets = min(tweet_limit, len(tweets))
    agent_count  = len(agents)

    content = "\n\n".join([
        f"# Latest Chart Time\n{chart_time}",
        f"# Latest BTC Price\n{latest_price}",
        format_ohlcv(seed_candles),
        format_tweets(tweets),
        format_agents(agents),
    ]) + "\n"

    out_dir  = os.path.join(project_root, "research", "seeds")
    os.makedirs(out_dir, exist_ok=True)
    filename = f"{dt_to_filename(latest_time)}-{interval}-{limit}-{actual_tweets}-{agent_count}.md"
    out_path = os.path.join(out_dir, filename)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
    return out_path


def step_run_trade(seed_path: str, output_csv: str,
                   actual_low: float, actual_high: float, prev_mid: float, rounds: int) -> None:
    subprocess.run(
        [
            BACKEND_PYTHON, RUN_TRADE, seed_path,
            "-o", output_csv,
            "--actual-low",  str(actual_low),
            "--actual-high", str(actual_high),
            "--prev-mid",    str(prev_mid),
            "--rounds",      str(rounds),
        ],
        check=True,
    )


def step_calc_metrics(output_csv: str) -> str:
    result = subprocess.run(
        [BACKEND_PYTHON, CALC_METRICS, output_csv],
        check=True, capture_output=True, text=True,
    )
    return result.stdout


def _parse_stdout_metric(stdout: str, key: str) -> str:
    match = re.search(rf"^{key}=(.+)$", stdout, re.MULTILINE)
    return match.group(1).strip() if match else "NA"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="MiroFish Realtime Pipeline")
    parser.add_argument("--latest-time", default=None,
                        help="Latest candle time YYYY-MM-DD-HH-MM (default: auto)")
    args = parser.parse_args()

    interval = os.getenv("INTERVAL", "1h")
    limit    = int(os.getenv("LIMIT", "4"))
    rounds   = int(os.getenv("ROUNDS", "5"))

    if limit < 2:
        print("Error: LIMIT must be >= 2")
        sys.exit(1)

    pipeline_start = time.time()
    now_time       = datetime.now(timezone.utc)
    interval_secs  = interval_to_seconds(interval)

    if args.latest_time:
        latest_time = datetime.strptime(args.latest_time, "%Y-%m-%d-%H-%M").replace(tzinfo=timezone.utc)
    else:
        latest_time = compute_latest_time(interval, now=now_time)

    out_dir    = os.path.join(project_root, "research", "predictions")
    os.makedirs(out_dir, exist_ok=True)
    output_csv = os.path.join(out_dir, f"{dt_to_filename(now_time)}.csv")

    print(f"MiroFish Pipeline | latest_time={dt_to_filename(latest_time)} | interval={interval} | limit={limit} | rounds={rounds}")
    print("=" * 60)

    print("[1/5] Fetching OHLCV...")
    all_candles = step_fetch_ohlcv(interval, limit, latest_time)
    seed_candles, actual_candle, prev_mid = split_candles(all_candles, limit)
    ohlcv_path = step_write_ohlcv(seed_candles, latest_time, interval)
    print(f"  → {ohlcv_path} ({len(seed_candles)} candles)")
    print(f"  actual: low={actual_candle['low']} high={actual_candle['high']}")
    print(f"  prev_mid={prev_mid:.2f}")

    expected_tweets = os.path.join(project_root, "research", "data", "tweets", f"{dt_to_filename(latest_time)}.json")
    if os.path.exists(expected_tweets):
        tweets_path = expected_tweets
        print(f"[2/5] Tweets already cached → {tweets_path}")
    else:
        print("[2/5] Fetching tweets...")
        tweets_path = step_fetch_tweets(latest_time, interval_secs)
        print(f"  → {tweets_path}")

    print("[3/5] Generating seed...")
    seed_path = step_gen_seed(seed_candles, tweets_path, latest_time, interval, limit)
    print(f"  → {seed_path}")

    print("[4/5] Running simulation...")
    step_run_trade(seed_path, output_csv,
                   actual_candle["low"], actual_candle["high"], prev_mid, rounds)

    print("[5/5] Calculating metrics...")
    metrics_stdout = step_calc_metrics(output_csv)
    print(metrics_stdout.strip())

    total_runtime_mins = (time.time() - pipeline_start) / 60.0
    mae = _parse_stdout_metric(metrics_stdout, "MAE")
    mda = _parse_stdout_metric(metrics_stdout, "MDA")
    insert_summary_before_b_lines(output_csv, total_runtime_mins, mae, mda)
    prepend_config(output_csv, latest_time, interval, limit)

    print()
    print("=" * 60)
    print(f"Output: {output_csv}")
    print(f"Total runtime: {total_runtime_mins:.1f} mins")


if __name__ == "__main__":
    main()
