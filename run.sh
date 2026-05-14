#!/usr/bin/env bash
# trade.sh — Run MiroFish predictions over seed files in parallel, then score results.
# Usage: ./trade.sh START_HOUR [NUM_SEEDS] [SEEDS_DIR]
# Example: ./trade.sh 2026-04-05T01 3 seeds/
# NUM_SEEDS: how many seeds to run simultaneously (default 3)
set -uo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="$ROOT/backend/.venv/bin/python3"
START_HOUR="${1:-}"
NUM_SEEDS="${2:-3}"
SEEDS_DIR="${3:-$ROOT/seeds}"
SIM_ROUNDS=5
MAX_PARALLEL=4

trap 'echo ""; echo "Interrupted."; exit 0' INT TERM

# Validate required argument
if [[ -z "$START_HOUR" ]]; then
    echo "Usage: ./trade.sh START_HOUR [NUM_SEEDS] [SEEDS_DIR]"
    echo "Example: ./trade.sh 2026-04-05T01 3"
    exit 1
fi

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "Error: Python venv not found at $VENV_PYTHON"
    exit 1
fi

# Create output directories
mkdir -p "$ROOT/price_predict"
mkdir -p "$ROOT/logs"

# Generate timestamped output filename (shared across CSV and log)
TIMESTAMP=$(date +"%Y-%m-%d-%H-%M-%S")
OUTPUT_CSV="$ROOT/price_predict/$TIMESTAMP.csv"
LOG_FILE="$ROOT/logs/$TIMESTAMP.log"

# Tee all output to log file
exec > >(tee -a "$LOG_FILE") 2>&1

echo "Trade loop | start=$START_HOUR | num_seeds=$NUM_SEEDS | rounds=$SIM_ROUNDS | seeds=$SEEDS_DIR | output=$OUTPUT_CSV"
echo ""

# Collect all seed files starting from START_HOUR
current_hour="$START_HOUR"
seed_files=()

for i in $(seq 1 "$NUM_SEEDS"); do
    seed_file="$SEEDS_DIR/seed_${current_hour}.md"

    if [[ ! -f "$seed_file" ]]; then
        break
    fi

    seed_files+=("$seed_file")

    next_hour=$(date -d "${current_hour/T/ } + 1 hour" +"%Y-%m-%dT%H" 2>/dev/null)
    if [[ -z "$next_hour" ]]; then
        break
    fi

    current_hour="$next_hour"
done

