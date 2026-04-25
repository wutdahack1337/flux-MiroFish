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
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv

from common import project_root
from gen_seed import generate as gen_seeds

load_dotenv(os.path.join(project_root, "research", ".env"))

VENV_PYTHON = os.path.join(project_root, "backend", ".venv", "bin", "python3")
RUN_TRADE = os.path.join(project_root, "backend", "scripts", "run_trade.py")
CALC_METRICS = os.path.join(project_root, "backend", "scripts", "calc_metrics.py")
AGGREGATE_OHLCV = os.path.join(project_root, "research", "aggregate_ohlcv.py")
MAX_PARALLEL = 4


def parse_intervals(interval_str: str) -> list[str]:
    intervals = [i.strip() for i in interval_str.split(",") if i.strip()]
    return intervals or ["1h"]


def _interval_to_timedelta(interval: str) -> timedelta:
    m = re.fullmatch(r"(\d+)([dhm])", interval.strip().lower())
    if not m:
        raise ValueError(f"Invalid interval '{interval}'. Use e.g. 1h, 30m, 1d, 2h")
    n = int(m.group(1))
    unit = m.group(2)
    if unit == "d":
        return timedelta(days=n)
    if unit == "h":
        return timedelta(hours=n)
    return timedelta(minutes=n)


def _predict_hours_from_interval(interval: str) -> int:
    """Convert interval to integer predict-hours for run_trade.py."""
    seconds = int(_interval_to_timedelta(interval).total_seconds())
    # Round up sub-hour intervals so run_trade always receives at least 1 hour.
    return max(1, (seconds + 3599) // 3600)


def _extract_ohlcv_step(path: str) -> Optional[timedelta]:
    """Return step between first two candle times, or None if unknown."""
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
        if not isinstance(rows, list) or len(rows) < 2:
            return None
        t0 = datetime.strptime(rows[0].get("time", ""), "%Y-%m-%d %H:%M")
        t1 = datetime.strptime(rows[1].get("time", ""), "%Y-%m-%d %H:%M")
        return t1 - t0
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        return None


def ensure_ohlcv(interval: str, start: Optional[str], end: Optional[str],
                 lookback_intervals: int = 0, tail_intervals: int = 0) -> None:
    """Run aggregate_ohlcv.py for any missing OHLCV JSON files in the date range."""
    if not start or not end:
        return

    ohlcv_dir = os.path.join(project_root, "research", "data", "ohlcv")
    interval_delta = _interval_to_timedelta(interval)
    expected_step = timedelta(hours=1) if interval_delta > timedelta(hours=1) else interval_delta
    start_dt = datetime.strptime(start, "%Y-%m-%d-%H-%M")
    end_dt = datetime.strptime(end, "%Y-%m-%d-%H-%M")

    # Match gen_seed data loading window: start - limit*delta ... end + delta
    start_d = (start_dt - interval_delta * lookback_intervals).date()
    end_d = (end_dt + interval_delta * tail_intervals).date()

    missing = []
    stale = []
    day = start_d
    while day <= end_d:
        fname = f"{day.strftime('%Y-%m-%d')}-{interval}.json"
        path = os.path.join(ohlcv_dir, fname)
        if not os.path.exists(path):
            missing.append(day)
        else:
            step = _extract_ohlcv_step(path)
            if step is not None and step != expected_step:
                stale.append(day)
        day += timedelta(days=1)

    days_to_refresh = sorted(set(missing + stale))
    if not days_to_refresh:
        return

    startday = min(days_to_refresh).strftime("%Y-%m-%d")
    endday = max(days_to_refresh).strftime("%Y-%m-%d")
    reason = "missing"
    if stale and missing:
        reason = "missing+stale"
    elif stale:
        reason = "stale"
    print(f"[ohlcv] {reason} {interval} data for {startday}->{endday}, aggregating...")
    cmd = [
        sys.executable,
        AGGREGATE_OHLCV,
        "--interval",
        interval,
        "--startday",
        startday,
        "--endday",
        endday,
    ]
    # For >1h intervals, keep candle length but roll timestamps hourly.
    if _interval_to_timedelta(interval) > timedelta(hours=1):
        cmd += ["--roll-step", "1h"]

    subprocess.run(
        cmd,
        check=True,
    )


def merge_csvs(temp_paths: list[str], output_csv: str) -> None:
    """Merge per-interval temp CSVs into one file, separated by blank lines."""
    with open(output_csv, "w", encoding="utf-8") as out:
        for i, path in enumerate(temp_paths):
            if i > 0:
                out.write("\n")
            with open(path, encoding="utf-8") as f:
                out.write(f.read())

    for path in temp_paths:
        try:
            os.unlink(path)
        except OSError:
            pass


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
                   tweet_count: Optional[int] = None,
                   start_dt: Optional[datetime] = None,
                   end_dt: Optional[datetime] = None) -> list[tuple[str, datetime]]:
    """Return sorted (seed_path, seed_time) filtered by date range, interval, limit, and agent_count."""
    results = []
    for root, _dirs, files in os.walk(seeds_dir):
        for fname in files:
            parsed = _parse_seed_filename(fname)
            if parsed is None:
                continue
            seed_time, f_interval, f_limit, _tweets, f_agents = parsed
            if f_interval != interval:
                continue
            if f_limit != limit:
                continue
            if tweet_count is not None and _tweets != tweet_count:
                continue
            if f_agents != agent_count:
                continue
            if start_dt and seed_time < start_dt:
                continue
            if end_dt and seed_time > end_dt:
                continue
            seed_path = os.path.join(root, fname)
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

    predict_hours = _predict_hours_from_interval(interval)
    cmd = [
        VENV_PYTHON,
        RUN_TRADE,
        "-o",
        output_csv,
        "--rounds",
        str(rounds),
        "--predict-hours",
        str(predict_hours),
    ]
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


