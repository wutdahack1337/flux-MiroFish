# research/test_pipeline.py
import os, sys
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime, timezone
import pytest


# --- Time logic ---

def test_interval_to_seconds_hours():
    from pipeline import interval_to_seconds
    assert interval_to_seconds("1h") == 3600

def test_interval_to_seconds_minutes():
    from pipeline import interval_to_seconds
    assert interval_to_seconds("30m") == 1800

def test_floor_to_interval_1h():
    from pipeline import floor_to_interval
    dt = datetime(2026, 4, 19, 6, 57, 0, tzinfo=timezone.utc)
    assert floor_to_interval(dt, 3600) == datetime(2026, 4, 19, 6, 0, 0, tzinfo=timezone.utc)

def test_floor_to_interval_30m():
    from pipeline import floor_to_interval
    dt = datetime(2026, 4, 19, 6, 45, 0, tzinfo=timezone.utc)
    assert floor_to_interval(dt, 1800) == datetime(2026, 4, 19, 6, 30, 0, tzinfo=timezone.utc)

def test_compute_latest_time_1h():
    from pipeline import compute_latest_time
    # 06:57 → floor=06:00 → minus 2h = 04:00
    now = datetime(2026, 4, 19, 6, 57, 0, tzinfo=timezone.utc)
    assert compute_latest_time("1h", now=now) == datetime(2026, 4, 19, 4, 0, 0, tzinfo=timezone.utc)

def test_compute_latest_time_30m():
    from pipeline import compute_latest_time
    # 06:45 → floor=06:30 → minus 1h = 05:30
    now = datetime(2026, 4, 19, 6, 45, 0, tzinfo=timezone.utc)
    assert compute_latest_time("30m", now=now) == datetime(2026, 4, 19, 5, 30, 0, tzinfo=timezone.utc)

def test_dt_to_filename():
    from pipeline import dt_to_filename
    dt = datetime(2026, 4, 19, 4, 0, 0, tzinfo=timezone.utc)
    assert dt_to_filename(dt) == "2026-04-19-04-00"


# --- Tweet time window ---

def test_build_query():
    from get_realtime_tweets import build_query
    result = build_query(["Alice", "Bob"], "BTC OR Bitcoin", 1000, 2000)
    assert result == "(BTC OR Bitcoin) (from:Alice OR from:Bob) since_time:1000 until_time:2000"

def test_tweet_time_window():
    from pipeline import tweet_time_window
    # latest=04:00, limit=4, interval=1h → since=01:00, until=05:00
    latest = datetime(2026, 4, 19, 4, 0, 0, tzinfo=timezone.utc)
    since, until = tweet_time_window(latest, limit=4, interval_secs=3600)
    assert since == int(datetime(2026, 4, 19, 1, 0, 0, tzinfo=timezone.utc).timestamp())
    assert until == int(datetime(2026, 4, 19, 5, 0, 0, tzinfo=timezone.utc).timestamp())


# --- Candle slicing ---

def test_split_candles_limit5():
    from pipeline import split_candles
    candles = [{"low": float(i), "high": float(i + 1)} for i in range(6)]
    seed, actual, prev_mid = split_candles(candles, limit=5)
    assert len(seed) == 5
    assert actual == candles[5]
    # prev_mid = candles[3] = (3+4)/2 = 3.5
    assert prev_mid == pytest.approx(3.5)

def test_split_candles_limit4():
    from pipeline import split_candles
    candles = [{"low": float(i), "high": float(i + 2)} for i in range(5)]
    seed, actual, prev_mid = split_candles(candles, limit=4)
    assert len(seed) == 4
    assert actual == candles[4]
    # prev_mid = candles[2] = (2+4)/2 = 3.0
    assert prev_mid == pytest.approx(3.0)


# --- CSV manipulation ---

def test_prepend_config(tmp_path):
    from pipeline import prepend_config
    csv_file = tmp_path / "out.csv"
    csv_file.write_text("header,col\nval,1\n")
    dt = datetime(2026, 4, 19, 4, 0, 0, tzinfo=timezone.utc)
    prepend_config(str(csv_file), dt, "1h", 4)
    content = csv_file.read_text()
    assert content.startswith("# latest_time=2026-04-19-04-00\n# interval=1h\n# limit=4\n")
    assert "header,col" in content

def test_insert_summary_before_b_lines(tmp_path):
    from pipeline import insert_summary_before_b_lines
    csv_file = tmp_path / "out.csv"
    csv_file.write_text("h1,h2\nval,1\n\nb_low,14.10bps\nb_high,5.74bps\n")
    insert_summary_before_b_lines(str(csv_file), total_runtime_mins=2.7, mae="179.9765", mda="1.000000")
    content = csv_file.read_text()
    lines = content.splitlines()
    b_low_idx   = next(i for i, l in enumerate(lines) if l.startswith("b_low"))
    summary_idx = next(i for i, l in enumerate(lines) if l.startswith("total_runtime_mins"))
    assert summary_idx < b_low_idx
    assert "total_runtime_mins,2.7" in content
    assert "MAE,179.9765" in content
    assert "MDA,1.000000" in content
