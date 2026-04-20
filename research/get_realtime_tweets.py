"""
get_realtime_tweets — Fetch top tweets for a query via twitterapi.io
Writes output to tweets/YYYY-MM-DD-HH-MM.json under research/.
Interval configured via env INTERVAL (default: 1h).

Usage:
    python3 research/get_realtime_tweets.py --latest-time 2026-04-19-04-00
    python3 research/get_realtime_tweets.py  # uses defaults
"""

import argparse
import json
import os
import re
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

from common import project_root

load_dotenv()

API_KEY  = os.getenv("TWEET_API_KEY", "")
BASE_URL = os.getenv("TWEET_BASE_URL", "https://api.twitterapi.io/twitter/tweet/advanced_search")

ACCOUNTS = [a.strip() for a in os.getenv("ACCOUNTS", "").split(",") if a.strip()]
QUERY    = os.getenv("QUERY", "")


def build_query(accounts: list[str], query: str, until_time: int) -> str:
    from_clause = " OR ".join(f"from:{a}" for a in accounts)
    return f"({query}) ({from_clause}) until_time:{until_time}"


def fetch_tweets(full_query: str) -> list[dict]:
    headers = {"X-API-Key": API_KEY}
    params  = {"queryType": "Latest", "query": full_query}
    response = requests.get(BASE_URL, headers=headers, params=params, timeout=30)
    response.raise_for_status()
    return response.json().get("tweets", [])


def clean_text(text: str) -> str:
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'[\U0001F000-\U0001FFFF]', '', text)  # all emoji/symbols blocks
    text = re.sub(r'[\u2600-\u27BF]', '', text)           # misc symbols, dingbats
    text = re.sub(r'\uFE0F', '', text)                     # variation selectors
    text = re.sub(r'@[\w]+', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def parse_created_at(raw: str) -> str:
    """Convert Twitter createdAt 'Wed Apr 08 10:18:04 +0000 2026' → '2026-04-08T10:00'."""
    try:
        dt = datetime.strptime(raw, "%a %b %d %H:%M:%S +0000 %Y").replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M")
    except (ValueError, TypeError):
        return ""


def extract(tweet: dict) -> dict:
    return {
        "userName":  tweet.get("author", {}).get("userName", ""),
        "text":      clean_text(tweet.get("text", "")),
        "likeCount": tweet.get("likeCount", 0),
        "createdAt": parse_created_at(tweet.get("createdAt", "")),
    }


def main():
    parser = argparse.ArgumentParser(description="Fetch top tweets for a time window")
    parser.add_argument("--latest-time", default=None,
                        help="Latest candle time YYYY-MM-DD-HH-MM (default: now)")
    args = parser.parse_args()

    if args.latest_time:
        latest_time = datetime.strptime(args.latest_time, "%Y-%m-%d-%H-%M").replace(tzinfo=timezone.utc)
    else:
        latest_time = datetime.now(timezone.utc)

    interval = os.getenv("INTERVAL", "1h")
    if interval.endswith("h"):
        interval_secs = int(interval[:-1]) * 3600
    elif interval.endswith("m"):
        interval_secs = int(interval[:-1]) * 60
    else:
        raise ValueError(f"Unknown interval: {interval}")

    until_time = int((latest_time + timedelta(seconds=interval_secs)).timestamp())

    full_query = build_query(ACCOUNTS, QUERY, until_time)
    tweets_raw = fetch_tweets(full_query)
    tweets     = [extract(t) for t in tweets_raw]

    out_dir  = os.path.join(project_root, "research", "data", "tweets")
    os.makedirs(out_dir, exist_ok=True)
    filename = latest_time.strftime("%Y-%m-%d-%H-%M") + ".json"
    out_path = os.path.join(out_dir, filename)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(tweets, f, ensure_ascii=False, indent=2)

    print(f"Wrote {len(tweets)} tweets (until: {until_time}) → {out_path}")


if __name__ == "__main__":
    main()
