#!/bin/bash
# /opt/iphone-backup/sync-photos.sh
#
# Copies new photos/videos from SOURCE_DIR to the iPhone's camera roll via ifuse.
# Files already present on the device (matched by filename) are skipped.
# The iPhone must be reachable over WiFi (netmuxd running and device paired).
#
# Run manually whenever new photos are in SOURCE_DIR:
#   /opt/iphone-backup/sync-photos.sh
#
# Or trigger via iOS Shortcut (same SSH pattern as trigger-backup.sh).

SOURCE_DIR="/backups/photo-sync"   # put photos/videos here to sync to iPhone
MOUNT_POINT="/tmp/iphone-ifuse"
LOG="/var/log/iphone-backup.log"
STATUS_URL="https://localhost/notify"   # set to "" to disable push notifications

SUPPORTED_TYPES=(-iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.png"
                 -o -iname "*.heic" -o -iname "*.heif"
                 -o -iname "*.mp4" -o -iname "*.mov" -o -iname "*.m4v")

_notify() {
    [ -n "$STATUS_URL" ] || return 0
    curl -sk -X POST "$STATUS_URL" \
        -H "Content-Type: application/json" \
        -d "{\"title\":\"$1\",\"body\":\"$2\"}" \
        > /dev/null 2>&1 || true
}

_unmount() {
    if mountpoint -q "$MOUNT_POINT" 2>/dev/null; then
        fusermount3 -u "$MOUNT_POINT" 2>/dev/null \
            || fusermount -u "$MOUNT_POINT" 2>/dev/null \
            || true
    fi
    rmdir "$MOUNT_POINT" 2>/dev/null || true
}

echo "=== Photo sync started: $(date) ===" >> "$LOG"

# Sanity checks
if ! command -v ifuse &>/dev/null; then
    echo "ERROR: ifuse not found — run install.sh" >> "$LOG"
    exit 1
fi

if [ ! -d "$SOURCE_DIR" ]; then
    echo "ERROR: Source directory $SOURCE_DIR does not exist" >> "$LOG"
    exit 1
fi

FILE_COUNT=$(find "$SOURCE_DIR" -maxdepth 1 -type f \( "${SUPPORTED_TYPES[@]}" \) | wc -l)
if [ "$FILE_COUNT" -eq 0 ]; then
    echo "Photo sync: nothing to do ($SOURCE_DIR is empty)" >> "$LOG"
    exit 0
fi

# Check iPhone reachable
if ! idevice_id -l -n 2>/dev/null | grep -q .; then
    echo "ERROR: Photo sync: iPhone not reachable" >> "$LOG"
    _notify "Photo Sync Failed" "iPhone not reachable over WiFi"
    exit 1
fi

# Mount iPhone filesystem
mkdir -p "$MOUNT_POINT"
trap "_unmount" EXIT

ifuse "$MOUNT_POINT" 2>> "$LOG"
if ! mountpoint -q "$MOUNT_POINT"; then
    echo "ERROR: Photo sync: could not mount iPhone (device locked?)" >> "$LOG"
    _notify "Photo Sync Failed" "Could not mount iPhone — is the screen unlocked?"
    exit 1
fi

# Find or create a DCIM subfolder
DCIM_TARGET=$(find "$MOUNT_POINT/DCIM" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort | head -1)
if [ -z "$DCIM_TARGET" ]; then
    DCIM_TARGET="$MOUNT_POINT/DCIM/100APPLE"
    mkdir -p "$DCIM_TARGET"
fi

# Copy new files
COPIED=0
SKIPPED=0
FAILED=0
START=$(date +%s)

while IFS= read -r f; do
    NAME=$(basename "$f")
    if [ -f "$DCIM_TARGET/$NAME" ]; then
        SKIPPED=$((SKIPPED + 1))
    else
        if cp "$f" "$DCIM_TARGET/$NAME" 2>> "$LOG"; then
            COPIED=$((COPIED + 1))
        else
            echo "ERROR: failed to copy $NAME" >> "$LOG"
            FAILED=$((FAILED + 1))
        fi
    fi
done < <(find "$SOURCE_DIR" -maxdepth 1 -type f \( "${SUPPORTED_TYPES[@]}" \))

ELAPSED=$(( $(date +%s) - START ))
MINS=$(( ELAPSED / 60 )); SECS=$(( ELAPSED % 60 ))

echo "Photo sync: ${COPIED} copied, ${SKIPPED} skipped, ${FAILED} failed (${MINS}m ${SECS}s)" >> "$LOG"
echo "=== Photo sync finished: $(date) ===" >> "$LOG"

if [ "$FAILED" -gt 0 ]; then
    _notify "Photo Sync Done (with errors)" "${COPIED} copied, ${FAILED} failed — check log"
else
    _notify "Photo Sync Complete" "${COPIED} photos copied, ${SKIPPED} already on device"
fi
