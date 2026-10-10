#!/bin/bash
# Income-framework paper entries (Mondays and Tuesdays 10:35 ET = 7:35 PT via cron).
# scan_income.py enforces the Monday/Tuesday cadence itself, so a
# misconfigured cron line degrades to a logged no-op rather than
# off-schedule entries — same posture as credit_entry.sh.
#
# Runs INCOME_PROFILE (default: as_specified, the written playbook).
# Set INCOME_PROFILE=evidence_adjusted to run the alternative. PAPER ONLY.
# OPTIONS_REPO_DIR overrides the repo location (default below).

REPO_DIR="${OPTIONS_REPO_DIR:-/home/banderson/robinhood-mcp-daily-options-strategy}"
cd "$REPO_DIR" || exit 1
mkdir -p logs
LOG_FILE="logs/income_trading.log"

echo "$(date): income entry scan starting" >> "$LOG_FILE"
python3 scripts/scan_income.py --profile "${INCOME_PROFILE:-as_specified}" \
    --provider mcp >> "$LOG_FILE" 2>&1
echo "$(date): income entry scan finished" >> "$LOG_FILE"
