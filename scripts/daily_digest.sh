#!/bin/bash
# Daily paper-trading digest (16:00 ET = 13:00 PT via cron).
REPO_DIR="/home/banderson/robinhood-mcp-daily-options-strategy"
cd "$REPO_DIR" || exit 1
mkdir -p logs
python3 scripts/daily_digest.py >> logs/daily_digest.log 2>&1
