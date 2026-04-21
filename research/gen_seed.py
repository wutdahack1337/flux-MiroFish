"""
gen_seed — Generate seed (.md) + label (.json) pairs from OHLCV and tweet data.

Usage:
    python3 research/gen_seed.py --start 2026-04-02-00-00 --end 2026-04-07-23-00 --limit 24 --interval 1h
    python3 research/gen_seed.py --start 2026-04-02-00-00 --end 2026-04-07-23-00 --limit 48 --interval 30m

Outputs per candle t:
  seeds/YYYY-MM-DD-HH-MM-{interval}-{limit}-tweet-agent.md   — candles up to t + tweets
  seeds/YYYY-MM-DD-HH-MM-{interval}-{limit}-tweet-agent-label.json — t+1 candle (the label)
"""

import argparse
import json
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from common import project_root

load_dotenv()


def load_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_agents(agents_path=None) -> list[dict]:
    if agents_path and os.path.exists(agents_path):
        with open(agents_path, encoding="utf-8") as f:
            return json.load(f)
    default = os.path.join(project_root, "research", "agents.json")
    if os.path.exists(default):
        with open(default, encoding="utf-8") as f:
            return json.load(f)
    return []


def parse_interval(interval: str) -> tuple[timedelta, str]:
    """Parse '1h', '30m', '2h', '1d' → (timedelta, suffix_for_filename)."""
    import re
    m = re.fullmatch(r"(\d+)([dhm])", interval.strip().lower())
    if not m:
        raise ValueError(f"Invalid interval '{interval}'. Use e.g. 1h, 30m, 1d, 2h")
    n, unit = int(m.group(1)), m.group(2)
    if unit == "h":
        return timedelta(hours=n), interval
    elif unit == "d":
        return timedelta(days=n), interval
    elif unit == "m":
        return timedelta(minutes=n), interval
    raise ValueError(f"Unknown unit '{unit}'")


def floor_to_interval(dt: datetime, delta: timedelta) -> datetime:
    """Floor dt down to the nearest delta boundary in UTC."""
    seconds = int(delta.total_seconds())
    floored = int(dt.timestamp()) // seconds * seconds
    return datetime.fromtimestamp(floored, tz=timezone.utc)


def load_ohlcv_for_date(date_str: str, interval: str) -> list[dict]:
    path = os.path.join(project_root, "research", "data", "ohlcv",
                        f"{date_str}-{interval}.json")
    if not os.path.exists(path):
        return []
    return load_json(path)


def load_tweets_for_day(day_str: str) -> list[dict]:
    path = os.path.join(project_root, "research", "data", "tweets", f"{day_str}.json")
    if not os.path.exists(path):
        return []
    return load_json(path)


def format_ohlcv(candles: list[dict], interval: str) -> str:
    items = [json.dumps(c, separators=(',', ': ')) for c in candles]
    return f"# OHLCV {interval.upper()}\n[\n  " + ",\n  ".join(items) + "\n]"


def format_tweets(tweets: list[dict], up_to: datetime, tweet_limit: int) -> str:
    cutoff = up_to.strftime("%Y-%m-%dT%H:%M")
    filtered = [t for t in tweets if t.get("createdAt", "") <= cutoff]
    filtered = sorted(filtered, key=lambda t: t.get("createdAt", ""), reverse=True)[:tweet_limit]
    filtered = sorted(filtered, key=lambda t: t.get("createdAt", ""))
    items = [
        {"tweet": t["text"], "createdAt": t.get("createdAt", "")}
        for t in filtered
    ]
    return "# X Tweets\n" + json.dumps(items, ensure_ascii=False, indent=2)


def format_agents(agents: list[dict]) -> str:
    return "# Agents Population\n" + json.dumps(agents, ensure_ascii=False, indent=2)


