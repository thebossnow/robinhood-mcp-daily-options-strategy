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

# Get current time in ET
CURRENT_TIME=$(TZ="America/New_York" date +%H:%M)

# Check if current time matches a collection slot
SHOULD_RUN=false
for slot in "${COLLECTION_TIMES[@]}"; do
    if [ "$CURRENT_TIME" = "$slot" ]; then
        SHOULD_RUN=true
        break
    fi
done

if [ "$SHOULD_RUN" = false ]; then
    echo "$(date): Skipped (not a collection time: $CURRENT_TIME ET)" >> "$LOG_FILE"
    exit 0
fi

# Run the scan for data collection only
echo "$(date): Starting data collection at $CURRENT_TIME ET" >> "$LOG_FILE"
python3 scripts/scan.py --data-only --save-snapshot --provider mcp >> "$LOG_FILE" 2>&1

echo "$(date): Data collection run completed" >> "$LOG_FILE"
