"""
fetch_tweets — Fetch top tweets per day via twitterapi.io
Writes output to research/data/tweets/YYYY-MM-DD.json, merging and deduplicating.

Usage:
    python3 research/fetch_tweets.py --startday 2026-04-01 --endday 2026-04-07
    python3 research/fetch_tweets.py --latest-time 2026-04-19-04-00
    python3 research/fetch_tweets.py  # fetches current interval window
"""

import argparse
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import time

import requests
from dotenv import load_dotenv

from common import project_root

load_dotenv()

API_KEY  = os.getenv("TWEET_API_KEY", "")
BASE_URL = os.getenv("TWEET_BASE_URL", "https://api.twitterapi.io/twitter/tweet/advanced_search")
ACCOUNTS = [a.strip() for a in os.getenv("ACCOUNTS", "").split(",") if a.strip()]
QUERY    = os.getenv("QUERY", "")


def build_query(accounts: list[str], query: str, since_time: int, until_time: int) -> str:
    from_clause = " OR ".join(f"from:{a}" for a in accounts)
    return f"({query}) ({from_clause}) since_time:{since_time} until_time:{until_time}"


def fetch_tweets(full_query: str) -> list[dict]:
    headers = {"X-API-Key": API_KEY}
    params  = {"queryType": "Top", "query": full_query}
    response = requests.get(BASE_URL, headers=headers, params=params, timeout=30)
    if response.status_code in (402, 429):
        print("  rate limited, skipping")
        return []
    response.raise_for_status()
    batch = response.json().get("tweets", [])
    print(f"  fetched {len(batch)} tweets")
    return batch


def clean_text(text: str) -> str:
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'[\U0001F000-\U0001FFFF]', '', text)
    text = re.sub(r'[\u2600-\u27BF]', '', text)
    text = re.sub(r'\uFE0F', '', text)
    text = re.sub(r'@[\w]+', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def parse_created_at(raw: str) -> str:
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


def merge_into_day_file(tweets: list[dict], out_dir: str, day: str) -> None:
    out_path = os.path.join(out_dir, f"{day}.json")
    existing = []
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as f:
            existing = json.load(f)

    seen = {(t.get("userName"), t.get("createdAt"), t.get("text")) for t in existing}
    added = 0
    for t in tweets:
        key = (t.get("userName"), t.get("createdAt"), t.get("text"))
        if key not in seen:
            seen.add(key)
            existing.append(t)
            added += 1

    existing.sort(key=lambda t: t.get("createdAt", ""))
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    print(f"  {day}.json — {len(existing)} tweets (+{added} new)")


def fetch_day(day: datetime.date, out_dir: str) -> None:
    since_dt = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    until_dt = since_dt + timedelta(days=1)
    since_time = int(since_dt.timestamp())
    until_time = int(until_dt.timestamp())

    date_str = day.strftime("%Y-%m-%d")
    print(f"{date_str} (since={since_time} until={until_time})")

    full_query = build_query(ACCOUNTS, QUERY, since_time, until_time)
    tweets_raw = fetch_tweets(full_query)
    tweets = [extract(t) for t in tweets_raw]
    merge_into_day_file(tweets, out_dir, date_str)


def main():
    parser = argparse.ArgumentParser(description="Fetch top tweets per day")
    parser.add_argument("--startday", default=None, help="Start date YYYY-MM-DD (inclusive)")
    parser.add_argument("--endday", default=None, help="End date YYYY-MM-DD (inclusive)")
    parser.add_argument("--latest-time", default=None,
                        help="Single window mode: latest candle time YYYY-MM-DD-HH-MM")
    args = parser.parse_args()

    out_dir = os.path.join(project_root, "research", "data", "tweets")
    os.makedirs(out_dir, exist_ok=True)

    if args.startday:
        start = datetime.strptime(args.startday, "%Y-%m-%d").date()
        end   = datetime.strptime(args.endday, "%Y-%m-%d").date() if args.endday else start
        day = start
        while day <= end:
            fetch_day(day, out_dir)
            day += timedelta(days=1)
            if day <= end:
                time.sleep(3)
    else:
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

        since_time = int(latest_time.timestamp())
        until_time = int((latest_time + timedelta(seconds=interval_secs)).timestamp())

        print(f"Fetching window since={since_time} until={until_time}...")
        full_query = build_query(ACCOUNTS, QUERY, since_time, until_time)
        tweets_raw = fetch_tweets(full_query)
        tweets = [extract(t) for t in tweets_raw]

        by_day = defaultdict(list)
        for t in tweets:
            day = t.get("createdAt", "")[:10]
            if day:
                by_day[day].append(t)
        for day, day_tweets in sorted(by_day.items()):
            merge_into_day_file(day_tweets, out_dir, day)


if __name__ == "__main__":
    main()
