#!/usr/bin/env python3
"""
Add the WiFiMACAddress field to an existing iOS pairing record.

netmuxd matches network-discovered devices against pairing records by WiFi MAC
address. When pairing was done over USB, this field is absent and must be added
manually before WiFi backup will work.

Usage:
    python3 add-wifi-mac.py <UDID> <MAC-ADDRESS>

Example:
    python3 add-wifi-mac.py 00008110-001234ABCDEF 12:34:56:78:9a:bc

The UDID is the filename of the pairing record (without .plist).
Find the MAC address with:
    avahi-browse -r _apple-mobdev2._tcp
It appears at the start of the service name, e.g. "de:ad:be:ef:13:37@...".
"""

import sys
import plistlib
from pathlib import Path

LOCKDOWN_DIR = Path("/var/lib/lockdown")


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)

    udid, mac = sys.argv[1], sys.argv[2]
    plist_path = LOCKDOWN_DIR / f"{udid}.plist"

    if not plist_path.exists():
        print(f"Error: pairing record not found at {plist_path}")
        sys.exit(1)

    with open(plist_path, "rb") as f:
        data = plistlib.load(f)

    if "WiFiMACAddress" in data:
        print(f"WiFiMACAddress already set to: {data['WiFiMACAddress']}")
        answer = input("Overwrite? [y/N] ").strip().lower()
        if answer != "y":
            sys.exit(0)

    data["WiFiMACAddress"] = mac

    with open(plist_path, "wb") as f:
        plistlib.dump(data, f)

    print(f"WiFiMACAddress set to {mac} in {plist_path}")


if __name__ == "__main__":
    main()
