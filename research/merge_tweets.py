#!/usr/bin/env python3
"""Merge scattered tweet JSON files in research/data/tweets/ into one per-day file sorted by createdAt."""

import json
import os
from collections import defaultdict

TWEETS_DIR = os.path.join(os.path.dirname(__file__), "data", "tweets")


def main():
    all_tweets = []
    source_files = []

    for fname in os.listdir(TWEETS_DIR):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(TWEETS_DIR, fname)
        with open(fpath, encoding="utf-8") as f:
            tweets = json.load(f)
        all_tweets.extend(tweets)
        source_files.append(fname)

    # Deduplicate by (userName, createdAt, text)
    seen = set()
    unique = []
    for t in all_tweets:
        key = (t.get("userName"), t.get("createdAt"), t.get("text"))
        if key not in seen:
            seen.add(key)
            unique.append(t)

    # Group by calendar day from createdAt (YYYY-MM-DD prefix)
    by_day = defaultdict(list)
    for t in unique:
        created = t.get("createdAt", "")
        day = created[:10]  # "YYYY-MM-DD"
        by_day[day].append(t)

    # Remove old scattered files
    for fname in source_files:
        os.remove(os.path.join(TWEETS_DIR, fname))

    # Write one file per day, sorted by createdAt
    for day, tweets in sorted(by_day.items()):
        tweets.sort(key=lambda t: t.get("createdAt", ""))
        out_path = os.path.join(TWEETS_DIR, f"{day}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(tweets, f, indent=2, ensure_ascii=False)
        print(f"Wrote {len(tweets)} tweets -> {out_path}")


if __name__ == "__main__":
    main()
