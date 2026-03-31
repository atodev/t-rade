#!/bin/bash
# push_logs.sh — push live log files to GitHub so the remote Claude agent can read them.
# Run this hourly via crontab:
#   0 * * * * /Users/tombutler/development/t-rade/push_logs.sh >> /tmp/push_logs.out 2>&1

set -e
cd "$(dirname "$0")"

# Ensure log files exist (create empty if not)
touch milestone_log.md strategy_log.md analytics_report.md trades.csv

# Stage only the log/data files — never commit .env or *.db
git add milestone_log.md strategy_log.md analytics_report.md trades.csv

# Only commit if there are staged changes
if git diff --cached --quiet; then
    echo "[$(date)] No log changes to push."
    exit 0
fi

git commit -m "logs: auto-update $(date -u '+%Y-%m-%d %H:%M UTC')"
git push origin main
echo "[$(date)] Logs pushed to GitHub."
