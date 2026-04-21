# research/test_backtest_pipeline.py
import os
import sys
import json
import pytest
from unittest.mock import patch, call
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))


# ── ensure_ohlcv ─────────────────────────────────────────────────────────────

def test_ensure_ohlcv_skips_when_no_dates():
    """ensure_ohlcv does nothing when start/end are None."""
    from backtest_pipeline import ensure_ohlcv
    with patch("backtest_pipeline.subprocess.run") as mock_run:
        ensure_ohlcv("1h", None, None)
        mock_run.assert_not_called()


def test_ensure_ohlcv_skips_when_all_files_exist(tmp_path, monkeypatch):
    """ensure_ohlcv does not call aggregate when all JSON files are present."""
    from backtest_pipeline import ensure_ohlcv
    ohlcv_dir = tmp_path / "research" / "data" / "ohlcv"
    ohlcv_dir.mkdir(parents=True)
    (ohlcv_dir / "2026-04-02-1h.json").write_text("[]")
    (ohlcv_dir / "2026-04-03-1h.json").write_text("[]")

    import backtest_pipeline as bp
    monkeypatch.setattr(bp, "project_root", str(tmp_path))

    with patch("backtest_pipeline.subprocess.run") as mock_run:
        ensure_ohlcv("1h", "2026-04-02-00-00", "2026-04-03-23-00")
        mock_run.assert_not_called()


def test_ensure_ohlcv_calls_aggregate_for_missing(tmp_path, monkeypatch):
    """ensure_ohlcv calls aggregate_ohlcv.py when JSON files are missing."""
    from backtest_pipeline import ensure_ohlcv
    ohlcv_dir = tmp_path / "research" / "data" / "ohlcv"
    ohlcv_dir.mkdir(parents=True)
    # Only day 02 exists; day 03 is missing
    (ohlcv_dir / "2026-04-02-1h.json").write_text("[]")

    import backtest_pipeline as bp
    monkeypatch.setattr(bp, "project_root", str(tmp_path))

    with patch("backtest_pipeline.subprocess.run") as mock_run:
        ensure_ohlcv("1h", "2026-04-02-00-00", "2026-04-03-23-00")
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "--interval" in cmd
        assert "1h" in cmd
        assert "--startday" in cmd
        assert "--endday" in cmd
        startday_idx = cmd.index("--startday") + 1
        endday_idx = cmd.index("--endday") + 1
        assert cmd[startday_idx] == "2026-04-03"
        assert cmd[endday_idx] == "2026-04-03"


def test_ensure_ohlcv_calls_aggregate_for_stale_roll_step(tmp_path, monkeypatch):
    """ensure_ohlcv re-aggregates stale 4h files not using 1h rolling timestamps."""
    from backtest_pipeline import ensure_ohlcv

    ohlcv_dir = tmp_path / "research" / "data" / "ohlcv"
    ohlcv_dir.mkdir(parents=True)
    (ohlcv_dir / "2026-04-02-4h.json").write_text(
        '[{"time":"2026-04-02 00:00"},{"time":"2026-04-02 04:00"}]'
    )

    import backtest_pipeline as bp
    monkeypatch.setattr(bp, "project_root", str(tmp_path))

    with patch("backtest_pipeline.subprocess.run") as mock_run:
        ensure_ohlcv("4h", "2026-04-02-00-00", "2026-04-02-23-00")
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "--roll-step" in cmd
        assert "1h" in cmd


# ── merge_csvs ────────────────────────────────────────────────────────────────

def test_merge_csvs_combines_files_with_blank_line(tmp_path):
    """merge_csvs writes all temp files joined by a blank line."""
    from backtest_pipeline import merge_csvs
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    a.write_text("# interval=30m\nrow1\n")
    b.write_text("# interval=1h\nrow2\n")
    out = tmp_path / "out.csv"
    merge_csvs([str(a), str(b)], str(out))
    content = out.read_text()
    assert "# interval=30m" in content
    assert "# interval=1h" in content
    assert "\n\n" in content  # blank line separator


def test_merge_csvs_deletes_temp_files(tmp_path):
    """merge_csvs removes temp files after merging."""
    from backtest_pipeline import merge_csvs
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    a.write_text("section1\n")
    b.write_text("section2\n")
    out = tmp_path / "out.csv"
    merge_csvs([str(a), str(b)], str(out))
    assert not a.exists()
    assert not b.exists()


def test_merge_csvs_preserves_order(tmp_path):
    """merge_csvs writes sections in the order provided."""
    from backtest_pipeline import merge_csvs
    files = []
    for i, label in enumerate(["30m", "1h", "4h"]):
        f = tmp_path / f"{label}.csv"
        f.write_text(f"# interval={label}\n")
        files.append(str(f))
    out = tmp_path / "out.csv"
    merge_csvs(files, str(out))
    content = out.read_text()
    assert content.index("interval=30m") < content.index("interval=1h") < content.index("interval=4h")


# ── interval parsing ──────────────────────────────────────────────────────────

def test_parse_intervals_single():
    """Single INTERVAL value produces a one-element list."""
    from backtest_pipeline import parse_intervals
    assert parse_intervals("1h") == ["1h"]


def test_parse_intervals_multi():
    """Comma-separated INTERVAL produces a list in order."""
    from backtest_pipeline import parse_intervals
    assert parse_intervals("30m,1h,2h,4h") == ["30m", "1h", "2h", "4h"]


def test_parse_intervals_strips_spaces():
    """Spaces around commas are stripped."""
    from backtest_pipeline import parse_intervals
    assert parse_intervals("30m, 1h , 4h") == ["30m", "1h", "4h"]


def test_discover_seeds_filters_tweet_count(tmp_path):
    """discover_seeds should only include files matching tweet_count when provided."""
    from backtest_pipeline import discover_seeds

    seeds_dir = tmp_path / "seeds"
    seeds_dir.mkdir(parents=True)

    # Same timestamp/interval/limit/agents, different tweet_count.
    a = seeds_dir / "2026-04-04-01-00-1h-10-5-6.md"
    b = seeds_dir / "2026-04-04-01-00-1h-10-6-6.md"
    a.write_text("seed-a")
    b.write_text("seed-b")

    # Matching labels for both files.
    (seeds_dir / "2026-04-04-01-00-1h-label.json").write_text('{"low":1,"high":2}')

    out = discover_seeds(
        str(seeds_dir),
        interval="1h",
        limit=10,
        agent_count=6,
        tweet_count=5,
        start_dt=datetime(2026, 4, 4, 0, 0, tzinfo=timezone.utc),
        end_dt=datetime(2026, 4, 4, 2, 0, tzinfo=timezone.utc),
    )

    assert len(out) == 1
    assert out[0][0].endswith("2026-04-04-01-00-1h-10-5-6.md")