if [[ ${#seed_files[@]} -eq 0 ]]; then
    echo "No seed files found starting from $START_HOUR"
    exit 1
fi

echo "Found ${#seed_files[@]} seed files"
echo ""

# Track overall metrics
script_start=$(date +%s.%N)
all_failed=false
job_index=1

# Sliding window: arrays indexed by slot (0..MAX_PARALLEL-1)
active_pids=()    # pid or "" if slot is free
active_times=()   # "job_start:hour:job_num" per slot
active_hours=()   # hour per slot (for display)

# Reap a finished job from the active pool; updates active_pids/active_times.
# Sets _reaped_slot to the freed slot index.
reap_one() {
    while true; do
        for slot in "${!active_pids[@]}"; do
            pid="${active_pids[$slot]}"
            [[ -z "$pid" ]] && continue
            if ! kill -0 "$pid" 2>/dev/null; then
                time_info="${active_times[$slot]}"
                local job_start hour job_num
                job_start=$(echo "$time_info" | cut -d: -f1)
                hour=$(echo "$time_info" | cut -d: -f2)
                job_num=$(echo "$time_info" | cut -d: -f3)
                local job_end runtime_mins
                job_end=$(date +%s.%N)
                runtime_mins=$(awk "BEGIN {printf \"%.1f\", ($job_end - $job_start) / 60}")
                if wait "$pid"; then
                    echo ""
                    echo "[$job_num] $hour — ✓ ($runtime_mins mins)"
                else
                    all_failed=true
                    echo ""
                    echo "[$job_num] $hour — ✗ (failed)"
                fi
                active_pids[$slot]=""
                active_times[$slot]=""
                _reaped_slot=$slot
                return
            fi
        done
        sleep 0.5
    done
}

# Launch a seed job into a given slot
launch_seed() {
    local slot="$1"
    local seed_file="$2"
    local job_num="$3"
    local hour
    hour=$(basename "$seed_file" .md | sed 's/seed_//')

    # Look up actual low/high from the next candle (seed_hour + 1h, all UTC)
    local actual_args=()
    local actual_low actual_high
    read actual_low actual_high prev_mid < <(
        "$VENV_PYTHON" - "$hour" "$ROOT/marketdata" <<'PYEOF'
import sys, os, csv, datetime, calendar

hour_str = sys.argv[1]   # e.g. 2026-04-05T01
marketdata_dir = sys.argv[2]

dt = datetime.datetime.strptime(hour_str, "%Y-%m-%dT%H").replace(tzinfo=datetime.timezone.utc)
next_dt = dt + datetime.timedelta(hours=1)
next_day = next_dt.strftime("%Y-%m-%d")
next_hour_ms = int(next_dt.timestamp()) * 1000
next_hour_end_ms = next_hour_ms + 3_600_000

# Next candle (actual)
ohlcv_file = os.path.join(marketdata_dir, next_day, "ohlcv.csv")
lo, hi = None, None
if os.path.exists(ohlcv_file):
    with open(ohlcv_file) as f:
        for row in csv.DictReader(f):
            t = int(row["T"])
            if next_hour_ms <= t < next_hour_end_ms:
                l, h = float(row["L"]), float(row["H"])
                lo = l if lo is None else min(lo, l)
                hi = h if hi is None else max(hi, h)

# Previous candle (T-1 from seed hour, for DA)
prev_dt = dt - datetime.timedelta(hours=1)
prev_day = prev_dt.strftime("%Y-%m-%d")
prev_hour_ms = int(prev_dt.timestamp()) * 1000
prev_hour_end_ms = prev_hour_ms + 3_600_000
prev_ohlcv = os.path.join(marketdata_dir, prev_day, "ohlcv.csv")
plo, phi = None, None
if os.path.exists(prev_ohlcv):
    with open(prev_ohlcv) as f:
        for row in csv.DictReader(f):
            t = int(row["T"])
            if prev_hour_ms <= t < prev_hour_end_ms:
                l, h = float(row["L"]), float(row["H"])
                plo = l if plo is None else min(plo, l)
                phi = h if phi is None else max(phi, h)
prev_mid = (plo + phi) / 2 if plo is not None and phi is not None else ""

print(lo if lo is not None else "", hi if hi is not None else "", prev_mid)
PYEOF
    )
    [[ -n "$actual_low" ]] && actual_args+=(--actual-low "$actual_low")
    [[ -n "$actual_high" ]] && actual_args+=(--actual-high "$actual_high")
    [[ -n "$prev_mid" ]] && actual_args+=(--prev-mid "$prev_mid")

    echo -n "[$job_num] $hour — running prediction..."

    local job_start
    job_start=$(date +%s.%N)
    "$VENV_PYTHON" "$ROOT/backend/scripts/run_trade.py" \
        -o "$OUTPUT_CSV" \
        --rounds "$SIM_ROUNDS" \
        "${actual_args[@]}" \
        "$seed_file" &

    active_pids[$slot]=$!
    active_times[$slot]="$job_start:$hour:$job_num"
}

# Initialize slots as empty
for ((s = 0; s < MAX_PARALLEL; s++)); do
    active_pids[$s]=""
    active_times[$s]=""
done

# Process all seeds with sliding window
for ((i = 0; i < ${#seed_files[@]}; i++)); do
    seed_file="${seed_files[$i]}"
    job_num=$job_index
    job_index=$((job_index + 1))

    # Find a free slot; if none, reap one first
    free_slot=-1
    for slot in "${!active_pids[@]}"; do
        if [[ -z "${active_pids[$slot]}" ]]; then
            free_slot=$slot
            break
        fi
    done

    if [[ $free_slot -eq -1 ]]; then
        reap_one
        free_slot=$_reaped_slot
    fi

    launch_seed "$free_slot" "$seed_file" "$job_num"
done

# Drain remaining active jobs
while true; do
    has_active=false
    for slot in "${!active_pids[@]}"; do
        [[ -n "${active_pids[$slot]}" ]] && { has_active=true; break; }
    done
    [[ "$has_active" == false ]] && break
    reap_one
done

# Brief delay to ensure all file writes complete
sleep 0.5

# Calculate total script runtime
script_end=$(date +%s.%N)
script_total_mins=$(awk "BEGIN {printf \"%.1f\", ($script_end - $script_start) / 60}")

echo "Scoring results..."
score_output=$("$VENV_PYTHON" "$ROOT/backend/scripts/calc_metrics.py" "$OUTPUT_CSV")
echo "$score_output"

mae=$(echo "$score_output" | grep '^MAE=' | cut -d= -f2)
mda=$(echo "$score_output" | grep '^MDA=' | cut -d= -f2)

# Append summary to CSV
echo "" >> "$OUTPUT_CSV"
echo "total_runtime_mins,$script_total_mins" >> "$OUTPUT_CSV"
echo "MAE,${mae:-NA}" >> "$OUTPUT_CSV"
echo "MDA,${mda:-NA}" >> "$OUTPUT_CSV"

echo ""
echo "Total runtime: $script_total_mins mins"

if [[ "$all_failed" == true ]]; then
    exit 1
fi

exit 0
