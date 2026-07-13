#!/bin/bash
# Weekly credit-strategy paper entries (Mondays 10:30 ET via cron).
# scan_credit.py itself enforces the Monday check, so a misconfigured cron
# line degrades to a logged no-op rather than off-schedule entries.

REPO_DIR="/home/banderson/robinhood-mcp-daily-options-strategy"
cd "$REPO_DIR" || exit 1
mkdir -p logs
LOG_FILE="logs/credit_trading.log"

echo "$(date): credit entry scan starting" >> "$LOG_FILE"
python3 scripts/scan_credit.py --provider mcp >> "$LOG_FILE" 2>&1
echo "$(date): credit entry scan finished" >> "$LOG_FILE"
