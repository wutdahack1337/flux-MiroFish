"""
gen_seed — Flux Exchange API → seed_YYYY-MM-DDTHH.md (BTC Futures)
Fetches OHLCV, liquidations, and news from the Flux exchange HTTP API,
then writes a structured Markdown seed file.
"""

import os
import argparse
from datetime import datetime, timezone, timedelta
import csv
import json

from common import project_root, resolve_path


# ── Time helpers ───────────────────────────────────────────────────────────

def hour_bounds_ms(dt: datetime):
    """Return (start_ms, end_ms) for a UTC hour (inclusive ms)."""
    floored = dt.replace(minute=0, second=0, microsecond=0)
    start = int(floored.timestamp() * 1000)
    end = int((floored + timedelta(hours=1)).timestamp() * 1000) - 1
    return start, end


def parse_hour(hour_str: str) -> datetime:
    """Parse YYYY-MM-DDTHH or YYYY-MM-DD HH into a UTC datetime."""
    for fmt in ("%Y-%m-%dT%H", "%Y-%m-%d %H"):
        try:
            return datetime.strptime(hour_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    raise ValueError(f"Cannot parse hour: {hour_str!r}  (expected YYYY-MM-DDTHH)")


def read_ohlcv_csv(path: str) -> list[dict]:
    """Read ohlcv.csv → list of {T, O, H, L, C, V} dicts. Returns [] if file missing."""
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            rows.append({
                "T": int(row["T"]),
                "O": float(row["O"]),
                "H": float(row["H"]),
                "L": float(row["L"]),
                "C": float(row["C"]),
                "V": float(row["V"]),
            })
    return rows


def read_liq_csv(path: str) -> list[dict]:
    """Read liq.csv → list of {S, q, p, T} dicts. Returns [] if file missing."""
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            rows.append({
                "S": row["S"],
                "q": float(row["q"]),
                "p": float(row["p"]),
                "T": int(row["T"]),
            })
    return rows


def aggregate_1h(rows: list[dict]) -> list[dict]:
    """
    Aggregate 1-minute OHLCV rows into 1H candles.
    Each row: {T (ms), O, H, L, C, V}. T of output candle = floor to hour start.
    Returns list sorted by T ascending.
    """
    buckets: dict[int, dict] = {}
    for row in sorted(rows, key=lambda r: r["T"]):
        hour_start_ms = (row["T"] // 3_600_000) * 3_600_000
        if hour_start_ms not in buckets:
            buckets[hour_start_ms] = {
                "T": hour_start_ms,
                "O": row["O"],
                "H": row["H"],
                "L": row["L"],
                "C": row["C"],
                "V": row["V"],
            }
        else:
            b = buckets[hour_start_ms]
            b["H"] = max(b["H"], row["H"])
            b["L"] = min(b["L"], row["L"])
            b["C"] = row["C"]
            b["V"] += row["V"]
    return sorted(buckets.values(), key=lambda c: c["T"])


def _days_in_window(end_dt: datetime, hours: int) -> list[str]:
    """Return list of YYYY-MM-DD strings for all calendar days (UTC) that overlap the window."""
    start_dt = end_dt - timedelta(hours=hours - 1)
    days = []
    cur = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end_day = end_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    while cur <= end_day:
        days.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return days


def load_ohlcv_window(marketdata_dir: str, end_dt: datetime, hours: int) -> list[dict]:
    """
    Load and aggregate 1H candles from marketdata CSVs for the window
    [end_dt - hours + 1h, end_dt] (inclusive). Returns list of 1H candle dicts.
    """
    start_dt = end_dt - timedelta(hours=hours - 1)
    start_ms = int(start_dt.replace(minute=0, second=0, microsecond=0).timestamp() * 1000)
    end_ms = int(end_dt.replace(minute=0, second=0, microsecond=0).timestamp() * 1000) + 3_599_999

    raw = []
    for day in _days_in_window(end_dt, hours):
        path = os.path.join(marketdata_dir, day, "ohlcv.csv")
        for row in read_ohlcv_csv(path):
            if start_ms <= row["T"] <= end_ms:
                raw.append(row)

    return aggregate_1h(raw)


def load_liq_window(marketdata_dir: str, end_dt: datetime, hours: int) -> dict:
    """
    Sum long and short liquidations from liq CSVs over the window.
    SELL rows = long liquidations; BUY rows = short liquidations.
    Returns {"long": float, "short": float}.
    """
    start_dt = end_dt - timedelta(hours=hours - 1)
    start_ms = int(start_dt.replace(minute=0, second=0, microsecond=0).timestamp() * 1000)
    end_ms = int(end_dt.replace(minute=0, second=0, microsecond=0).timestamp() * 1000) + 3_599_999

    total_long = 0.0
    total_short = 0.0
    for day in _days_in_window(end_dt, hours):
        path = os.path.join(marketdata_dir, day, "liq.csv")
        for row in read_liq_csv(path):
            if start_ms <= row["T"] <= end_ms:
                value = row["q"] * row["p"]
                if row["S"] == "SELL":
                    total_long += value
                else:
                    total_short += value

    return {"long": total_long, "short": total_short}


# ── Formatters ─────────────────────────────────────────────────────────────

def format_chart_time(end_dt: datetime) -> str:
    return f"# Latest Chart Time\n{end_dt.strftime('%Y-%m-%d %H:%M')}"


def format_btc_price(candles_1h: list[dict]) -> str:
    """Latest close = close of the highest-T candle."""
    if not candles_1h:
        return "# Latest BTC Price\nN/A"
    latest = max(candles_1h, key=lambda c: c["T"])
    return f"# Latest BTC Price\n{latest['C']}"


def format_ohlcv_json(candles_1h: list[dict]) -> str:
    """Render all 1H candles as JSON array, one object per line."""
    sorted_candles = sorted(candles_1h, key=lambda c: c["T"], reverse=True)
    items = []
    for c in sorted_candles:
        dt = datetime.fromtimestamp(c["T"] / 1000, tz=timezone.utc)
        obj = {
            "time": dt.strftime("%Y-%m-%d %H:%M"),
            "open": c["O"],
            "high": c["H"],
            "low": c["L"],
            "close": c["C"],
            "volume": round(c["V"], 2),
        }
        items.append(json.dumps(obj, separators=(',', ': ')))
    return "# OHLCV 1H\n[\n  " + ",\n  ".join(items) + "\n]"


def format_liquidations_json(liq: dict) -> str:
    """Render single-row liquidation summary as JSON."""
    return "# Liquidations\n" + json.dumps({"long": liq["long"], "short": liq["short"]})


def load_news(news_dir: str, end_dt: datetime) -> str:
    """Load news from news/YYYY-MM-DDTHH.md file if it exists."""
    news_file = os.path.join(news_dir, end_dt.strftime("%Y-%m-%dT%H.md"))
    if os.path.exists(news_file):
        with open(news_file, "r") as f:
            return f.read().strip()
    return "(no news)"


def format_news(news_content: str) -> str:
    return f"# News\n{news_content}"


# ── Agents ─────────────────────────────────────────────────────────────────


def load_agents(agents_path=None):
    if agents_path and os.path.exists(agents_path):
        with open(agents_path, encoding="utf-8") as f:
            return json.load(f)
    default = os.path.join(project_root, "research", "agents.json")
    if os.path.exists(default):
        with open(default, encoding="utf-8") as f:
            return json.load(f)
    return []


def format_agents(agents):
    return "# Agents Population\n" + json.dumps(agents, ensure_ascii=False, indent=2)


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    _now = datetime.now(tz=timezone.utc)
    _default_hour = _now.strftime("%Y-%m-%dT%H")

    parser = argparse.ArgumentParser(description="gen_seed — marketdata CSV → seed_YYYY-MM-DDTHH.md (BTC Futures)")
    parser.add_argument("--end-hour", default=_default_hour,
                        help="End hour of the last seed YYYY-MM-DDTHH in UTC (default: current hour)")
    parser.add_argument("--hours", type=int, default=72,
                        help="Hours of history per seed (default: 72)")
    parser.add_argument("--count", type=int, default=1,
                        help="Number of seeds to generate, each shifted back by --step hours (default: 1)")
    parser.add_argument("--step", type=int, default=1,
                        help="Hour shift between seeds (default: 1)")
    parser.add_argument("--marketdata", default=None,
                        help="Path to marketdata directory (default: <project_root>/marketdata)")
    parser.add_argument("--agents", default=None,
                        help="Optional agents file path (default: <project_root>/research/agents.txt)")
    parser.add_argument("--output-dir", default=None,
                        help="Directory for output seed files (default: seeds/)")
    parser.add_argument("--news", default=None,
                        help="Path to news directory (default: <project_root>/news)")
    args = parser.parse_args()

    marketdata_dir = resolve_path(args.marketdata or "marketdata")
    output_dir = resolve_path(args.output_dir or "seeds")
    news_dir = resolve_path(args.news or "news")
    os.makedirs(output_dir, exist_ok=True)

    end_dt = parse_hour(args.end_hour)
    agents = load_agents(args.agents)

    # Seeds: end_dt - (count-1)*step, ..., end_dt - step, end_dt
    seed_end_times = [
        end_dt - timedelta(hours=(args.count - 1 - i) * args.step)
        for i in range(args.count)
    ]

    print(f"gen_seed — marketdata CSV → seed_YYYY-MM-DDTHH.md")
    print(f"  Marketdata : {marketdata_dir}")
    print(f"  Output dir : {output_dir}")
    print(f"  Seeds      : {args.count}  (step={args.step}h, window={args.hours}h each)")
    print()

    for seed_end in seed_end_times:
        label = seed_end.strftime("%Y-%m-%dT%H")
        filename = f"seed_{label}.md"
        out_path = os.path.join(output_dir, filename)

        print(f"  Generating {label} ...", end=" ", flush=True)

        candles = load_ohlcv_window(marketdata_dir, seed_end, args.hours)
        liq = load_liq_window(marketdata_dir, seed_end, args.hours)
        news_content = load_news(news_dir, seed_end)

        content = "\n\n".join([
            format_chart_time(seed_end),
            format_btc_price(candles),
            format_ohlcv_json(candles),
            format_liquidations_json(liq),
            format_news(news_content),
            format_agents(agents),
        ]) + "\n"

        with open(out_path, "w") as f:
            f.write(content)

        print(f"{len(candles)} candles → {filename}")

    print("\nDone.")


if __name__ == "__main__":
    main()
