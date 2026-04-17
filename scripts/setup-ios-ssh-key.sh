#!/bin/bash
# setup-ios-ssh-key.sh
#
# Adds the iOS Shortcut's public SSH key to authorized_keys with a
# command= restriction so that key can ONLY trigger the backup script,
# nothing else. Run once per iPhone/key.
#
# Usage:
#   ./setup-ios-ssh-key.sh "ssh-ed25519 AAAA... optional-comment"
#
# How to get the public key from the Shortcuts app:
#   Shortcuts → "Run Script over SSH" action → tap the key → "Share Public Key"

set -euo pipefail

BACKUP_SCRIPT="/opt/iphone-backup/trigger-backup.sh"

if [ $# -eq 0 ]; then
    echo "Usage: $0 \"ssh-ed25519 AAAA...\""
    echo ""
    echo "Paste the public key from the Shortcuts app SSH action."
    exit 1
fi

PUBKEY="$*"

# Basic sanity check — must start with a known key type
if ! echo "$PUBKEY" | grep -qE "^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) "; then
    echo "Error: does not look like a valid public key."
    echo "Expected format: ssh-ed25519 AAAA..."
    exit 1
fi

RESTRICTED="command=\"$BACKUP_SCRIPT\",no-pty,no-agent-forwarding,no-X11-forwarding,no-port-forwarding $PUBKEY"

mkdir -p ~/.ssh
chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys

# Prevent duplicates
if grep -qF "$PUBKEY" ~/.ssh/authorized_keys 2>/dev/null; then
    echo "This key is already in authorized_keys."
    # Check if it already has the command= restriction
    if grep -F "$PUBKEY" ~/.ssh/authorized_keys | grep -q "command="; then
        echo "Restriction already in place. Nothing to do."
    else
        echo "WARNING: Key exists WITHOUT the command= restriction — it has unrestricted access."
        echo "Remove it manually from ~/.ssh/authorized_keys and re-run this script."
    fi
    exit 0
fi

echo "$RESTRICTED" >> ~/.ssh/authorized_keys
echo "Key added. This key can now only run: $BACKUP_SCRIPT"
