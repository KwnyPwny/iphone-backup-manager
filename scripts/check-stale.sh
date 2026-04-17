#!/bin/bash
# /opt/iphone-backup/check-stale.sh
# Run daily via systemd timer. Sends a push notification if no successful
# backup has been recorded in the last MAX_AGE_DAYS days.

LOG="/var/log/iphone-backup.log"
STATUS_URL="https://localhost/notify"
MAX_AGE_DAYS=3

_notify() {
    [ -n "$STATUS_URL" ] || return 0
    curl -sk -X POST "$STATUS_URL" \
        -H "Content-Type: application/json" \
        -d "{\"title\":\"$1\",\"body\":\"$2\"}" \
        > /dev/null 2>&1 || true
}

# Find the last successful finish timestamp
LAST_FINISH=$(grep "=== Backup finished:" "$LOG" 2>/dev/null | tail -1 \
              | sed 's/=== Backup finished: //;s/ ===//')

if [ -z "$LAST_FINISH" ]; then
    _notify "Backup Warning" "No successful backup has ever been recorded"
    exit 0
fi

LAST_EPOCH=$(date -d "$LAST_FINISH" +%s 2>/dev/null)
if [ -z "$LAST_EPOCH" ]; then
    exit 0   # can't parse date, skip silently
fi

AGE_DAYS=$(( ( $(date +%s) - LAST_EPOCH ) / 86400 ))

if [ "$AGE_DAYS" -ge "$MAX_AGE_DAYS" ]; then
    _notify "Backup Overdue" "Last successful backup was ${AGE_DAYS} days ago"
fi
