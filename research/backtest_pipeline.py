"""
backtest_pipeline — Run MiroFish predictions over research seeds in parallel, then score.

Usage:
    python3 research/backtest_pipeline.py
    python3 research/backtest_pipeline.py --seeds-dir research/seeds --rounds 5
    python3 research/backtest_pipeline.py --start 2026-04-02-00-00 --end 2026-04-07-23-00

Config (research/.env):
    INTERVAL=1h
    LIMIT=4
    ROUNDS=5
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

from common import project_root
from gen_seed import generate as gen_seeds

load_dotenv(os.path.join(project_root, "research", ".env"))

VENV_PYTHON = os.path.join(project_root, "backend", ".venv", "bin", "python3")
RUN_TRADE = os.path.join(project_root, "backend", "scripts", "run_trade.py")
CALC_METRICS = os.path.join(project_root, "backend", "scripts", "calc_metrics.py")
MAX_PARALLEL = 3


# ── Seed discovery ────────────────────────────────────────────────────────────

# Filename: YYYY-MM-DD-HH-MM-{interval}-{limit}-{tweet_count}-{agent_count}.md
_SEED_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}-\d{2}-\d{2})-([^-]+)-(\d+)-(\d+)-(\d+)\.md$"
)


def _parse_seed_filename(filename: str) -> Optional[tuple[datetime, str, int, int, int]]:
    """Return (seed_time, interval, limit, tweet_count, agent_count) or None."""
    m = _SEED_RE.match(filename)
    if not m:
        return None
    try:
        seed_time = datetime.strptime(m.group(1), "%Y-%m-%d-%H-%M").replace(tzinfo=timezone.utc)
        return seed_time, m.group(2), int(m.group(3)), int(m.group(4)), int(m.group(5))
    except ValueError:
        return None


def _parse_seed_time(filename: str) -> Optional[datetime]:
    parsed = _parse_seed_filename(filename)
    return parsed[0] if parsed else None


def _label_path(seed_path: str, interval: str) -> str:
    """Return label JSON path for a given seed file."""
    d = os.path.dirname(seed_path)
    ts_part = os.path.basename(seed_path)[:16]  # YYYY-MM-DD-HH-MM
    return os.path.join(d, f"{ts_part}-{interval}-label.json")


def discover_seeds(seeds_dir: str, interval: str, limit: int, agent_count: int,
                   start_dt: Optional[datetime] = None,
                   end_dt: Optional[datetime] = None) -> list[tuple[str, datetime]]:
    """Return sorted (seed_path, seed_time) filtered by date range, interval, limit, and agent_count."""
    results = []
    for fname in os.listdir(seeds_dir):
        parsed = _parse_seed_filename(fname)
        if parsed is None:
            continue
        seed_time, f_interval, f_limit, _tweets, f_agents = parsed
        if f_interval != interval:
            continue
        if f_limit != limit:
            continue
        if f_agents != agent_count:
            continue
        if start_dt and seed_time < start_dt:
            continue
        if end_dt and seed_time > end_dt:
            continue
        seed_path = os.path.join(seeds_dir, fname)
        if not os.path.exists(_label_path(seed_path, interval)):
            continue
        results.append((seed_path, seed_time))
    results.sort(key=lambda x: x[1])
    return results


# ── Actual candle lookup ──────────────────────────────────────────────────────

def _prev_mid_from_seed(seed_path: str) -> Optional[float]:
    """Return mid of the T-1 candle by reading the second-to-last entry in the seed's OHLCV section."""
    try:
        with open(seed_path, encoding="utf-8") as f:
            text = f.read()
        # Extract the JSON array from the # OHLCV section
        m = re.search(r"# OHLCV[^\n]*\n(\[.*?\])", text, re.DOTALL)
        if not m:
            return None
        candles = json.loads(m.group(1))
        if len(candles) < 2:
            return None
        c = candles[-2]  # T-1: second-to-last candle
        lo, hi = c.get("low"), c.get("high")
        if lo is not None and hi is not None:
            return (lo + hi) / 2
    except (json.JSONDecodeError, OSError):
        pass
    return None


