#!/usr/bin/env python3
"""Generate PNG chart and TXT report from a predictions CSV."""

import argparse
import json
import sys
from pathlib import Path

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


def parse_csv(path: Path):
    lines = path.read_text().splitlines()
    data_lines = []
    meta = {}

    # Split data rows from footer key,value lines
    header = None
    for line in lines:
        if not line.strip():
            continue
        parts = line.split(",")
        if header is None:
            header = line
            data_lines.append(line)
            continue
        # Footer: key=value or key,value lines where key is not a timestamp
        if "=" in line and "," not in line:
            k, _, v = line.partition("=")
            meta[k.strip()] = v.strip()
        elif len(parts) == 2 and not parts[0][0].isdigit():
            meta[parts[0].strip()] = parts[1].strip()
        else:
            data_lines.append(line)

    df = pd.read_csv(pd.io.common.StringIO("\n".join(data_lines)))
    df["latest_chart_time"] = pd.to_datetime(df["latest_chart_time"])
    df = df.sort_values("latest_chart_time").reset_index(drop=True)

    # Detect interval from consecutive timestamps
    if len(df) >= 2:
        deltas = df["latest_chart_time"].diff().dropna()
        interval = deltas.mode()[0]
    else:
        interval = pd.Timedelta(hours=1)

    df["candle_time"] = df["latest_chart_time"] + interval

    return df, meta, interval


def load_ohlcv(ohlcv_dir: Path) -> pd.DataFrame:
    rows = []
    for f in sorted(ohlcv_dir.glob("*-1h.json")):
        rows.extend(json.loads(f.read_text()))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"])
    return df.drop_duplicates("time").sort_values("time").reset_index(drop=True)


def draw_candle(ax, x, open_, close, low, high, color, width=0.35, alpha=1.0, label=None):
    body_low = min(open_, close)
    body_high = max(open_, close)
    ax.bar(x, body_high - body_low, bottom=body_low, width=width,
           color=color, alpha=alpha, zorder=3, label=label)
    ax.plot([x, x], [low, body_low], color=color, linewidth=1.2, alpha=alpha, zorder=3)
    ax.plot([x, x], [body_high, high], color=color, linewidth=1.2, alpha=alpha, zorder=3)


def plot_candles(df: pd.DataFrame, interval: pd.Timedelta, out_path: Path,
                 ohlcv: pd.DataFrame = None):
    fig, ax = plt.subplots(figsize=(min(24, max(12, len(df) * 0.35)), 6))

    # Build lookup from candle_time -> ohlc row
    ohlcv_map = {}
    if ohlcv is not None and not ohlcv.empty:
        ohlcv_map = {row["time"]: row for _, row in ohlcv.iterrows()}

    for i, row in df.iterrows():
        x = i
        ct = row["candle_time"]
        ohlc = ohlcv_map.get(ct)
        if ohlc is not None:
            o, c, lo, hi = ohlc["open"], ohlc["close"], ohlc["low"], ohlc["high"]
        else:
            o = c = (row["actual_low"] + row["actual_high"]) / 2
            lo, hi = row["actual_low"], row["actual_high"]

        candle_color = "#26A69A" if c >= o else "#EF5350"  # green if up, red if down
        draw_candle(ax, x, o, c, lo, hi,
                    color=candle_color, width=0.5,
                    label="Actual" if i == 0 else None)

        # Predicted overlaid on same x, lighter and slightly narrower
        pred_mid = (row["predicted_low"] + row["predicted_high"]) / 2
        draw_candle(ax, x, row["predicted_low"], row["predicted_high"],
                    row["predicted_low"], row["predicted_high"],
                    color="#FDD835", width=0.35, alpha=0.55,
                    label="Predicted" if i == 0 else None)

    tick_labels = [row["candle_time"].strftime("%m-%d %H:%M") for _, row in df.iterrows()]
    ax.set_xticks(range(len(df)))
    ax.set_xticklabels(tick_labels, rotation=60, ha="right", fontsize=6)

    ax.set_title(f"Predicted vs Actual Candles", fontsize=13, fontweight="bold")
    ax.set_ylabel("Price (USD)")
    ax.set_xlabel(f"Candle time")
    up_patch = mpatches.Patch(color="#26A69A", label="Actual (up)")
    dn_patch = mpatches.Patch(color="#EF5350", label="Actual (down)")
    pred_patch = mpatches.Patch(color="#FDD835", alpha=0.55, label="Predicted")
    ax.legend(handles=[up_patch, dn_patch, pred_patch], loc="upper right")
    ax.grid(axis="y", linestyle="--", alpha=0.4)

    plt.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"Chart saved: {out_path}")


