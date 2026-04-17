#!/bin/bash
# /opt/iphone-backup/trigger-backup.sh
# Triggered by iOS Shortcut automation when iPhone is plugged in to charger.

LOG="/var/log/iphone-backup.log"
BACKUP_DIR="/backups/raw"
BORG_REPO="/backups/borg"
LOCKFILE="/tmp/iphone-backup.lock"
BACKUP_TIMEOUT="1h"  # kill idevicebackup2 if it hangs longer than this
STATUS_URL="https://localhost/notify"   # set to "" to disable push notifications

# Exit if a backup is already running
if [ -f "$LOCKFILE" ]; then
    PID=$(cat "$LOCKFILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "$(date): Backup already running (PID $PID). Exiting." >> "$LOG"
        exit 0
    else
        rm -f "$LOCKFILE"
    fi
fi

# Set lock
echo $$ > "$LOCKFILE"
trap "rm -f $LOCKFILE" EXIT

_notify() {
    [ -n "$STATUS_URL" ] || return 0
    local title="$1" body="$2"
    curl -sk -X POST "$STATUS_URL" \
        -H "Content-Type: application/json" \
        -d "{\"title\":\"$title\",\"body\":\"$body\"}" \
        > /dev/null 2>&1 || true   # never fail the backup due to a notification error
}

BACKUP_START=$(date +%s)
echo "=== Backup started: $(date) ===" >> "$LOG"

# Wait until iPhone is reachable (max 60 seconds)
for i in $(seq 1 12); do
    if idevice_id -l -n 2>/dev/null | grep -q .; then
        break
    fi
    sleep 5
done

if ! idevice_id -l -n 2>/dev/null | grep -q .; then
    echo "ERROR: iPhone not reachable" >> "$LOG"
    _notify "Backup Failed" "iPhone not reachable over WiFi"
    exit 1
fi

# Run backup (abort if it hangs longer than BACKUP_TIMEOUT)
timeout "$BACKUP_TIMEOUT" idevicebackup2 -n backup "$BACKUP_DIR" >> "$LOG" 2>&1
RC=$?
if [ $RC -eq 124 ]; then
    echo "ERROR: Backup timed out after $BACKUP_TIMEOUT" >> "$LOG"
    _notify "Backup Failed" "Timed out after $BACKUP_TIMEOUT — iPhone went offline?"
    exit 1
elif [ $RC -ne 0 ]; then
    echo "ERROR: Backup failed (exit $RC)" >> "$LOG"
    _notify "Backup Failed" "idevicebackup2 exited with an error — check the log"
    exit 1
fi

echo "Backup successful. Starting Borg." >> "$LOG"

# Create Borg snapshot
borg create --stats --compression zstd \
    "$BORG_REPO"::{now:%Y-%m-%d_%H:%M} \
    "$BACKUP_DIR" >> "$LOG" 2>&1

# Prune old snapshots
borg prune --stats \
    --keep-daily=7 \
    --keep-weekly=4 \
    --keep-monthly=12 \
    --keep-yearly=1 \
    "$BORG_REPO" >> "$LOG" 2>&1

borg compact "$BORG_REPO" >> "$LOG" 2>&1

echo "=== Backup finished: $(date) ===" >> "$LOG"

ELAPSED=$(( $(date +%s) - BACKUP_START ))
MINS=$(( ELAPSED / 60 ))
SECS=$(( ELAPSED % 60 ))
_notify "Backup Complete" "Finished in ${MINS}m ${SECS}s"
