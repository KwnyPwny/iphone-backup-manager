# iphone-backup-server

Automatic iPhone backups over WiFi to a **self-hosted server**, using **libimobiledevice**, **netmuxd**, and **Borg Backup**.

## Example Scenario

As an example, you can use this project to backup your iPhone automatically when you plug it into the charger at night.

**Key constraint:** Since iOS 16.1, Apple requires a PIN confirmation on the device for every backup. This means a fully automated (unattended) backup does not work. Instead, you can use an iOS Shortcut automation that triggers the backup when you want it to happen. At that point, you have to confirm the device password once but the rest runs unattended.

## Requirements

- Backup server (currently only tested on **Debian 13 (Trixie)**)
- iPhone on the same network as the server
- One-time USB pairing to server or another Linux machine
- **macOS or Windows machine with Finder / iTunes** (one-time requirement, see below)

### Why macOS or Windows is required

WiFi Sync cannot be enabled from Linux. It must be activated exactly once using **Finder on macOS** or **iTunes on Windows**:

1. Connect the iPhone via USB to the Mac or Windows machine
2. Open Finder (macOS) or iTunes (Windows) and select the iPhone
3. Enable "Show this iPhone over Wi-Fi" and click **Sync**

The Sync click is mandatory. The iPhone only starts broadcasting itself on the local network after that first sync. Without this step, nothing on the Linux side will ever discover the device over WiFi, regardless of how everything else is configured.

After this one-time setup, the Mac or Windows machine is no longer needed.

## Vibe coded

This project is mainly coded with Claude. As with every piece of software on the Internet, use at your own risk.

## Installation

```bash
git clone https://github.com/KwnyPwny/iphone-backup-server.git
cd iphone-backup-server
./install.sh
```

The script installs all dependencies, builds the libimobiledevice stack from source, downloads netmuxd, sets up the backup directories, initialises the Borg repository, and enables the systemd services.

## Setup Guide

### 1. Pair your iPhone (one-time, USB required)

USB pairing must happen exactly once.

```bash
sudo apt install usbmuxd libimobiledevice-utils
idevicepair pair           # plug in iPhone, confirm Trust
idevice_id -l              # note the UDID
scp /var/lib/lockdown/<UDID>.plist user@<VM-IP>:/tmp/
```

On the VM:
```bash
sudo mkdir -p /var/lib/lockdown
sudo cp /tmp/<UDID>.plist /var/lib/lockdown/
sudo chown root:root /var/lib/lockdown/<UDID>.plist
```

### 2. Enable WiFi Sync

WiFi Sync must be activated in **Finder (macOS)** or **iTunes (Windows)**. There is no CLI for this.

1. Connect iPhone via USB to a Mac or Windows PC
2. Open Finder / iTunes, select your iPhone
3. Enable **"Show this iPhone over Wi-Fi"**
4. Click **Sync** (mandatory: the iPhone will not broadcast itself on the network until after this sync)

Verify:
```bash
ideviceinfo -q com.apple.mobile.wireless_lockdown
# Expected: EnableWifiConnections: true
```

### 3. Add WiFi MAC address to pairing record

netmuxd matches network-discovered devices against pairing records using the WiFi MAC address. Because pairing was done over USB, this field is absent and must be added manually.

Find the MAC address:
```bash
avahi-browse -r _apple-mobdev2._tcp
# The MAC appears at the start of the service name, e.g. "de:ad:be:ef:13:37@..."
```

Add it to the pairing record:
```bash
sudo python3 /opt/iphone-backup/add-wifi-mac.py <UDID> <MAC-ADDRESS>
```

### 4. Test the WiFi connection

Plug the iPhone into a charger, lock the screen, then on the VM:
```bash
avahi-browse -t _apple-mobdev2._tcp    # iPhone should appear
idevice_id -l -n                       # UDID over network
ideviceinfo -n                         # device info over WiFi
```

### 5. Enable backup encryption (recommended)

```bash
idevicebackup2 -n encryption on <YOUR_PASSPHRASE>
```

### 6. Run a manual backup

