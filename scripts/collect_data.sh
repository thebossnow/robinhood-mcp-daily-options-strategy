#!/bin/bash
# Data collection script for frequent snapshots every 45 minutes.
# Called by cron every 15 min; this script checks if it's a scheduled slot.

REPO_DIR="/home/banderson/robinhood-mcp-daily-options-strategy"
cd "$REPO_DIR" || exit 1

# Log directory
mkdir -p logs
LOG_FILE="logs/data_collection.log"

# Collection times in ET (America/New_York): HH:MM
# Starting 9:00 (30min before 9:30 open), every 45 min until 16:30 after close.
COLLECTION_TIMES=("09:00" "09:45" "10:30" "11:15" "12:00" "12:45" "13:30" "14:15" "15:00" "15:45" "16:30")

# Tolerance (minutes) around each slot, so a cron tick that fires a little
# late (system load, clock skew) still counts. Kept well under the 15-min cron
# interval and the 45-min slot spacing so a slot can only match one tick.
TOLERANCE_MIN=2

# Current time in ET, as minutes-since-midnight (10# forces base-10 so a
# leading-zero hour/minute like 09 isn't parsed as octal).
CURRENT_TIME=$(TZ="America/New_York" date +%H:%M)
CURRENT_MIN=$((10#$(TZ="America/New_York" date +%H) * 60 + 10#$(TZ="America/New_York" date +%M)))

# Check if we're within TOLERANCE_MIN of any collection slot
SHOULD_RUN=false
MATCHED_SLOT=""
for slot in "${COLLECTION_TIMES[@]}"; do
    slot_min=$((10#${slot%:*} * 60 + 10#${slot#*:}))
    diff=$((CURRENT_MIN - slot_min))
    diff=${diff#-}   # absolute value
    if [ "$diff" -le "$TOLERANCE_MIN" ]; then
        SHOULD_RUN=true
        MATCHED_SLOT="$slot"
        break
    fi
done

if [ "$SHOULD_RUN" = false ]; then
    echo "$(date): Skipped (not a collection time: $CURRENT_TIME ET)" >> "$LOG_FILE"
    exit 0
fi

# Run the scan for data collection only
echo "$(date): Starting data collection at $CURRENT_TIME ET (slot $MATCHED_SLOT)" >> "$LOG_FILE"
python3 scripts/scan.py --data-only --save-snapshot --provider mcp >> "$LOG_FILE" 2>&1

echo "$(date): Data collection run completed" >> "$LOG_FILE"
