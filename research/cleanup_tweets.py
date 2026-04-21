"""
Propose and optionally apply low-value tweet cleanup.

Default behavior is preview only:
    python3 research/cleanup_tweets.py

Apply deletions only after explicit confirmation:
    python3 research/cleanup_tweets.py --apply --confirm DELETE_TWEETS
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from common import project_root


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


@dataclass
class TweetRef:
    file_path: str
    index: int
    user_name: str
    text: str
    created_at: str
    like_count: int

    def key(self) -> tuple[str, str, str, int]:
        return (self.user_name, self.text, self.created_at, self.like_count)


def read_tweets(tweets_dir: str) -> list[TweetRef]:
    refs: list[TweetRef] = []
    for name in sorted(os.listdir(tweets_dir)):
        if not name.endswith(".json"):
            continue
        full_path = os.path.join(tweets_dir, name)
        with open(full_path, encoding="utf-8") as f:
            rows = json.load(f)
        for idx, row in enumerate(rows):
            refs.append(
                TweetRef(
                    file_path=full_path,
                    index=idx,
                    user_name=str(row.get("userName", "")),
                    text=str(row.get("text", "")).strip(),
                    created_at=str(row.get("createdAt", "")),
                    like_count=int(row.get("likeCount", 0) or 0),
                )
            )
    return refs


def build_candidates(refs: list[TweetRef]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    marked: set[tuple[str, int]] = set()

    by_text: dict[str, list[TweetRef]] = defaultdict(list)
    for ref in refs:
        ntext = normalize_text(ref.text)
        if ntext:
            by_text[ntext].append(ref)

    for ntext, items in by_text.items():
        if len(items) < 2:
            continue

        keep = sorted(
            items,
            key=lambda r: (-r.like_count, r.created_at, os.path.basename(r.file_path), r.index),
        )[0]

        for ref in items:
            if ref.file_path == keep.file_path and ref.index == keep.index:
                continue
            key = (ref.file_path, ref.index)
            if key in marked:
                continue
            marked.add(key)
            candidates.append(
                {
                    "file": os.path.basename(ref.file_path),
                    "index": ref.index,
                    "reason": "duplicate_exact_text",
                    "duplicate_count": len(items),
                    "userName": ref.user_name,
                    "createdAt": ref.created_at,
                    "likeCount": ref.like_count,
                    "text": ref.text,
                    "signature": {
                        "userName": ref.user_name,
                        "text": ref.text,
                        "createdAt": ref.created_at,
                        "likeCount": ref.like_count,
                    },
                }
            )

    low_info_phrases = {
        "read it all now:",
        "thank you",
        "thank you lucky",
    }
    promo_phrases = {
        "qastle wallet",
        "free pro pass to bitcoin 2026",
    }

    for ref in refs:
        key = (ref.file_path, ref.index)
        if key in marked:
            continue

        ntext = normalize_text(ref.text)
        reasons: list[str] = []

        if ntext in low_info_phrases:
            reasons.append("low_information_phrase")

        if any(p in ntext for p in promo_phrases) and ref.like_count <= 100:
            reasons.append("promo_content")

        if len(ntext) <= 15 and ref.like_count <= 5 and "bitcoin" not in ntext and "btc" not in ntext:
            reasons.append("very_short_low_signal")

        if not reasons:
            continue

        marked.add(key)
        candidates.append(
            {
                "file": os.path.basename(ref.file_path),
                "index": ref.index,
                "reason": ",".join(sorted(set(reasons))),
                "userName": ref.user_name,
                "createdAt": ref.created_at,
                "likeCount": ref.like_count,
                "text": ref.text,
                "signature": {
                    "userName": ref.user_name,
                    "text": ref.text,
                    "createdAt": ref.created_at,
                    "likeCount": ref.like_count,
                },
            }
        )

    candidates.sort(key=lambda c: (c["file"], c["index"]))
    return candidates


def save_proposal(path: str, candidates: list[dict[str, Any]]) -> None:
    payload = {
        "candidateCount": len(candidates),
        "reasonCounts": dict(Counter(c["reason"] for c in candidates)),
        "candidates": candidates,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def print_preview(candidates: list[dict[str, Any]], max_lines: int = 50) -> None:
    print(f"Candidates: {len(candidates)}")
    if not candidates:
        return

    reason_counts = Counter(c["reason"] for c in candidates)
    print("Reasons:")
    for reason, count in sorted(reason_counts.items(), key=lambda x: (-x[1], x[0])):
        print(f"  - {reason}: {count}")

    print("\nPreview:")
    for item in candidates[:max_lines]:
        text = item["text"].replace("\n", " ")
        if len(text) > 180:
            text = text[:177] + "..."
        print(
            f"  - {item['file']}#{item['index']} like={item['likeCount']} "
            f"reason={item['reason']} text={text}"
        )


def apply_deletions(tweets_dir: str, candidates: list[dict[str, Any]]) -> int:
    by_file: dict[str, Counter[tuple[str, str, str, int]]] = defaultdict(Counter)
    for c in candidates:
        sig = c["signature"]
        key = (
            str(sig.get("userName", "")),
            str(sig.get("text", "")),
            str(sig.get("createdAt", "")),
            int(sig.get("likeCount", 0) or 0),
        )
        by_file[c["file"]][key] += 1

    removed_total = 0
    for name, to_remove in sorted(by_file.items()):
        file_path = os.path.join(tweets_dir, name)
        if not os.path.exists(file_path):
            print(f"Skip missing file: {name}")
            continue

        with open(file_path, encoding="utf-8") as f:
            rows = json.load(f)

        new_rows = []
        removed = 0
        for row in rows:
            key = (
                str(row.get("userName", "")),
                str(row.get("text", "")).strip(),
                str(row.get("createdAt", "")),
                int(row.get("likeCount", 0) or 0),
            )
            if to_remove[key] > 0:
                to_remove[key] -= 1
                removed += 1
                continue
            new_rows.append(row)

        if removed > 0:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(new_rows, f, ensure_ascii=False, indent=2)
            print(f"{name}: removed {removed}, remaining {len(new_rows)}")
            removed_total += removed

    return removed_total


def main() -> None:
    parser = argparse.ArgumentParser(description="Propose or apply low-value tweet cleanup")
    parser.add_argument(
        "--tweets-dir",
        default=os.path.join(project_root, "research", "data", "tweets"),
        help="Directory containing daily tweet JSON files",
    )
    parser.add_argument(
        "--proposal-out",
        default=os.path.join(project_root, "research", "data", "tweets", "delete_candidates.json"),
        help="Path to save proposal JSON",
    )
    parser.add_argument("--apply", action="store_true", help="Apply deletions from proposal")
    parser.add_argument(
        "--confirm",
        default="",
        help="Confirmation token required with --apply",
    )
    parser.add_argument(
        "--confirm-token",
        default="DELETE_TWEETS",
        help="Required token value for --apply",
    )
    args = parser.parse_args()

    refs = read_tweets(args.tweets_dir)
    candidates = build_candidates(refs)
    save_proposal(args.proposal_out, candidates)
    print_preview(candidates)
    print(f"\nProposal saved: {args.proposal_out}")

    if not args.apply:
        print("Dry run only. Re-run with --apply --confirm <token> to delete.")
        return

    if args.confirm != args.confirm_token:
        print("Confirmation token mismatch. No files changed.")
        return

    removed = apply_deletions(args.tweets_dir, candidates)
    print(f"Done. Removed {removed} tweets.")


if __name__ == "__main__":
    main()