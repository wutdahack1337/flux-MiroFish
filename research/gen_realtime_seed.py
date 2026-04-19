"""
gen_realtime_seed — ohlcv/YYYY-MM-DD-HH-MM.json + tweets/YYYY-MM-DD.json → seeds/YYYY-MM-DD-HH-MM.md

Usage:
    python3 backend/scripts/gen_realtime_seed.py --ohlcv ohlcv/2026-04-16-14-00.json --tweets tweets/2026-04-16.json
    python3 backend/scripts/gen_realtime_seed.py  # auto-resolves latest ohlcv and today's tweets
"""

import argparse
import json
import os
from datetime import datetime, timezone

from common import project_root


def load_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def latest_file(directory: str, ext: str) -> str | None:
    """Return the most recently modified file with the given extension in directory."""
    if not os.path.isdir(directory):
        return None
    files = [
        os.path.join(directory, f)
        for f in os.listdir(directory)
        if f.endswith(ext)
    ]
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def load_agents(agents_path: str | None = None) -> str:
    if agents_path and os.path.exists(agents_path):
        with open(agents_path) as f:
            return f.read().strip()
    default = os.path.join(project_root, "agents.txt")
    if os.path.exists(default):
        with open(default) as f:
            return f.read().strip()
    return ""


def format_ohlcv(candles: list[dict]) -> str:
    items = [json.dumps(c, separators=(',', ': ')) for c in candles]
    return "# OHLCV 1H\n[\n  " + ",\n  ".join(items) + "\n]"


def format_tweets(tweets: list[dict]) -> str:
    sorted_tweets = sorted(tweets, key=lambda t: t.get("createdAt", ""), reverse=True)[:5]
    sorted_tweets = sorted(sorted_tweets, key=lambda t: t.get("createdAt", ""))
    items = [
        {"userName": t["userName"], "tweet": t["text"], "createdAt": t.get("createdAt", "")}
        for t in sorted_tweets
    ]
    return "# X Tweets\n" + json.dumps(items, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(description="gen_realtime_seed — build seed from live OHLCV + tweets")
    parser.add_argument("--ohlcv", default=None,
                        help="Path to ohlcv JSON file (default: latest in ohlcv/)")
    parser.add_argument("--tweets", default=None,
                        help="Path to tweets JSON file (default: today's in tweets/)")
    parser.add_argument("--agents", default=None,
                        help="Path to agents file (default: agents.txt)")
    parser.add_argument("--output-dir", default=None,
                        help="Output directory (default: seeds/)")
    args = parser.parse_args()

    # Resolve ohlcv file
    ohlcv_path = args.ohlcv
    if not ohlcv_path:
        ohlcv_path = latest_file(os.path.join(project_root, "ohlcv"), ".json")
    if not ohlcv_path or not os.path.exists(ohlcv_path):
        raise FileNotFoundError(f"No OHLCV file found. Run get_ohlcv.py first or pass --ohlcv.")
    ohlcv_path = os.path.join(project_root, ohlcv_path) if not os.path.isabs(ohlcv_path) else ohlcv_path

    # Resolve tweets file
    tweets_path = args.tweets
    if not tweets_path:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        tweets_path = os.path.join(project_root, "tweets", f"{today}.json")
    if not os.path.exists(tweets_path):
        raise FileNotFoundError(f"No tweets file found at {tweets_path}. Run get_tweets.py first or pass --tweets.")
    tweets_path = os.path.join(project_root, tweets_path) if not os.path.isabs(tweets_path) else tweets_path

    candles = load_json(ohlcv_path)
    tweets = load_json(tweets_path)

    # candles is a list; latest is last entry
    latest = candles[-1]
    chart_time = latest["time"]
    latest_price = latest["close"]

    # Filename from chart time: YYYY-MM-DD-HH-MM
    dt = datetime.strptime(chart_time, "%Y-%m-%d %H:%M")
    filename = dt.strftime("%Y-%m-%d-%H-%M") + ".md"

    output_dir = os.path.join(project_root, args.output_dir or "seeds")
    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, filename)

    agents_text = load_agents(args.agents)

    content = "\n\n".join([
        f"# Latest Chart Time\n{chart_time}",
        f"# Latest BTC Price\n{latest_price}",
        format_ohlcv(candles),
        format_tweets(tweets),
        "# Agents Population\n" + agents_text,
    ]) + "\n"

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"Wrote {len(candles)} candles + {len(tweets)} tweets → {out_path}")


if __name__ == "__main__":
    main()
