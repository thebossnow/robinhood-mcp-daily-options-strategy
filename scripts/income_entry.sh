#!/bin/bash
# Income-framework paper entries (Mondays and Tuesdays 10:30 ET via cron).
# scan_income.py enforces the Monday/Tuesday cadence itself, so a
# misconfigured cron line degrades to a logged no-op rather than
# off-schedule entries — same posture as credit_entry.sh.
#
# Runs the evidence_adjusted profile. To observe what the verbatim
# framework refuses, run by hand:
#   python3 scripts/scan_income.py --profile as_specified --dry-run

REPO_DIR="/home/banderson/robinhood-mcp-daily-options-strategy"
cd "$REPO_DIR" || exit 1
mkdir -p logs
LOG_FILE="logs/income_trading.log"

echo "$(date): income entry scan starting" >> "$LOG_FILE"
python3 scripts/scan_income.py --profile evidence_adjusted \
    --provider mcp >> "$LOG_FILE" 2>&1
echo "$(date): income entry scan finished" >> "$LOG_FILE"