def generate(
    start: str,
    end: str,
    interval: str = "1h",
    limit: int = 24,
    agents_path: str = None,
    output_dir: str = None,
    tweet_limit: int = 5,
    agents_limit: int = 0,
    roll_step: str = None,
) -> int:
    """Generate seed/label pairs. Returns count of pairs written."""
    start_dt = datetime.strptime(start, "%Y-%m-%d-%H-%M").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(end, "%Y-%m-%d-%H-%M").replace(tzinfo=timezone.utc)
    delta, interval_label = parse_interval(interval)
    step_delta = parse_interval(roll_step)[0] if roll_step else delta

    out_dir = os.path.join(project_root, output_dir or os.path.join("research", "seeds"))
    os.makedirs(out_dir, exist_ok=True)

    agents = load_agents(agents_path)
    if agents_limit > 0:
        agents = agents[:agents_limit]

    candle_map: dict[str, dict] = {}
    cur = start_dt - delta * limit
    end_load = end_dt + delta
    loaded_dates: set[str] = set()
    while cur <= end_load:
        date_str = cur.strftime("%Y-%m-%d")
        if date_str not in loaded_dates:
            for c in load_ohlcv_for_date(date_str, interval_label):
                candle_map[c["time"]] = c
            loaded_dates.add(date_str)
        cur += delta

    tweet_cache: dict[str, list] = {}
    count = 0

    cur = start_dt
    while cur <= end_dt:
        latest_dt = floor_to_interval(cur, step_delta)
        time_str = latest_dt.strftime("%Y-%m-%d %H:%M")
        t1_str = (latest_dt + step_delta).strftime("%Y-%m-%d %H:%M")

        if time_str not in candle_map or t1_str not in candle_map:
            cur += step_delta
            continue

        # Keep candle interval spacing (e.g. 4h) while rolling seed timestamps (e.g. 1h).
        needed_times = [
            (latest_dt - delta * i).strftime("%Y-%m-%d %H:%M")
            for i in range(limit - 1, -1, -1)
        ]
        if any(t not in candle_map for t in needed_times):
            cur += step_delta
            continue

        window = [candle_map[t] for t in needed_times]

        earliest_day = datetime.strptime(min(t["time"][:10] for t in window), "%Y-%m-%d") - timedelta(days=1)
        window_days = sorted({t["time"][:10] for t in window} | {cur.strftime("%Y-%m-%d")} | {earliest_day.strftime("%Y-%m-%d")})
        for d in window_days:
            if d not in tweet_cache:
                tweet_cache[d] = load_tweets_for_day(d)
        day_tweets = [t for d in window_days for t in tweet_cache[d]]

        cutoff = cur.strftime("%Y-%m-%dT%H:%M")
        tweet_count = min(len([t for t in day_tweets if t.get("createdAt", "") <= cutoff]), tweet_limit)
        agent_count = len(agents)
        day_dir = os.path.join(out_dir, cur.strftime("%Y-%m-%d"))
        os.makedirs(day_dir, exist_ok=True)
        fname_base = f"{cur.strftime('%Y-%m-%d-%H-%M')}-{interval_label}-{limit}-{tweet_count}-{agent_count}"
        seed_path = os.path.join(day_dir, f"{fname_base}.md")
        label_path = os.path.join(day_dir, f"{cur.strftime('%Y-%m-%d-%H-%M')}-{interval_label}-label.json")

        seed_content = "\n\n".join([
            f"# Latest Chart Time\n{cur.strftime('%Y-%m-%d %H:%M')}",
            f"# Latest BTC Price\n{candle_map[time_str]['close']}",
            format_ohlcv(window, interval_label),
            format_tweets(day_tweets, cur, tweet_limit),
            format_agents(agents),
        ]) + "\n"

        with open(seed_path, "w", encoding="utf-8") as f:
            f.write(seed_content)

        with open(label_path, "w", encoding="utf-8") as f:
            json.dump(candle_map[t1_str], f, ensure_ascii=False, indent=2)

        count += 1
        cur += step_delta

    print(f"Generated {count} seed/label pairs → {out_dir}")
    return count


def main():
    parser = argparse.ArgumentParser(description="Generate seed/label pairs from OHLCV + tweets")
    parser.add_argument("--start", default=os.getenv("SEED_START"), help="Start candle time YYYY-MM-DD-HH-MM")
    parser.add_argument("--end", default=os.getenv("SEED_END"), help="End candle time YYYY-MM-DD-HH-MM")
    parser.add_argument("--limit", type=int, default=int(os.getenv("LIMIT", "24")), help="Candles per seed window")
    parser.add_argument("--interval", default=os.getenv("INTERVAL", "1h"), help="Candle interval e.g. 1h, 30m, 1d, 2h")
    parser.add_argument("--roll-step", default=None,
                        help="Seed rolling step e.g. 1h. Default follows --interval")
    parser.add_argument("--agents", default=None, help="Path to agents JSON")
    parser.add_argument("--output-dir", default=None, help="Output directory (default: research/seeds)")
    args = parser.parse_args()

    if not args.start or not args.end:
        raise ValueError("--start and --end (or SEED_START/SEED_END in .env) are required")

    generate(
        start=args.start,
        end=args.end,
        interval=args.interval,
        limit=args.limit,
        agents_path=args.agents,
        output_dir=args.output_dir,
        tweet_limit=int(os.getenv("TWEET_LIMIT", "5")),
        agents_limit=int(os.getenv("AGENTS", "0")),
        roll_step=args.roll_step,
    )


if __name__ == "__main__":
    main()
