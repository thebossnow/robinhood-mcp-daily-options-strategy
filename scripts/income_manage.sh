#!/bin/bash
# Daily management of open income positions (15:50 ET = 12:50 PT via cron):
# 50% profit take and scaled time exit are applied automatically;
# the IV-spike grade is REPORTED ONLY and needs a human decision.
# Grep the log for DEFEND or CLOSE.

REPO_DIR="${OPTIONS_REPO_DIR:-/home/banderson/robinhood-mcp-daily-options-strategy}"
# OPTIONS_REPO_DIR overrides the repo location.
cd "$REPO_DIR" || exit 1
mkdir -p logs
LOG_FILE="logs/income_trading.log"

echo "$(date): income management starting" >> "$LOG_FILE"
python3 scripts/manage_income.py --provider mcp >> "$LOG_FILE" 2>&1
echo "$(date): income management finished" >> "$LOG_FILE"
