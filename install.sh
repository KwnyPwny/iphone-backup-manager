#!/bin/bash
# install.sh — setup, update, and uninstall for iphone-backup-server
#
# Usage:
#   ./install.sh              Full install (idempotent — skips completed steps)
#   ./install.sh --update     Update scripts + services only (skips build from source)
#   ./install.sh --uninstall  Remove all program files (backup data is preserved)

set -euo pipefail

# ── Parse mode ────────────────────────────────────────────────────────────────
MODE="install"
case "${1:-}" in
    --update)    MODE="update"    ;;
    --uninstall) MODE="uninstall" ;;
    "")          MODE="install"   ;;
    *) echo "Usage: $0 [--update|--uninstall]"; exit 1 ;;
esac

# ── Constants ─────────────────────────────────────────────────────────────────
INSTALL_DIR="/opt/iphone-backup"
BACKUP_RAW="/backups/raw"
BACKUP_BORG="/backups/borg"
BACKUP_PHOTO_SYNC="/backups/photo-sync"
LOG_FILE="/var/log/iphone-backup.log"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[done]${NC}  $*"; }
skip()  { echo -e "${CYAN}[skip]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC}  $*"; }
error() { echo -e "${RED}[fail]${NC}  $*"; exit 1; }
step()  { echo -e "\n${YELLOW}──${NC} $*"; }

# ── Guards ────────────────────────────────────────────────────────────────────
require_not_root() {
    [[ $EUID -ne 0 ]] || error "Run as a regular user with sudo, not as root."
    sudo -v          || error "sudo access required."
}

# ── Step 1: system packages ───────────────────────────────────────────────────
install_packages() {
    step "System packages"
    # Check if all required packages are already present
    local pkgs=(build-essential git autoconf automake libtool
                pkg-config libssl-dev libusb-1.0-0-dev libplist-dev
                python3 python3-cryptography avahi-daemon avahi-utils borgbackup ifuse authbind)
    local missing=()
    for p in "${pkgs[@]}"; do
        dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" \
            || missing+=("$p")
    done
    if [[ ${#missing[@]} -eq 0 ]]; then
        skip "All packages already installed"
    else
        info "Installing: ${missing[*]}"
        sudo apt-get update -q
        sudo apt-get install -y "${missing[@]}"
    fi
}

# ── Step 2: libimobiledevice stack from source ────────────────────────────────
build_libimobiledevice() {
    step "libimobiledevice stack"
    if command -v idevicebackup2 &>/dev/null; then
        skip "idevicebackup2 already installed ($(idevicebackup2 --version 2>&1 | head -1))"
        return
    fi

    info "Building from source (this takes a few minutes)..."
    sudo mkdir -p /opt/libimobiledevice-src
    sudo chown "$USER":"$USER" /opt/libimobiledevice-src
    cd /opt/libimobiledevice-src

    local PKG=/usr/local/lib/pkgconfig

    _build_repo() {
        local name="$1"
        info "  $name"
        [[ -d "$name" ]] || git clone --depth=1 "https://github.com/libimobiledevice/${name}.git"
        cd "$name"
        PKG_CONFIG_PATH="$PKG" ./autogen.sh --prefix=/usr/local
        make -j"$(nproc)"
        sudo make install
        cd ..
    }

    _build_repo libplist
    _build_repo libimobiledevice-glue
    _build_repo libusbmuxd
    _build_repo libimobiledevice
    sudo ldconfig
    cd "$SCRIPT_DIR"
}

# ── Step 3: netmuxd binary ────────────────────────────────────────────────────
install_netmuxd() {
    step "netmuxd"
    local dest="$INSTALL_DIR/netmuxd"
    if [[ -x "$dest" ]]; then
        skip "netmuxd already at $dest"
        return
    fi

    local arch
    arch=$(uname -m)
    local tag
    case "$arch" in
        x86_64)  tag="x86_64-linux-gnu"  ;;
        aarch64) tag="aarch64-linux-gnu"  ;;
        *) error "Unsupported arch: $arch. Get netmuxd from https://github.com/jkcoxson/netmuxd/releases" ;;
    esac

    sudo mkdir -p "$INSTALL_DIR"
    sudo wget -q --show-progress \
        "https://github.com/jkcoxson/netmuxd/releases/latest/download/netmuxd-${tag}" \
        -O "$dest"
    sudo chmod +x "$dest"
    info "netmuxd installed"
}