def reap_one(active_pids: list, active_meta: list) -> tuple[int, int]:
    """Wait until one slot finishes; return (slot_index, return_code)."""
    while True:
        for slot, proc in enumerate(active_pids):
            if proc is None:
                continue
            ret = proc.poll()
            if ret is not None:
                proc.wait()  # reap zombie so OS releases the process entry
                job_start, hour_label, job_num = active_meta[slot]
                mins = (time.time() - job_start) / 60
                status = "✓" if ret == 0 else "✗ (failed)"
                print(f"\n[{job_num}] {hour_label} — {status} ({mins:.1f} mins)")
                active_pids[slot] = None
                active_meta[slot] = None
                return slot, ret
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
            free_slot, ret = reap_one(active_pids, active_meta)
            if ret != 0:
                all_ok = False

        launch(free_slot, seed_path, job_num, output_csv, rounds, interval,
               active_pids, active_meta)

    # Drain remaining
    while any(p is not None for p in active_pids):
        _slot, ret = reap_one(active_pids, active_meta)
        if ret != 0:
            all_ok = False

    return all_ok


def run_interval(interval: str, args, seeds_dir: str, output_csv: str) -> None:
    """Run the full pipeline for one interval."""
    limit = int(os.getenv("LIMIT", "24"))
    start_dt = (
        datetime.strptime(args.start, "%Y-%m-%d-%H-%M").replace(tzinfo=timezone.utc)
        if args.start
        else None
    )
    end_dt = (
        datetime.strptime(args.end, "%Y-%m-%d-%H-%M").replace(tzinfo=timezone.utc)
        if args.end
        else None
    )

    ensure_ohlcv(interval, args.start, args.end, lookback_intervals=limit, tail_intervals=1)

    if args.start and args.end:
        print(f"[{interval}] Generating seeds...")
        gen_seeds(
            start=args.start,
            end=args.end,
            interval=interval,
            limit=int(os.getenv("LIMIT", "24")),
            tweet_limit=int(os.getenv("TWEET_LIMIT", "5")),
            agents_limit=int(os.getenv("AGENTS", "0")),
            roll_step="1h",
            output_dir=os.path.relpath(seeds_dir, project_root),
        )
        print()

    agent_count = int(os.getenv("AGENTS", "0"))
    tweet_limit = int(os.getenv("TWEET_LIMIT", "5"))
    seeds = discover_seeds(
        seeds_dir,
        interval,
        limit,
        agent_count,
        tweet_count=tweet_limit,
        start_dt=start_dt,
        end_dt=end_dt,
    )
    if not seeds:
        print(f"[{interval}] No seed files found (with matching labels)")
        return

    print(f"[{interval}] seeds={len(seeds)} | rounds={args.rounds} | output={output_csv}")
    print()

    script_start = time.time()
    all_ok = run_pipeline(seeds, output_csv, args.rounds, interval)
    if not all_ok:
        print(f"[{interval}] one or more prediction jobs failed", file=sys.stderr)

    script_mins = (time.time() - script_start) / 60
    print()
    print(f"[{interval}] Scoring results...")

    result = None
    if os.path.exists(output_csv):
        result = subprocess.run(
            [VENV_PYTHON, CALC_METRICS, output_csv],
            capture_output=True,
            text=True,
        )
        print(result.stdout)
        if result.returncode != 0:
            print(result.stderr, file=sys.stderr)

    stdout = result.stdout if result else ""
    mae_line = next((l for l in stdout.splitlines() if l.startswith("MAE=")), "")
    mda_line = next((l for l in stdout.splitlines() if l.startswith("MDA=")), "")
    mae = mae_line.split("=", 1)[1] if mae_line else "NA"
    mda = mda_line.split("=", 1)[1] if mda_line else "NA"

    if os.path.exists(output_csv):
        with open(output_csv, "a", encoding="utf-8") as f:
            f.write(f"\ntotal_runtime_mins,{script_mins:.1f}\n")
            f.write(f"MAE,{mae}\n")
            f.write(f"MDA,{mda}\n")

        with open(output_csv, "r", encoding="utf-8") as f:
            original = f.read()
        config = (
            f"# interval={interval}\n"
            f"# limit={limit}\n"
            f"# rounds={args.rounds}\n"
            f"# tweet_limit={os.getenv('TWEET_LIMIT', '5')}\n"
            f"# agents={agent_count}\n"
            f"# start={args.start or 'all'}\n"
            f"# end={args.end or 'all'}\n"
        )
        with open(output_csv, "w", encoding="utf-8") as f:
            f.write(config + original)

    print(f"[{interval}] Total runtime: {script_mins:.1f} mins")


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
                        help="Candle interval(s), comma-separated: 1h or 30m,1h,4h")
    parser.add_argument("--start", default=os.getenv("SEED_START"),
                        help="Filter seeds from YYYY-MM-DD-HH-MM (default: SEED_START env)")
    parser.add_argument("--end", default=os.getenv("SEED_END"),
                        help="Filter seeds until YYYY-MM-DD-HH-MM (default: SEED_END env)")
    parser.add_argument("--single-interval", action="store_true",
                        help="Internal flag: run exactly one interval (used by multi-interval subprocesses)")
    args = parser.parse_args()

    if not os.path.isfile(VENV_PYTHON):
        print(f"Error: Python venv not found at {VENV_PYTHON}")
        sys.exit(1)

    seeds_dir = args.seeds_dir or os.path.join(project_root, "research", "seeds")
    if not os.path.isdir(seeds_dir):
        print(f"Error: seeds directory not found: {seeds_dir}")
        sys.exit(1)

    intervals = parse_intervals(args.interval)

    timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    day_dir = os.path.join(project_root, "research", "predictions", datetime.now().strftime("%Y-%m-%d"))
    os.makedirs(day_dir, exist_ok=True)
    output_csv = args.output or os.path.join(day_dir, f"{timestamp}.csv")

    if args.single_interval or len(intervals) == 1:
        run_interval(intervals[0], args, seeds_dir, output_csv)
        return

    import tempfile

    limit = int(os.getenv("LIMIT", "24"))
    for interval in intervals:
        ensure_ohlcv(interval, args.start, args.end, lookback_intervals=limit, tail_intervals=1)

    temp_files = []
    procs = []
    for interval in intervals:
        fd, tmp_path = tempfile.mkstemp(suffix=f"-{interval}.csv")
        os.close(fd)
        temp_files.append(tmp_path)

        cmd = [
            sys.executable,
            os.path.abspath(__file__),
            "--interval",
            interval,
            "--single-interval",
            "--output",
            tmp_path,
            "--rounds",
            str(args.rounds),
        ]
        if args.start:
            cmd += ["--start", args.start]
        if args.end:
            cmd += ["--end", args.end]
        if args.seeds_dir:
            cmd += ["--seeds-dir", args.seeds_dir]

        print(f"[multi] spawning interval={interval}...")
        procs.append(subprocess.Popen(cmd))

    all_ok = True
    for proc in procs:
        ret = proc.wait()
        if ret != 0:
            all_ok = False

    if not all_ok:
        print("[multi] one or more interval subprocesses failed", file=sys.stderr)
        for path in temp_files:
            try:
                os.unlink(path)
            except OSError:
                pass
        sys.exit(1)

    print(f"\n[multi] merging {len(intervals)} interval results -> {output_csv}")
    merge_csvs(temp_files, output_csv)
    print(f"[multi] done -> {output_csv}")


if __name__ == "__main__":
    main()
