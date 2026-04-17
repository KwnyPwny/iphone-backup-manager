#!/usr/bin/env python3
"""
/opt/iphone-backup/status-server.py

Status dashboard + Web Push notifications for iphone-backup-server.

HTTPS on port 8443 when ssl/cert.pem + ssl/key.pem exist (required for push).
Falls back to plain HTTP on port 8080 without SSL files.

Optional dependency (via apt):
  sudo apt install python3-cryptography   ← enables Web Push
"""

import base64
import hmac as _hmac_mod
import json
import os
import re
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

try:
    from http.server import ThreadingHTTPServer
except ImportError:
    ThreadingHTTPServer = HTTPServer  # < 3.7 fallback, won't happen on Debian 13

try:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand
    PUSH_AVAILABLE = True
except ImportError:
    PUSH_AVAILABLE = False


# ── Config ────────────────────────────────────────────────────────────────────

LOG_FILE  = Path(os.environ.get("BACKUP_LOG",  "/var/log/iphone-backup.log"))
BORG_REPO = Path(os.environ.get("BORG_REPO",   "/backups/borg"))
HOST      =     os.environ.get("STATUS_HOST",  "0.0.0.0")
LOCKFILE  = Path("/tmp/iphone-backup.lock")

INSTALL_DIR    = Path("/opt/iphone-backup")
VAPID_KEY_FILE = INSTALL_DIR / "vapid-private.pem"
SUBS_FILE      = INSTALL_DIR / "push-subscriptions.json"
SSL_CERT       = INSTALL_DIR / "ssl" / "cert.pem"
SSL_KEY        = INSTALL_DIR / "ssl" / "key.pem"

USE_HTTPS    = SSL_CERT.exists() and SSL_KEY.exists()
DEFAULT_PORT = 443 if USE_HTTPS else 8080
PORT         = int(os.environ.get("STATUS_PORT", str(DEFAULT_PORT)))

_subs_lock = threading.Lock()


# ── VAPID key management ──────────────────────────────────────────────────────

_vapid_private    = None
_vapid_public_raw = None   # uncompressed P-256 point, 65 bytes


def _init_vapid():
    global _vapid_private, _vapid_public_raw
    if not PUSH_AVAILABLE or not INSTALL_DIR.exists():
        return
    if VAPID_KEY_FILE.exists():
        _vapid_private = serialization.load_pem_private_key(
            VAPID_KEY_FILE.read_bytes(), password=None
        )
    else:
        _vapid_private = ec.generate_private_key(ec.SECP256R1())
        pem = _vapid_private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        VAPID_KEY_FILE.write_bytes(pem)
        VAPID_KEY_FILE.chmod(0o600)
        print("Generated new VAPID key pair.")
    _vapid_public_raw = _vapid_private.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )


def _vapid_public_b64() -> str:
    return _b64u(_vapid_public_raw) if _vapid_public_raw else ""


# ── Subscription storage ──────────────────────────────────────────────────────

def _load_subs() -> list:
    try:
        return json.loads(SUBS_FILE.read_text()) if SUBS_FILE.exists() else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_subs(subs: list):
    SUBS_FILE.write_text(json.dumps(subs, indent=2))


def add_subscription(sub: dict):
    with _subs_lock:
        subs = [s for s in _load_subs() if s.get("endpoint") != sub.get("endpoint")]
        subs.append(sub)
        _save_subs(subs)


def remove_subscription(endpoint: str):
    with _subs_lock:
        _save_subs([s for s in _load_subs() if s.get("endpoint") != endpoint])


# ── Web Push crypto (RFC 8291 aes128gcm + RFC 8292 VAPID) ────────────────────

def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64u_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    return HKDFExpand(algorithm=hashes.SHA256(), length=length, info=info).derive(prk)