# ── Step 4: backup directories ────────────────────────────────────────────────
setup_directories() {
    step "Backup directories"
    sudo mkdir -p "$BACKUP_RAW" "$BACKUP_BORG" "$BACKUP_PHOTO_SYNC"
    sudo chown "$USER":"$USER" "$BACKUP_RAW" "$BACKUP_BORG" "$BACKUP_PHOTO_SYNC"
    sudo touch "$LOG_FILE"
    sudo chown "$USER":"$USER" "$LOG_FILE"
    info "Directories ready"
}

# ── Step 5: Borg repository ───────────────────────────────────────────────────
init_borg() {
    step "Borg repository"
    if [[ -f "$BACKUP_BORG/config" ]]; then
        skip "Borg repo already initialised at $BACKUP_BORG"
    else
        borg init --encryption=none "$BACKUP_BORG"
        info "Borg repo initialised"
    fi
}

# ── Step 6: program scripts ───────────────────────────────────────────────────
install_scripts() {
    step "Scripts"
    sudo mkdir -p "$INSTALL_DIR"
    local files=(trigger-backup.sh add-wifi-mac.py status-server.py setup-ios-ssh-key.sh check-stale.sh sync-photos.sh)
    for f in "${files[@]}"; do
        sudo cp "$SCRIPT_DIR/scripts/$f" "$INSTALL_DIR/$f"
        sudo chmod +x "$INSTALL_DIR/$f"
    done
    info "Scripts installed to $INSTALL_DIR"
}

# ── Step 7: TLS certificate ───────────────────────────────────────────────────
setup_ssl() {
    step "TLS certificate"
    local ssl_dir="$INSTALL_DIR/ssl"
    sudo mkdir -p "$ssl_dir"
    sudo chown "$USER":"$USER" "$ssl_dir"

    if [[ -f "$ssl_dir/cert.pem" ]]; then
        local expiry
        expiry=$(openssl x509 -enddate -noout -in "$ssl_dir/cert.pem" 2>/dev/null \
                 | sed 's/notAfter=//')
        skip "Certificate already exists (expires: $expiry)"
        return
    fi

    local vm_ip
    vm_ip=$(hostname -I | awk '{print $1}')
    openssl req -x509 \
        -newkey ec -pkeyopt ec_paramgen_curve:P-256 \
        -keyout "$ssl_dir/key.pem" \
        -out    "$ssl_dir/cert.pem" \
        -days   3650 -nodes \
        -subj   "/CN=${vm_ip}" \
        -addext "subjectAltName=IP:${vm_ip}" \
        2>/dev/null
    chmod 600 "$ssl_dir/key.pem"
    info "Certificate generated for $vm_ip (valid 10 years)"
}

# ── Step 8: systemd services ──────────────────────────────────────────────────
setup_systemd() {
    step "systemd services"

    sudo cp "$SCRIPT_DIR/systemd/netmuxd.service" /etc/systemd/system/netmuxd.service

    sed "s/STATUS_USER/$USER/g" \
        "$SCRIPT_DIR/systemd/iphone-backup-status.service" \
        | sudo tee /etc/systemd/system/iphone-backup-status.service > /dev/null

    sed "s/STATUS_USER/$USER/g" \
        "$SCRIPT_DIR/systemd/iphone-backup-stale.service" \
        | sudo tee /etc/systemd/system/iphone-backup-stale.service > /dev/null

    sudo cp "$SCRIPT_DIR/systemd/iphone-backup-stale.timer" \
            /etc/systemd/system/iphone-backup-stale.timer


    sed "s/STATUS_USER/$USER/g" \
        "$SCRIPT_DIR/config/logrotate" \
        | sudo tee /etc/logrotate.d/iphone-backup > /dev/null

    # Allow the backup user to bind port 443 via authbind
    sudo touch /etc/authbind/byport/443
    sudo chown "$USER" /etc/authbind/byport/443
    sudo chmod 755 /etc/authbind/byport/443

    sudo systemctl daemon-reload
    sudo systemctl enable --now avahi-daemon
    sudo systemctl enable --now netmuxd
    sudo systemctl enable --now iphone-backup-stale.timer

    # Start or restart the status server
    if systemctl is-active --quiet iphone-backup-status 2>/dev/null; then
        sudo systemctl restart iphone-backup-status
    else
        sudo systemctl enable --now iphone-backup-status
    fi

    # Verify it actually came up
    sleep 2
    if systemctl is-active --quiet iphone-backup-status; then
        info "Status server running"
    else
        warn "Status server failed to start. Logs:"
        sudo journalctl -u iphone-backup-status -n 20 --no-pager || true
    fi

    # Disable usbmuxd if running — it conflicts with netmuxd's socket
    if systemctl is-active --quiet usbmuxd 2>/dev/null; then
        warn "usbmuxd is running — stopping it (netmuxd takes over /var/run/usbmuxd)"
        sudo systemctl stop usbmuxd
        sudo systemctl disable usbmuxd
    fi

    info "Services enabled"
}

