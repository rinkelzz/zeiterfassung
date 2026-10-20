"""Anmeldung mit DSM-Benutzern.

Zwei Wege:
1. Single Sign-on im DSM-Fenster: ui/app.cgi (läuft im Webserver von DSM) ermittelt den
   angemeldeten DSM-Benutzer und schickt ihn zusammen mit einem Geheimnis (Proxy-Schlüssel)
   an die App. Nur wer den Schlüssel kennt, kann so einen Benutzer „ansagen“.
2. Login-Formular: Benutzername/Passwort werden bei DSM geprüft (SYNO.API.Auth), z. B.
   am Handy über den Reverse Proxy. Zwei-Faktor-Codes werden unterstützt.
"""
import hmac
import json
import os
import secrets
import sqlite3
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Tuple

from .config import settings
from .db import audit, get_setting
from .timeutil import now_utc_str, today_local

USERNAME_MAX = 64


# --- Proxy-Schlüssel ---------------------------------------------------------

_secret: Optional[str] = None


def proxy_secret() -> str:
    global _secret
    if _secret is None:
        path = settings.data_dir / "proxy.secret"
        try:
            _secret = path.read_text(encoding="utf-8").strip()
        except OSError:
            _secret = ""
        if len(_secret) < 32:
            _secret = secrets.token_urlsafe(32)
            path.write_text(_secret, encoding="utf-8")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
    return _secret


def publish_proxy_files() -> bool:
    """Schlüssel und Port für ui/app.cgi neben das Paket legen (Paketordner, nicht im Web-Ordner)."""
    if not settings.ui_dir:
        return False
    target = Path(settings.ui_dir).parent
    try:
        for name, content in ((".proxy_secret", proxy_secret()), (".port", str(settings.port))):
            p = target / name
            tmp = p.with_name(name + ".tmp")
            tmp.write_text(content, encoding="utf-8")
            os.chmod(tmp, 0o644)  # der DSM-Webserver muss es lesen können
            tmp.replace(p)
        return True
    except OSError as e:
        print(f"[zeiterfassung] Proxy-Dateien nicht schreibbar: {e}")
        return False


def proxy_info(request) -> Optional[Dict]:
    """Prüft, ob die Anfrage über ui/app.cgi kam. Liefert {user, admin, base} oder None."""
    sent = request.headers.get("x-ze-proxy-secret")
    if not sent or not hmac.compare_digest(sent, proxy_secret()):
        return None
    user = urllib.parse.unquote(request.headers.get("x-dsm-user", "")).strip()
    return {
        "user": user[:USERNAME_MAX] if user else "",
        "admin": request.headers.get("x-dsm-admin") == "1",
        "base": request.headers.get("x-ze-proxy-base", "app.cgi"),
    }


# --- Benutzer zuordnen -------------------------------------------------------

def _flag(conn: sqlite3.Connection, key: str) -> bool:
    return get_setting(conn, key) == "1"


def dsm_user_row(conn: sqlite3.Connection, username: str, dsm_admin: bool) -> Tuple[Optional[sqlite3.Row], str]:
    """Findet oder erstellt den Benutzer zum DSM-Konto. Liefert (Zeile, Fehlertext)."""
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if row and not row["active"]:
        return None, "Dein Zugang zur Zeiterfassung ist deaktiviert."
    make_admin = dsm_admin and _flag(conn, "dsm_admins")
    if not row:
        if not _flag(conn, "dsm_auto_create"):
            return None, f"Für das DSM-Konto „{username}“ gibt es noch keinen Zugang zur Zeiterfassung. Bitte beim Admin melden."
        now = now_utc_str()
        cur = conn.execute(
            "INSERT INTO users(username, display_name, password_hash, is_admin, created_at, tracking_start) "
            "VALUES (?, ?, NULL, ?, ?, ?)",
            (username, username, int(make_admin), now, today_local().isoformat()),
        )
        audit(conn, None, "user_created_dsm", f"user:{cur.lastrowid}", f"{username}{' (Admin)' if make_admin else ''}")
        row = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
    elif make_admin and not row["is_admin"]:
        conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (row["id"],))
        audit(conn, None, "user_admin_dsm", f"user:{row['id']}", "DSM-Administrator")
        row = conn.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()
    return row, ""


def is_dsm_admin(username: str) -> bool:
    try:
        res = subprocess.run(["id", "-Gn", username], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        if "administrators" in res.stdout.decode("utf-8", "replace").split():
            return True
        res = subprocess.run(["id", "-G", username], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        return "101" in res.stdout.decode().split()
    except (OSError, subprocess.SubprocessError):
        return False


# --- Passwortprüfung bei DSM -------------------------------------------------

def dsm_password_check(conn: sqlite3.Connection, username: str, password: str, otp: str = "") -> str:
    """Fragt DSM, ob die Zugangsdaten stimmen.
    Ergebnis: ok | wrong | otp | blocked | unreachable"""
    base = (get_setting(conn, "dsm_url") or "http://127.0.0.1:5000").rstrip("/")
    form = {
        "api": "SYNO.API.Auth", "version": "6", "method": "login",
        "account": username, "passwd": password, "session": "Zeiterfassung", "format": "sid",
    }
    if otp:
        form["otp_code"] = otp
    data = urllib.parse.urlencode(form).encode()
    ctx = ssl.create_default_context()
    if base.startswith("https://127.0.0.1") or base.startswith("https://localhost"):
        ctx.check_hostname = False  # eigenes Zertifikat auf der NAS selbst
        ctx.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(base + "/webapi/entry.cgi", data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10, context=ctx if base.startswith("https") else None) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
    except (OSError, ValueError, urllib.error.URLError):
        return "unreachable"
    if body.get("success"):
        sid = (body.get("data") or {}).get("sid")
        if sid:  # Sitzung bei DSM gleich wieder schließen
            try:
                q = urllib.parse.urlencode({"api": "SYNO.API.Auth", "version": "6", "method": "logout",
                                            "session": "Zeiterfassung", "_sid": sid})
                urllib.request.urlopen(base + "/webapi/entry.cgi?" + q, timeout=5,
                                       context=ctx if base.startswith("https") else None).read()
            except (OSError, urllib.error.URLError):
                pass
        return "ok"
    code = (body.get("error") or {}).get("code")
    if code in (403, 406):          # 2FA-Code nötig / 2FA erzwungen
        return "otp"
    if code == 404:                 # 2FA-Code falsch
        return "otp_wrong"
    if code in (407,):              # IP gesperrt
        return "blocked"
    return "wrong"                  # 400 falsches Passwort, 401/402 Konto deaktiviert/keine Berechtigung


# --- Schutz vor Durchprobieren ----------------------------------------------

_fails: Dict[str, list] = {}
_lock = threading.Lock()
MAX_FAILS, WINDOW_S = 5, 15 * 60


def too_many_attempts(key: str) -> bool:
    now = time.time()
    with _lock:
        recent = [t for t in _fails.get(key, []) if now - t < WINDOW_S]
        _fails[key] = recent
        return len(recent) >= MAX_FAILS


def note_failure(key: str) -> None:
    with _lock:
        _fails.setdefault(key, []).append(time.time())


def clear_failures(key: str) -> None:
    with _lock:
        _fails.pop(key, None)