```bash
/opt/iphone-backup/trigger-backup.sh
tail -f /var/log/iphone-backup.log
```

### 7. Set up iOS Shortcut Automation

The automation triggers the backup script via SSH when you plug your iPhone into the charger after 22:00 on your home WiFi.

**Create the Shortcut:**

1. Open **Shortcuts** app → **Shortcuts** tab → **+**
2. Name it **"Trigger Backup"**
3. Add these actions in order:

   - **Get Current Date**
   - **Format Date** → Custom format → `H`
   - **If** → Formatted Date (number) **is greater than or equal to** `22` → then:
     - **Get Network Details** → **Network Name**
     - **If** → **is** → `<YOUR_WIFI_NAME>` → then:
       - **Run Script over SSH**
         - Host: `<VM-IP>`
         - Port: `22` (or your SSH port)
         - User: `<your-user>`
         - Authentication: SSH Key
         - Script: `/opt/iphone-backup/trigger-backup.sh`
     - **Else** → End If
   - **Else** → End If

Instead of adding the key to `authorized_keys` directly, use the hardening helper in the next step.

**Create the Automation:**

1. **Shortcuts** app → **Automation** tab → **+**
2. **Create Personal Automation**
3. Trigger: **Charger** → **Is Connected**
4. Action: **Run Shortcut** → select **"Trigger Backup"**
5. Disable **"Ask Before Running"** → **Run Immediately**

### 8. Restrict the iOS Shortcut SSH key

The key used by the iOS Shortcut should only be able to run the backup script, nothing else. A helper script sets up the `command=` restriction in `authorized_keys` automatically.

In the Shortcuts app, open the SSH action, tap the key, and share the public key. Then on the VM:

```bash
/opt/iphone-backup/setup-ios-ssh-key.sh "ssh-ed25519 AAAA..."
```

The key is then locked down so that even if it were ever compromised, an attacker could only trigger a backup and not get a shell.

## Photo Sync (server → iPhone)

Photos and videos placed in `/backups/photo-sync/` can be synced to the iPhone's camera roll via `ifuse`. This is the same thing iTunes does when syncing photos from a computer, without iTunes.

Supported formats: JPG, JPEG, PNG, HEIC, HEIF, MP4, MOV, M4V.

Files already present on the device (matched by filename) are skipped. The sync is one-way and additive — nothing is deleted from the iPhone.

> The device must be unlocked when the sync runs, as iOS requires user presence for filesystem write access.

**Trigger manually** whenever new photos are in the sync folder:

```bash
# Copy photos to the sync folder (from your local machine)
scp my-photos/*.jpg user@<VM-IP>:/backups/photo-sync/

# Run the sync (iPhone must be reachable and unlocked)
/opt/iphone-backup/sync-photos.sh
```

You can also add it to your iOS Shortcut as a second SSH action (same pattern as the backup trigger, different script path).

## Restoring a Backup

List available snapshots:
```bash
borg list /backups/borg
```

Extract a snapshot:
```bash
cd /tmp
borg extract /backups/borg::2026-04-07_04:00
```

Restore to iPhone (USB only):
```bash
idevicebackup2 -p restore --system --settings /tmp/backups/raw/
```

The iPhone will reboot several times during restore. Apps are re-downloaded automatically from the App Store.

## Status Dashboard

A minimal web UI is included and runs automatically after installation. Open it in any browser on your local network:

```
https://<VM-IP>
```

The dashboard is served over **HTTPS on port 443**. The TLS certificate is self-signed and generated automatically during installation.

### Push Notifications

The dashboard supports native iOS push notifications via the Web Push API. The backup script automatically sends a notification when a backup succeeds or fails.

**One-time setup on iPhone:**

1. Open `https://<VM-IP>` in Safari
2. Tap the **"Download TLS certificate"** link at the bottom of the page
3. Open the downloaded profile: **Settings > General > VPN & Device Management > Install**
4. Enable full trust: **Settings > General > About > Certificate Trust Settings > toggle on**
5. Back in Safari, tap the Share button and **"Add to Home Screen"**
6. Open the page from the Home Screen (this is required since push only works for installed PWAs on iOS)
7. Tap **"Enable Notifications"** and confirm the permission dialog

