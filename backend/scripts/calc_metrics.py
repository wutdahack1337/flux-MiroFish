#!/usr/bin/env python3
"""Calculate AE and DA metrics from a forecast CSV.

Metrics per row:
  AE     = |predicted_low - actual_low| + |predicted_high - actual_high|
  DA     = 1 if sign(actual_mid - prev_mid) == sign(predicted_mid - prev_mid) else 0
           (skipped when prev_mid is missing)
  e_low  = (predicted_low  - actual_low)  / actual_low
  e_high = (predicted_high - actual_high) / actual_high

Summary (appended to CSV):
  MAE    = mean AE across rows with full actual data
  MDA    = mean DA across rows with prev_mid available
  b_low  = mean(e_low) as %
  b_high = mean(e_high) as %
"""

import argparse
import csv
import os
import sys


def parse_float(value):
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def sign(x):
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0


def evaluate_row(row):
    """Return (ae, da) for a row. Either may be None if data is missing."""
    predicted_low = parse_float(row.get("predicted_low"))
    predicted_high = parse_float(row.get("predicted_high"))
    actual_low = parse_float(row.get("actual_low"))
    actual_high = parse_float(row.get("actual_high"))
    prev_mid = parse_float(row.get("prev_mid"))

    ae = None
    e_low = None
    e_high = None
    if None not in (predicted_low, predicted_high, actual_low, actual_high):
        ae = abs(predicted_low - actual_low) + abs(predicted_high - actual_high)
        if actual_low != 0:
            e_low = (predicted_low - actual_low) / actual_low
        if actual_high != 0:
            e_high = (predicted_high - actual_high) / actual_high

    da = None
    if None not in (predicted_low, predicted_high, actual_low, actual_high, prev_mid):
        actual_mid = (actual_low + actual_high) / 2
        predicted_mid = (predicted_low + predicted_high) / 2
        da = 1 if sign(actual_mid - prev_mid) == sign(predicted_mid - prev_mid) else 0

    return ae, da, e_low, e_high


def main():
    parser = argparse.ArgumentParser(description="Calculate AE/DA metrics from CSV")
    parser.add_argument("csv_file", help="Path to forecast CSV")
    args = parser.parse_args()

    if not os.path.exists(args.csv_file):
        print(f"Error: CSV file not found: {args.csv_file}")
        sys.exit(1)

    with open(args.csv_file, newline="", encoding="utf-8") as f:
        raw_lines = f.readlines()

    if not raw_lines:
        print("No rows found in CSV")
        sys.exit(1)

    header_line = raw_lines[0]
    fieldnames = [f.strip() for f in header_line.split(",")]

    # Separate data rows from trailing summary lines
    data_lines = []
    summary_lines = []
    for line in raw_lines[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        cols = stripped.split(",")
        if len(cols) == len(fieldnames):
            data_lines.append(line)
        else:
            summary_lines.append(line)

    rows = list(csv.DictReader(data_lines, fieldnames=fieldnames))

    if not rows:
        print("No data rows found in CSV")
        sys.exit(1)

    ae_values = []
    da_values = []
    e_low_values = []
    e_high_values = []
    results = []

    for row in rows:
        ae, da, e_low, e_high = evaluate_row(row)
        results.append((row, ae, da, e_low, e_high))
        if ae is not None:
            ae_values.append(ae)
        if da is not None:
            da_values.append(da)
        if e_low is not None:
            e_low_values.append(e_low)
        if e_high is not None:
            e_high_values.append(e_high)

    mae = sum(ae_values) / len(ae_values) if ae_values else None
    mda = sum(da_values) / len(da_values) if da_values else None
    b_low = sum(e_low_values) / len(e_low_values) * 100 if e_low_values else None  # %
    b_high = sum(e_high_values) / len(e_high_values) * 100 if e_high_values else None  # %

    # Build output fieldnames: insert metrics after 'runtime' if present, else append
    out_fields = list(fieldnames)
    to_add = [m for m in ("ae", "da", "e_low", "e_high") if m not in out_fields]
    if to_add:
        if "runtime" in out_fields:
            idx = out_fields.index("runtime") + 1
            for i, metric in enumerate(to_add):
                out_fields.insert(idx + i, metric)
        else:
            out_fields.extend(to_add)

    # Write updated CSV with ae/da columns and cleaned summary lines
    with open(args.csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
        writer.writeheader()
        for row, ae, da, e_low, e_high in results:
            row["ae"] = "" if ae is None else f"{ae:.4f}"
            row["da"] = "" if da is None else da
            row["e_low"] = "" if e_low is None else f"{e_low:.6f}"
            row["e_high"] = "" if e_high is None else f"{e_high:.6f}"
            writer.writerow(row)

        f.write("\n")
        for line in summary_lines:
            cols = line.strip().split(",")
            if len(cols) == 2 and cols[0] not in ("b_low", "b_high", "MAE", "MDA", "total_runtime_mins"):
                f.write(line)
        if mae is not None:
            f.write(f"MAE,{mae:.4f}\n")
        else:
            f.write("MAE,NA\n")
        if mda is not None:
            f.write(f"MDA,{mda:.6f}\n")
        else:
            f.write("MDA,NA\n")
        if b_low is not None:
            f.write(f"b_low,{b_low:.4f}%\n")
        else:
            f.write("b_low,NA\n")
        if b_high is not None:
            f.write(f"b_high,{b_high:.4f}%\n")
        else:
            f.write("b_high,NA\n")

    print(f"evaluated_rows={len(ae_values)}")
    print(f"da_rows={len(da_values)}")
    if mae is not None:
        print(f"MAE={mae:.4f}")
    else:
        print("MAE=NA")
    if mda is not None:
        print(f"MDA={mda:.6f}")
    else:
        print("MDA=NA")
    if b_low is not None:
        print(f"b_low={b_low:.4f}%")
    else:
        print("b_low=NA")
    if b_high is not None:
        print(f"b_high={b_high:.4f}%")
    else:
        print("b_high=NA")


if __name__ == "__main__":
    main()