def _vapid_jwt(audience: str) -> str:
    """Build and sign a VAPID JWT (ES256)."""
    header  = _b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    payload = _b64u(json.dumps({
        "aud": audience,
        "exp": int(time.time()) + 43200,  # 12 h
        "sub": "mailto:admin@localhost",
    }, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode()
    der_sig = _vapid_private.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_sig)
    return f"{header}.{payload}.{_b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def _encrypt_payload(plaintext: bytes, sub: dict) -> bytes:
    """
    Encrypt plaintext for Web Push per RFC 8291 (aes128gcm content encoding).
    Returns the full body: 86-byte header + ciphertext.
    """
    client_pub_raw = _b64u_decode(sub["keys"]["p256dh"])   # 65-byte uncompressed P-256 point
    auth_secret    = _b64u_decode(sub["keys"]["auth"])      # 16-byte auth secret

    client_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), client_pub_raw)

    server_priv   = ec.generate_private_key(ec.SECP256R1())
    server_pub_raw = server_priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    shared_secret = server_priv.exchange(ec.ECDH(), client_pub)  # 32-byte x-coordinate

    salt = os.urandom(16)

    # RFC 8291 §3.3 — key derivation
    # PRK_key = HKDF-Extract(salt=auth_secret, IKM=shared_secret)
    prk_key = _hmac_mod.new(auth_secret, shared_secret, "sha256").digest()
    # IKM = HKDF-Expand(PRK_key, "WebPush: info\x00" || ua_pub || as_pub, 32)
    ikm = _hkdf_expand(prk_key, b"WebPush: info\x00" + client_pub_raw + server_pub_raw, 32)
    # PRK = HKDF-Extract(salt=salt, IKM=ikm)
    prk = _hmac_mod.new(salt, ikm, "sha256").digest()

    cek   = _hkdf_expand(prk, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf_expand(prk, b"Content-Encoding: nonce\x00",     12)

    # Pad: append 0x02 end-of-record delimiter, encrypt with AES-128-GCM
    ciphertext = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)

    # Header: salt(16) + rs(4, big-endian) + idlen(1) + server_pub(65)
    header = salt + struct.pack(">I", 4096) + struct.pack("B", 65) + server_pub_raw
    return header + ciphertext