def compute_report(df: pd.DataFrame, meta: dict, interval: pd.Timedelta, out_path: Path):
    errors_low = df["e_low"].dropna()
    errors_high = df["e_high"].dropna()
    ae = df["ae"].dropna()
    da = df["da"].dropna()

    def stats(series, name):
        return {
            "name": name,
            "count": len(series),
            "mean": series.mean(),
            "min": series.min(),
            "max": series.max(),
            "std": series.std(),
            "median": series.median(),
        }

    rows = [
        stats(ae, "Absolute Error (AE)"),
        stats(errors_low * 100, "Signed Rel Err Low %"),
        stats(errors_high * 100, "Signed Rel Err High %"),
    ]

    lines = []
    lines.append("=" * 60)
    lines.append("PREDICTION REPORT")
    lines.append(f"File: {out_path.stem}")
    lines.append(f"Candles: {len(df)}  |  Interval: {interval}")
    lines.append(f"Period: {df['candle_time'].min()} → {df['candle_time'].max()}")
    lines.append("=" * 60)

    lines.append("\n── Overall Metrics ──")
    lines.append(f"  MAE            : {meta.get('MAE', 'N/A')}")
    lines.append(f"  MDA            : {meta.get('MDA', 'N/A')}")
    lines.append(f"  Signed Rel Err Low  : {meta.get('b_low', 'N/A')}")
    lines.append(f"  Signed Rel Err High : {meta.get('b_high', 'N/A')}")
    lines.append(f"  Total Runtime  : {meta.get('total_runtime_mins', 'N/A')} min")

    avg_agents = df["agent_count"].mean() if "agent_count" in df.columns else "N/A"
    avg_rounds = df["simulation_rounds"].mean() if "simulation_rounds" in df.columns else "N/A"
    lines.append(f"  Avg Agents     : {avg_agents:.1f}" if isinstance(avg_agents, float) else f"  Avg Agents     : {avg_agents}")
    lines.append(f"  Avg Sim Rounds : {avg_rounds:.1f}" if isinstance(avg_rounds, float) else f"  Avg Sim Rounds : {avg_rounds}")

    da_pct = da.mean() * 100 if len(da) > 0 else 0
    lines.append(f"  Direction Acc  : {da_pct:.1f}%")

    lines.append("\n── Error Distribution ──")
    header = f"  {'Metric':<28} {'Mean':>10} {'Min':>10} {'Max':>10} {'Std':>10} {'Median':>10}"
    lines.append(header)
    lines.append("  " + "-" * 78)
    for s in rows:
        lines.append(
            f"  {s['name']:<28} {s['mean']:>10.4f} {s['min']:>10.4f} {s['max']:>10.4f} {s['std']:>10.4f} {s['median']:>10.4f}"
        )

    lines.append("\n── Per-Candle Detail ──")
    col_header = f"  {'Candle Time':<18} {'Pred Low':>10} {'Pred High':>10} {'Act Low':>10} {'Act High':>10} {'AE':>8} {'DA':>4}"
    lines.append(col_header)
    lines.append("  " + "-" * 76)
    for _, row in df.iterrows():
        lines.append(
            f"  {row['candle_time'].strftime('%Y-%m-%d %H:%M'):<18}"
            f" {row['predicted_low']:>10.1f} {row['predicted_high']:>10.1f}"
            f" {row['actual_low']:>10.1f} {row['actual_high']:>10.1f}"
            f" {row['ae']:>8.1f} {int(row['da']):>4}"
        )

    lines.append("\n" + "=" * 60)

    text = "\n".join(lines)
    out_path.write_text(text)
    print(f"Report saved: {out_path}")
    print(text)


def main():
    parser = argparse.ArgumentParser(description="Generate report from predictions CSV")
    parser.add_argument("csv", help="Path to predictions CSV file")
    parser.add_argument("--ohlcv-dir", default="research/data/ohlcv",
                        help="Directory containing *-1h.json OHLCV files")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"Error: {csv_path} not found", file=sys.stderr)
        sys.exit(1)

    stem = csv_path.stem
    parent = csv_path.parent
    png_path = parent / f"{stem}.png"
    txt_path = parent / f"{stem}.txt"

    df, meta, interval = parse_csv(csv_path)

    if df.empty:
        print("No data rows found.", file=sys.stderr)
        sys.exit(1)

    ohlcv = load_ohlcv(Path(args.ohlcv_dir))
    plot_candles(df, interval, png_path, ohlcv=ohlcv)
    compute_report(df, meta, interval, txt_path)


if __name__ == "__main__":
    main()