After this, the server sends push notifications directly to your iPhone whenever a backup completes or fails, even when the page is not open.

> Push notifications require iOS 16.4 or later.

The dashboard shows:

- **Last Backup** — status (Success / Failed / Running), start time, duration, and any error messages
- **Storage** — original backup size, deduplicated size stored by Borg, and space saved
- **System** — whether the iPhone is currently reachable over WiFi, netmuxd service state, and total backup count
- **Snapshots** — list of all Borg archives with timestamps

The page refreshes automatically every 60 seconds. No dependencies beyond Python 3 (stdlib only).

To check the service:
```bash
sudo systemctl status iphone-backup-status
```

The port and other settings can be changed via the environment variables in `/etc/systemd/system/iphone-backup-status.service`.

### Local preview (demo mode)

To preview the dashboard without a running server, use the `--demo` flag. It serves realistic fake data over plain HTTP on `localhost:8080`.

```bash
python3 scripts/status-server.py --demo
# Open http://127.0.0.1:8080
```

## Architecture

| Component | Role |
|---|---|
| **avahi-daemon** | Listens for mDNS broadcasts; makes `_apple-mobdev2._tcp` services available locally |
| **netmuxd** | Queries avahi, matches WiFi MAC to pairing record, connects to iPhone over TCP, exposes `/var/run/usbmuxd` socket |
| **libusbmuxd** | Client library that talks to the usbmuxd/netmuxd socket; abstracts USB vs WiFi transport |
| **libimobiledevice** | Implements Apple protocols: Lockdown (pairing/TLS), MobileBackup2 (backup protocol) |
| **libplist** | Reads/writes Apple Property List files (pairing records, backup manifests) |
| **libimobiledevice-glue** | Shared utilities (socket handling, threads) between libimobiledevice and libusbmuxd |
| **idevicebackup2** | CLI tool; uses libimobiledevice to run a backup session and write iTunes-compatible files |
| **Borg Backup** | Deduplication, compression, and retention management on the finished backup directory |
| **Pairing record** | `/var/lib/lockdown/<UDID>.plist`: trust certificate with key pairs from USB pairing + WiFi MAC |

## File Layout

```
/opt/iphone-backup/
├── netmuxd              # network muxer daemon binary
├── trigger-backup.sh    # backup script (called by iOS Shortcut)
├── add-wifi-mac.py      # helper to patch WiFiMACAddress into pairing record
└── status-server.py     # web status dashboard (HTTPS, port 443)

/backups/
├── raw/                 # iTunes-compatible backup (idevicebackup2 output)
└── borg/                # Borg repository (deduplicated snapshots)

/var/log/iphone-backup.log
/var/lib/lockdown/<UDID>.plist   # pairing record
/etc/systemd/system/netmuxd.service
```

## Troubleshooting

**`avahi-browse` shows the iPhone but `idevice_id -l -n` is empty**
→ Check netmuxd is running: `sudo systemctl status netmuxd`
→ Verify WiFiMACAddress is in the pairing record: `sudo python3 /opt/iphone-backup/add-wifi-mac.py`

**iPhone does not broadcast `_apple-mobdev2._tcp`**
→ WiFi Sync was not properly activated. In Finder/iTunes, click **Sync** (not just enabling the checkbox). iPhone must be charging and screen locked.

**netmuxd logs "Could not get string value of WiFiMACAddress"**
→ The WiFiMACAddress field is missing from the pairing record. Run step 3 above.

**netmuxd socket conflicts with usbmuxd**
→ `sudo systemctl stop usbmuxd && sudo systemctl disable usbmuxd`
→ netmuxd owns `/var/run/usbmuxd`.

**Backup fails with "PIN required"**
→ Expected since iOS 16.1. Confirm the PIN on your iPhone when prompted. The iOS Shortcut automation handles re-triggering; you only need to confirm once per backup.