def _send_one_push(sub: dict, title: str, body: str) -> bool:
    """Send one push notification. Returns False if the subscription is gone."""
    endpoint = sub["endpoint"]
    audience = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(endpoint))
    jwt       = _vapid_jwt(audience)
    encrypted = _encrypt_payload(json.dumps({"title": title, "body": body}).encode(), sub)

    req = urllib.request.Request(
        endpoint,
        data=encrypted,
        headers={
            "Authorization":    f"vapid t={jwt},k={_vapid_public_b64()}",
            "Content-Type":     "application/octet-stream",
            "Content-Encoding": "aes128gcm",
            "TTL":              "86400",
            "Content-Length":   str(len(encrypted)),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status in (200, 201, 202)
    except urllib.error.HTTPError as e:
        return e.code not in (404, 410)   # 404/410 = subscription gone, remove it
    except Exception as e:
        print(f"Push error ({endpoint[:50]}…): {e}")
        return True  # keep subscription — might be a transient network issue


def notify_all(title: str, body: str):
    """Fan out a push notification to all subscribers (non-blocking)."""
    if not PUSH_AVAILABLE or _vapid_private is None:
        return

    def _task():
        stale = [
            sub["endpoint"]
            for sub in _load_subs()
            if not _send_one_push(sub, title, body)
        ]
        for ep in stale:
            remove_subscription(ep)

    threading.Thread(target=_task, daemon=True).start()


# ── Data collection ───────────────────────────────────────────────────────────

def _run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout, r.returncode
    except Exception:
        return "", -1


def parse_log():
    if not LOG_FILE.exists():
        return {"status": "unknown", "message": "Log file not found"}

    content = LOG_FILE.read_text(errors="replace")
    starts   = re.findall(r"=== Backup started: (.+?) ===",  content)
    finishes = re.findall(r"=== Backup finished: (.+?) ===", content)
    errors   = re.findall(r"^(ERROR:.+)$", content, re.MULTILINE)

    if not starts:
        return {"status": "unknown", "message": "No backups recorded yet", "total_runs": 0}

    last_start = starts[-1].strip()
    result = {
        "started": last_start, "finished": None,
        "duration": None, "status": "unknown",
        "errors": [], "total_runs": len(starts),
    }

    if LOCKFILE.exists():
        try:
            pid = int(LOCKFILE.read_text().strip())
            Path(f"/proc/{pid}").stat()
            result["status"] = "running"
            return result
        except (ValueError, OSError):
            pass

    if finishes:
        last_finish = finishes[-1].strip()
        result["finished"] = last_finish
        try:
            delta = (
                datetime.strptime(last_finish, "%a %b %d %H:%M:%S %Z %Y")
                - datetime.strptime(last_start,  "%a %b %d %H:%M:%S %Z %Y")
            )
            m, s = divmod(int(delta.total_seconds()), 60)
            result["duration"] = f"{m}m {s}s" if m else f"{s}s"
        except ValueError:
            pass
        idx = content.rfind(f"=== Backup started: {last_start}")
        session_errors = re.findall(r"^(ERROR:.+)$", content[idx:], re.MULTILINE)
        result["errors"] = session_errors[-3:]
        if session_errors:
            result["status"] = "error"
        else:
            # Check if the last successful backup is more than 3 days old
            try:
                last_dt = datetime.strptime(last_finish, "%a %b %d %H:%M:%S %Z %Y")
                age_days = (datetime.now() - last_dt).days
                if age_days >= 3:
                    result["status"] = "stale"
                    result["stale_days"] = age_days
                else:
                    result["status"] = "success"
            except ValueError:
                result["status"] = "success"
    else:
        result["status"] = "error"
        result["errors"] = ["Backup started but never finished (possible crash)"]

    return result


def get_borg_info():
    if not BORG_REPO.exists():
        return None
    out, rc = _run(["borg", "info", "--json", str(BORG_REPO)])
    if rc != 0:
        return None
    try:
        stats = json.loads(out)["cache"]["stats"]

        def fmt(n):
            for u in ["B", "KB", "MB", "GB", "TB"]:
                if n < 1024:
                    return f"{n:.1f}\u202f{u}"
                n /= 1024
            return f"{n:.1f}\u202fPB"

        orig  = stats.get("total_size", 0)
        stored = stats.get("unique_csize", 0)
        savings = (1 - stored / orig) * 100 if orig > 0 else 0
        return {"original": fmt(orig), "stored": fmt(stored), "savings": f"{savings:.0f}%"}
    except (KeyError, ValueError, json.JSONDecodeError):
        return None


def get_archives():
    if not BORG_REPO.exists():
        return []
    out, rc = _run(["borg", "list", "--json", str(BORG_REPO)])
    if rc != 0:
        return []
    try:
        result = []
        for a in reversed(json.loads(out).get("archives", [])):
            try:
                label = datetime.fromisoformat(a["start"]).strftime("%-d. %b %Y, %H:%M")
            except (ValueError, KeyError):
                label = a.get("start", "")
            result.append({"name": a.get("name", ""), "date": label})
        return result
    except (json.JSONDecodeError, KeyError):
        return []


def get_device():
    out, rc = _run(["idevice_id", "-l", "-n"], timeout=5)
    if rc == 0 and out.strip():
        udid = out.strip().split("\n")[0]
        name_out, name_rc = _run(["ideviceinfo", "-n", "-k", "DeviceName"], timeout=5)
        name = name_out.strip() if name_rc == 0 and name_out.strip() else None
        return {"reachable": True, "udid": udid, "name": name}
    return {"reachable": False}


def is_netmuxd_active():
    out, _ = _run(["systemctl", "is-active", "netmuxd"])
    return out.strip() == "active"


# ── Demo mode (--demo flag) ───────────────────────────────────────────────────
# Overrides all data-collection functions with realistic fake data so the UI
# can be previewed locally without a running server.

def _demo_data():
    from datetime import timedelta
    now = datetime.now()
    fmt = "%a %b %-d %H:%M:%S UTC %Y"

    log = {
        "status":     "success",
        "started":    (now - timedelta(hours=6, minutes=8)).strftime(fmt),
        "finished":   (now - timedelta(hours=6)).strftime(fmt),
        "duration":   "7m 43s",
        "errors":     [],
        "total_runs": 42,
    }
    borg = {
        "original": "48.3\u202fGB",
        "stored":   "13.1\u202fGB",
        "savings":  "73%",
    }
    archives = [
        {"name": f"2026-04-{16 - i:02d}_03:00", "date": f"{16 - i}. Apr 2026, 03:00"}
        for i in range(14)
    ]
    device  = {"reachable": True, "udid": "000001984-001A2B3C4D5E6F78", "name": "Your iPhone"}
    netmuxd = True
    return log, borg, archives, device, netmuxd


# ── Service Worker + Manifest ─────────────────────────────────────────────────

_SERVICE_WORKER = """\
self.addEventListener('push', event => {
    const d = event.data ? event.data.json() : {};
    event.waitUntil(self.registration.showNotification(
        d.title || 'iPhone Backup',
        { body: d.body || '', tag: 'backup', renotify: true,
          icon: '/icon.svg', data: { url: '/' } }
    ));
});
self.addEventListener('notificationclick', event => {
    event.notification.close();
    event.waitUntil(clients.openWindow(event.notification.data.url || '/'));
});
"""

_MANIFEST = json.dumps({
    "name": "iPhone Backup",
    "short_name": "Backup",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#000000",
    "theme_color": "#000000",
    "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
}, indent=2)

# Minimal iPhone-shaped icon
_ICON_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect width="100" height="100" rx="22" fill="#1c1c1e"/>
  <rect x="38" y="10" width="24" height="4" rx="2" fill="#3a3a3c"/>
  <rect x="18" y="20" width="64" height="56" rx="4" fill="#2c2c2e"/>
  <path d="M42 85 Q50 92 58 85" stroke="#30d158" stroke-width="3"
        fill="none" stroke-linecap="round"/>
  <path d="M34 54 L42 62 L58 46" stroke="#30d158" stroke-width="4"
        fill="none" stroke-linecap="round" stroke-linejoin="round"/>
</svg>"""


# ── HTML rendering ────────────────────────────────────────────────────────────

_CSS = """
:root{--bg:#000;--card:#1c1c1e;--sep:#38383a;--lbl:#8e8e93;--txt:#fff;
      --blue:#0a84ff;--green:#30d158;--red:#ff453a;--orange:#ff9f0a}
*{box-sizing:border-box;margin:0;padding:0}
html{background:var(--bg)}
body{background:var(--bg);color:var(--txt);
     font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display',
     'Helvetica Neue',system-ui,sans-serif;font-size:16px;line-height:1.5;
     max-width:640px;margin:0 auto;padding-bottom:52px;
     padding-left:max(20px,env(safe-area-inset-left));
     padding-right:max(20px,env(safe-area-inset-right));
     padding-top:env(safe-area-inset-top,0)}
header{padding:52px 0 4px}
header h1{font-size:34px;font-weight:700;letter-spacing:-.5px}
.header-sub{display:flex;align-items:center;flex-wrap:wrap;
            gap:8px;margin-top:4px}
.header-sub p{font-size:13px;color:var(--lbl)}

section{margin-top:32px}
.stitle{font-size:13px;font-weight:600;text-transform:uppercase;
        letter-spacing:.5px;color:var(--lbl);margin-bottom:8px;padding:0 2px}
.card{background:var(--card);border-radius:12px;overflow:hidden}

.hero{background:var(--card);border-radius:12px;padding:16px;
      display:flex;align-items:center;gap:16px}
.hero-icon{width:52px;height:52px;border-radius:50%;flex-shrink:0;
           display:flex;align-items:center;justify-content:center;
           font-size:22px;font-weight:700}
.hero-text h2{font-size:20px;font-weight:600}
.hero-text .meta{font-size:13px;color:var(--lbl);margin-top:2px}

.row{display:flex;justify-content:space-between;align-items:center;
     padding:12px 16px;border-bottom:1px solid var(--sep);gap:8px}
.row:last-child{border-bottom:none}
.rk{color:var(--lbl);font-size:15px;white-space:nowrap}
.rv{font-size:15px;font-weight:500;text-align:right}
.rv.green{color:var(--green)}.rv.blue{color:var(--blue)}

.badge{display:inline-block;font-size:13px;font-weight:600;
       padding:3px 10px;border-radius:20px}
.badge.green{background:rgba(48,209,88,.18);color:var(--green)}
.badge.red{background:rgba(255,69,58,.18);color:var(--red)}
.badge.gray{background:rgba(142,142,147,.18);color:var(--lbl)}

.arch{display:flex;justify-content:space-between;align-items:center;
      padding:11px 16px;border-bottom:1px solid var(--sep);gap:8px}
.arch:last-child{border-bottom:none}
.arch-n{font-family:'SF Mono','Menlo',monospace;font-size:13px;color:var(--blue)}
.arch-d{font-size:13px;color:var(--lbl);white-space:nowrap}

.errs{margin-top:10px;display:flex;flex-direction:column;gap:6px}
.err{font-size:13px;color:var(--red);background:rgba(255,69,58,.1);
     border-radius:8px;padding:8px 12px;
     font-family:'SF Mono','Menlo',monospace}
.empty{color:var(--lbl);font-size:14px;padding:16px;text-align:center}

/* notification button */
#notif-btn{display:none;align-items:center;gap:5px;
           background:rgba(10,132,255,.15);color:var(--blue);border:none;
           cursor:pointer;font-size:13px;font-weight:600;padding:4px 12px;
           border-radius:20px;font-family:inherit;white-space:nowrap}
#notif-btn.on{background:rgba(48,209,88,.15);color:var(--green)}
#notif-btn.blocked{background:rgba(142,142,147,.15);color:var(--lbl);cursor:default}
#notif-btn:disabled{opacity:.6;cursor:default}

footer{margin-top:48px;text-align:center;font-size:12px;color:var(--lbl)}
footer a{color:var(--lbl)}
"""

# Placeholders __VAPID_KEY__ and __PUSH_ENABLED__ are substituted at render time
# with plain str.replace() — no .format(), so JS braces need no escaping.
_JS = """
const VAPID_KEY = "__VAPID_KEY__";
const PUSH_ENABLED = __PUSH_ENABLED__;

function b64uToUint8(b64) {
    const pad = '='.repeat((4 - b64.length % 4) % 4);
    const raw = atob((b64 + pad).replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from(raw, c => c.charCodeAt(0));
}

async function initPush() {
    const btn = document.getElementById('notif-btn');
    if (!btn || !PUSH_ENABLED) return;
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
        btn.title = 'Web Push not supported in this browser'; return;
    }
    btn.style.display = 'inline-flex';

    let reg;
    try { reg = await navigator.serviceWorker.register('/sw.js'); }
    catch (e) { btn.textContent = 'SW Error'; btn.disabled = true; return; }

    await reg.update();
    const perm = Notification.permission;
    const existing = await reg.pushManager.getSubscription();

    if (perm === 'granted' && existing) {
        btn.textContent = '🔔 Enabled'; btn.classList.add('on'); btn.disabled = true; return;
    }
    if (perm === 'denied') {
        btn.textContent = 'Notifications Blocked'; btn.classList.add('blocked'); btn.disabled = true; return;
    }

    btn.textContent = 'Enable Notifications';
    btn.addEventListener('click', async () => {
        btn.textContent = '…'; btn.disabled = true;
        const p = await Notification.requestPermission();
        if (p !== 'granted') { btn.textContent = 'Blocked'; btn.classList.add('blocked'); return; }
        try {
            const sub = await reg.pushManager.subscribe({
                userVisibleOnly: true,
                applicationServerKey: b64uToUint8(VAPID_KEY)
            });
            await fetch('/subscribe', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(sub)
            });
            btn.textContent = '🔔 Enabled'; btn.classList.add('on'); btn.disabled = true;
        } catch (e) {
            btn.textContent = 'Error'; btn.disabled = false;
            console.error('Push subscribe failed:', e);
        }
    });
}

document.addEventListener('DOMContentLoaded', initPush);
"""

_STATUS_META = {
    "success": ("#30d158", "Success", "✓"),
    "error":   ("#ff453a", "Failed",  "✕"),
    "running": ("#ff9f0a", "Running", "↻"),
    "stale":   ("#ff9f0a", "Overdue", "!"),
    "unknown": ("#8e8e93", "Unknown", "?"),
}


def _badge(ok, t="Active", f="Inactive"):
    return f'<span class="badge {"green" if ok else "red"}">{t if ok else f}</span>'


def render(log, borg, archives, device, netmuxd):
    color, label, icon = _STATUS_META.get(log["status"], _STATUS_META["unknown"])
    now = datetime.now().strftime("%-d. %b %Y, %H:%M:%S")

    meta = "".join(
        f'<div class="meta">{l}</div>'
        for l in filter(None, [
            log.get("started"),
            f"Duration: {log['duration']}" if log.get("duration") else None,
            f"Last backup was {log['stale_days']} days ago" if log.get("stale_days") else None,
            log.get("message"),
        ])
    )
    errors_html = (
        '<div class="errs">'
        + "".join(f'<div class="err">{e}</div>' for e in log.get("errors", []))
        + "</div>"
    ) if log.get("errors") else ""

    storage_inner = (
        f'<div class="row"><span class="rk">Original size</span>'
        f'<span class="rv">{borg["original"]}</span></div>'
        f'<div class="row"><span class="rk">Stored (deduplicated)</span>'
        f'<span class="rv">{borg["stored"]}</span></div>'
        f'<div class="row"><span class="rk">Space saved</span>'
        f'<span class="rv green">{borg["savings"]}</span></div>'
        f'<div class="row"><span class="rk">Snapshots</span>'
        f'<span class="rv">{len(archives)}</span></div>'
    ) if borg else '<div class="empty">No Borg data available</div>'

    name_row = (
        f'<div class="row"><span class="rk">Device</span>'
        f'<span class="rv">{device["name"]}</span></div>'
    ) if device.get("name") else ""
    udid_row = (
        f'<div class="row"><span class="rk">UDID</span>'
        f'<span class="rv blue" style="font-family:monospace;font-size:12px">'
        f'{device["udid"]}</span></div>'
    ) if device.get("udid") else ""

    archives_inner = "".join(
        f'<div class="arch"><span class="arch-n">{a["name"]}</span>'
        f'<span class="arch-d">{a["date"]}</span></div>'
        for a in archives
    ) if archives else '<div class="empty">No snapshots yet</div>'

    push_enabled_js = "true" if (PUSH_AVAILABLE and _vapid_private is not None and USE_HTTPS) else "false"
    js = _JS.replace("__VAPID_KEY__", _vapid_public_b64()).replace("__PUSH_ENABLED__", push_enabled_js)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <meta name="color-scheme" content="dark">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black">
  <meta name="apple-mobile-web-app-title" content="iPhone Backup">
  <meta http-equiv="refresh" content="60">
  <link rel="manifest" href="/manifest.json">
  <title>iPhone Backup</title>
  <style>{_CSS}</style>
</head>
<body>
<header>
  <h1>iPhone Backup</h1>
  <div class="header-sub">
    <p>Updated {now} &middot; auto-refresh 60&thinsp;s</p>
    <button id="notif-btn" title="Requires adding this page to your Home Screen"></button>
  </div>
</header>

<section>
  <div class="stitle">Last Backup</div>
  <div class="hero">
    <div class="hero-icon" style="background:{color}22;color:{color}">{icon}</div>
    <div class="hero-text">
      <h2 style="color:{color}">{label}</h2>
      {meta}
    </div>
  </div>
  {errors_html}
</section>

<section>
  <div class="stitle">Storage</div>
  <div class="card">{storage_inner}</div>
</section>

<section>
  <div class="stitle">System</div>
  <div class="card">
    <div class="row"><span class="rk">iPhone</span>
      <span class="rv">{_badge(device["reachable"], "Reachable", "Offline")}</span></div>
    {name_row}
    {udid_row}
    <div class="row"><span class="rk">netmuxd</span>
      <span class="rv">{_badge(netmuxd, "Running", "Stopped")}</span></div>
    <div class="row"><span class="rk">Backups run</span>
      <span class="rv">{log.get("total_runs", 0)}</span></div>
  </div>
</section>

<section>
  <div class="stitle">Snapshots</div>
  <div class="card">{archives_inner}</div>
</section>

<footer>
  iphone-backup-server &middot;
  <a href="/cert.pem">Download TLS certificate</a>
</footer>

<script>{js}</script>
</body>
</html>"""


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    # ── GET ───────────────────────────────────────────────────────────────────
    def do_GET(self):
        routes = {
            "/sw.js":           ("application/javascript",        _SERVICE_WORKER.encode()),
            "/manifest.json":   ("application/manifest+json",     _MANIFEST.encode()),
            "/icon.svg":        ("image/svg+xml",                  _ICON_SVG.encode()),
            "/vapid-public-key":("text/plain",                     _vapid_public_b64().encode()),
        }

        if self.path in ("/", "/index.html"):
            html = render(
                parse_log(), get_borg_info(), get_archives(),
                get_device(), is_netmuxd_active()
            )
            self._respond(200, "text/html; charset=utf-8", html.encode())

        elif self.path == "/cert.pem":
            if SSL_CERT.exists():
                self._respond(200, "application/x-pem-file", SSL_CERT.read_bytes(),
                              extra_headers={"Content-Disposition":
                                             'attachment; filename="iphone-backup.pem"'})
            else:
                self._respond(404, "text/plain", b"No certificate found")

        elif self.path in routes:
            ct, body = routes[self.path]
            self._respond(200, ct, body)

        else:
            self._respond(404, "text/plain", b"Not found")

    # ── POST ──────────────────────────────────────────────────────────────────
    def do_POST(self):
        body = self._read_body()

        if self.path == "/subscribe":
            if not PUSH_AVAILABLE:
                self._respond(503, "text/plain", b"python3-cryptography not installed")
                return
            try:
                add_subscription(json.loads(body))
                self._respond(200, "text/plain", b"OK")
            except (json.JSONDecodeError, KeyError):
                self._respond(400, "text/plain", b"Invalid subscription JSON")

        elif self.path == "/unsubscribe":
            try:
                remove_subscription(json.loads(body).get("endpoint", ""))
                self._respond(200, "text/plain", b"OK")
            except (json.JSONDecodeError, KeyError):
                self._respond(400, "text/plain", b"Invalid JSON")

        elif self.path == "/notify":
            # Only callable from localhost — do not expose this endpoint externally
            if self.client_address[0] not in ("127.0.0.1", "::1", "localhost"):
                self._respond(403, "text/plain", b"Forbidden")
                return
            try:
                data = json.loads(body)
                notify_all(data.get("title", "iPhone Backup"), data.get("body", ""))
                self._respond(200, "text/plain", b"OK")
            except (json.JSONDecodeError, KeyError):
                self._respond(400, "text/plain", b"Invalid JSON")

        else:
            self._respond(404, "text/plain", b"Not found")

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _respond(self, status: int, content_type: str, body: bytes,
                 extra_headers: dict = None):
        self.send_response(status)
        self.send_header("Content-Type",   content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control",  "no-cache")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import sys
    demo = "--demo" in sys.argv

    if demo:
        # In demo mode: patch data collectors, run plain HTTP on localhost:8080
        _demo_log, _demo_borg, _demo_archives, _demo_device, _demo_netmuxd = _demo_data()

        # Monkey-patch the five data functions so the handler uses fake data
        global parse_log, get_borg_info, get_archives, get_device, is_netmuxd_active
        parse_log         = lambda: _demo_log
        get_borg_info     = lambda: _demo_borg
        get_archives      = lambda: _demo_archives
        get_device        = lambda: _demo_device
        is_netmuxd_active = lambda: _demo_netmuxd

        demo_port = 8080
        server = ThreadingHTTPServer(("127.0.0.1", demo_port), Handler)
        print(f"Demo mode — open http://127.0.0.1:{demo_port} in your browser")
        print("Ctrl+C to stop.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
        return

    _init_vapid()

    server = ThreadingHTTPServer((HOST, PORT), Handler)

    if USE_HTTPS:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(SSL_CERT), str(SSL_KEY))
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    else:
        scheme = "http"
        if PUSH_AVAILABLE:
            print(f"WARNING: No SSL certificate found at {SSL_CERT}.")
            print("         Web Push requires HTTPS. Run install.sh to generate a cert.")

    print(f"Status server: {scheme}://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