def get_actuals(seed_path: str, interval: str) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Return (actual_low, actual_high, prev_mid) for a seed.

    actual_low/high: from the label JSON (= T+1 candle)
    prev_mid: mid of T-1 candle read directly from the seed's OHLCV section
    """
    actual_low: Optional[float] = None
    actual_high: Optional[float] = None

    label = _label_path(seed_path, interval)
    if os.path.exists(label):
        try:
            with open(label, encoding="utf-8") as f:
                c = json.load(f)
            actual_low = c.get("low")
            actual_high = c.get("high")
        except (json.JSONDecodeError, OSError):
            pass

    prev_mid = _prev_mid_from_seed(seed_path)
    return actual_low, actual_high, prev_mid


# ── Parallel runner ───────────────────────────────────────────────────────────

def launch(slot: int, seed_path: str, job_num: int, output_csv: str,
           rounds: int, interval: str,
           active_pids: list, active_meta: list) -> None:
    seed_time = _parse_seed_time(os.path.basename(seed_path))
    hour_label = seed_time.strftime("%Y-%m-%dT%H:%M") if seed_time else os.path.basename(seed_path)

    actual_low, actual_high, prev_mid = get_actuals(seed_path, interval)

    cmd = [VENV_PYTHON, RUN_TRADE, "-o", output_csv, "--rounds", str(rounds)]
    if actual_low is not None:
        cmd += ["--actual-low", str(actual_low)]
    if actual_high is not None:
        cmd += ["--actual-high", str(actual_high)]
    if prev_mid is not None:
        cmd += ["--prev-mid", str(prev_mid)]
    cmd.append(seed_path)

    print(f"[{job_num}] {hour_label} — running prediction...", end="", flush=True)

    proc = subprocess.Popen(cmd)
    active_pids[slot] = proc
    active_meta[slot] = (time.time(), hour_label, job_num)


def reap_one(active_pids: list, active_meta: list) -> int:
    """Wait until one slot finishes; return its slot index."""
    while True:
        for slot, proc in enumerate(active_pids):
            if proc is None:
                continue
            ret = proc.poll()
            if ret is not None:
                job_start, hour_label, job_num = active_meta[slot]
                mins = (time.time() - job_start) / 60
                status = "✓" if ret == 0 else "✗ (failed)"
                print(f"\n[{job_num}] {hour_label} — {status} ({mins:.1f} mins)")
                active_pids[slot] = None
                active_meta[slot] = None
                return slot
        time.sleep(0.5)


def run_pipeline(seeds: list[tuple[str, datetime]], output_csv: str, rounds: int,
                 interval: str) -> bool:
    active_pids: list = [None] * MAX_PARALLEL
    active_meta: list = [None] * MAX_PARALLEL
    all_ok = True

    for job_num, (seed_path, seed_time) in enumerate(seeds, 1):
        # Find a free slot
        free_slot = next((i for i, p in enumerate(active_pids) if p is None), -1)
        if free_slot == -1:
            free_slot = reap_one(active_pids, active_meta)

        launch(free_slot, seed_path, job_num, output_csv, rounds, interval,
               active_pids, active_meta)

    # Drain remaining
    while any(p is not None for p in active_pids):
        slot = reap_one(active_pids, active_meta)

    return all_ok


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Backtest MiroFish predictions over research seeds")
    parser.add_argument("--seeds-dir", default=None,
                        help="Directory containing seed files (default: research/seeds)")
    parser.add_argument("--output", default=None,
                        help="Output CSV path (default: price_predict/TIMESTAMP.csv)")
    parser.add_argument("--rounds", type=int,
                        default=int(os.getenv("ROUNDS", "5")),
                        help="Simulation rounds per seed (default: ROUNDS env or 5)")
    parser.add_argument("--interval", default=os.getenv("INTERVAL", "1h"),
                        help="Candle interval matching seed files (default: INTERVAL env or 1h)")
    parser.add_argument("--start", default=os.getenv("SEED_START"),
                        help="Filter seeds from YYYY-MM-DD-HH-MM (default: SEED_START env)")
    parser.add_argument("--end", default=os.getenv("SEED_END"),
                        help="Filter seeds until YYYY-MM-DD-HH-MM (default: SEED_END env)")
    args = parser.parse_args()

    if not os.path.isfile(VENV_PYTHON):
        print(f"Error: Python venv not found at {VENV_PYTHON}")
        sys.exit(1)

    seeds_dir = args.seeds_dir or os.path.join(project_root, "research", "seeds")
    if not os.path.isdir(seeds_dir):
        print(f"Error: seeds directory not found: {seeds_dir}")
        sys.exit(1)

    start_dt = (datetime.strptime(args.start, "%Y-%m-%d-%H-%M").replace(tzinfo=timezone.utc)
                if args.start else None)
    end_dt = (datetime.strptime(args.end, "%Y-%m-%d-%H-%M").replace(tzinfo=timezone.utc)
              if args.end else None)

    if args.start and args.end:
        print("Generating seeds...")
        gen_seeds(
            start=args.start,
            end=args.end,
            interval=args.interval,
            limit=int(os.getenv("LIMIT", "24")),
            tweet_limit=int(os.getenv("TWEET_LIMIT", "5")),
            agents_limit=int(os.getenv("AGENTS", "0")),
            output_dir=os.path.relpath(seeds_dir, project_root),
        )
        print()

    limit = int(os.getenv("LIMIT", "24"))
    agent_count = int(os.getenv("AGENTS", "0"))
    seeds = discover_seeds(seeds_dir, args.interval, limit, agent_count, start_dt, end_dt)
    if not seeds:
        print("No seed files found (with matching labels)")
        sys.exit(1)

    timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    day_dir = os.path.join(project_root, "research", "predictions", datetime.now().strftime("%Y-%m-%d"))
    os.makedirs(day_dir, exist_ok=True)
    output_csv = args.output or os.path.join(day_dir, f"{timestamp}.csv")

    print(f"Backtest | seeds={len(seeds)} | rounds={args.rounds} | interval={args.interval} | output={output_csv}")
    print()

    script_start = time.time()
    run_pipeline(seeds, output_csv, args.rounds, args.interval)

    if os.path.exists(output_csv):
        with open(output_csv, "r", encoding="utf-8") as f:
            original = f.read()
        config = (
            f"# interval={args.interval}\n"
            f"# limit={limit}\n"
            f"# rounds={args.rounds}\n"
            f"# tweet_limit={os.getenv('TWEET_LIMIT', '5')}\n"
            f"# agents={agent_count}\n"
            f"# start={args.start or 'all'}\n"
            f"# end={args.end or 'all'}\n"
        )
        with open(output_csv, "w", encoding="utf-8") as f:
            f.write(config + original)

    script_mins = (time.time() - script_start) / 60
    print()
    print("Scoring results...")

    result = subprocess.run(
        [VENV_PYTHON, CALC_METRICS, output_csv],
        capture_output=True, text=True
    )
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)

    mae_line = next((l for l in result.stdout.splitlines() if l.startswith("MAE=")), "")
    mda_line = next((l for l in result.stdout.splitlines() if l.startswith("MDA=")), "")
    mae = mae_line.split("=", 1)[1] if mae_line else "NA"
    mda = mda_line.split("=", 1)[1] if mda_line else "NA"

    with open(output_csv, "a", encoding="utf-8") as f:
        f.write(f"\ntotal_runtime_mins,{script_mins:.1f}\n")
        f.write(f"MAE,{mae}\n")
        f.write(f"MDA,{mda}\n")

    print(f"Total runtime: {script_mins:.1f} mins")


if __name__ == "__main__":
    main()