# ── Uninstall ─────────────────────────────────────────────────────────────────
uninstall() {
    echo ""
    warn "This removes all iphone-backup-server program files."
    echo ""
    echo "  Will be removed:"
    echo "    - Systemd services: netmuxd, iphone-backup-status"
    echo "    - $INSTALL_DIR  (scripts, TLS cert, VAPID key, push subscriptions)"
    echo "    - /etc/logrotate.d/iphone-backup"
    echo "    - $LOG_FILE"
    echo ""
    echo "  Will NOT be removed (your data):"
    echo "    - $BACKUP_RAW and $BACKUP_BORG  (backup data)"
    echo "    - /var/lib/lockdown/             (iPhone pairing records)"
    echo "    - /opt/libimobiledevice-src/     (compiled libraries)"
    echo "    - /usr/local/bin/idevice*        (installed binaries)"
    echo ""
    read -r -p "Continue? [y/N] " confirm
    echo
    [[ "${confirm,,}" == "y" ]] || { echo "Aborted."; exit 0; }

    step "Stopping services"
    for svc in iphone-backup-status iphone-backup-stale.timer iphone-backup-stale netmuxd; do
        if systemctl is-active --quiet "$svc" 2>/dev/null; then
            sudo systemctl stop "$svc"
        fi
        if systemctl is-enabled --quiet "$svc" 2>/dev/null; then
            sudo systemctl disable "$svc"
        fi
        sudo rm -f "/etc/systemd/system/${svc}.service"
    done
    sudo systemctl daemon-reload
    info "Services removed"

    step "Removing program files"
    sudo rm -rf "$INSTALL_DIR"
    sudo rm -f /etc/logrotate.d/iphone-backup
    sudo rm -f "$LOG_FILE"
    info "Program files removed"

    echo ""
    info "Uninstall complete. Your backup data in $BACKUP_BORG and $BACKUP_RAW was not touched."
}

# ── Print next-steps summary ──────────────────────────────────────────────────
print_summary() {
    local vm_ip
    vm_ip=$(hostname -I | awk '{print $1}')
    echo ""
    info "Done!"
    echo ""
    echo "  Status dashboard:  https://${vm_ip}"
    echo ""
    echo "  Next steps (manual, one-time):"
    echo "    1. Pair iPhone over USB             → README.md § Pairing"
    echo "    2. Add WiFi MAC to pairing record:"
    echo "         avahi-browse -r _apple-mobdev2._tcp"
    echo "         sudo python3 $INSTALL_DIR/add-wifi-mac.py <UDID> <MAC>"
    echo "    3. Enable WiFi Sync in Finder/iTunes and click Sync"
    echo "    4. Test: idevice_id -l -n"
    echo "    5. Set up iOS Shortcut             → README.md § iOS Automation"
    echo "    6. Restrict Shortcut SSH key:"
    echo "         $INSTALL_DIR/setup-ios-ssh-key.sh \"ssh-ed25519 AAAA...\""
    echo "    7. Enable push notifications       → README.md § Push Notifications"
    echo ""
    echo "  Optional: photo sync (server → iPhone)"
    echo "    Put photos in $BACKUP_PHOTO_SYNC, then run:"
    echo "      $INSTALL_DIR/sync-photos.sh"
    echo ""
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
    require_not_root

    case "$MODE" in
        install)
            install_packages
            build_libimobiledevice
            install_netmuxd
            setup_directories
            init_borg
            install_scripts
            setup_ssl
            setup_systemd
            print_summary
            ;;
        update)
            echo "Updating scripts and services (skipping build from source)..."
            install_packages   # picks up new deps added since last install
            install_scripts    # always deploy latest scripts
            setup_ssl          # no-op if cert exists
            setup_systemd      # updates unit files + restarts
            echo ""
            info "Update complete."
            ;;
        uninstall)
            uninstall
            ;;
    esac
}

main "$@"
