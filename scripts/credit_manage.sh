#!/bin/bash
# Daily management of open paper credit positions (15:45 ET via cron):
# 50% profit take, scaled time exit, settlement — no breach stop.

REPO_DIR="/home/banderson/robinhood-mcp-daily-options-strategy"
cd "$REPO_DIR" || exit 1
mkdir -p logs
LOG_FILE="logs/credit_trading.log"

echo "$(date): credit management starting" >> "$LOG_FILE"
python3 scripts/manage_credit.py --provider mcp >> "$LOG_FILE" 2>&1
echo "$(date): credit management finished" >> "$LOG_FILE"
