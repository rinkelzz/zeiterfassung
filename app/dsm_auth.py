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


LOOPBACK = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})


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
    if not sent:
        return None
    # app.cgi läuft auf der NAS und verbindet sich immer über 127.0.0.1 – von woanders zählt der Schlüssel nicht
    peer = request.scope.get("ze_peer", request.client.host if request.client else "")
    # Über den Reverse Proxy weitergereichte Anfragen kommen zwar auch von 127.0.0.1, tragen aber X-Forwarded-For
    forwarded = any(request.headers.get(h) for h in ("x-forwarded-for", "x-real-ip", "forwarded"))
    if peer not in LOOPBACK or forwarded or not hmac.compare_digest(sent, proxy_secret()):
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

DEFAULT_DSM_URL = "http://127.0.0.1:5000"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keine Weiterleitungen: DSM antwortet auf entry.cgi nie mit 3xx – alles andere gilt als „nicht erreichbar“."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "Weiterleitung abgelehnt", headers, fp)


def _dsm_opener(ctx: Optional[ssl.SSLContext]) -> urllib.request.OpenerDirector:
    """HTTP-Client nur für die DSM-Passwortprüfung: ohne Proxy (http_proxy/https_proxy werden ignoriert,
    die Anfrage geht immer direkt an die NAS) und ohne Weiterleitungen."""
    handlers = [urllib.request.ProxyHandler({}), _NoRedirect()]
    if ctx is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def local_dsm_url(url: str) -> Optional[str]:
    """Bereinigte DSM-Adresse, wenn sie auf die NAS selbst zeigt (127.0.0.1, localhost, ::1) – sonst None.

    Die Passwörter der Mitarbeiter gehen an diese Adresse. Ein anderer Rechner ist deshalb nie erlaubt:
    sonst könnte ein Admin der Zeiterfassung DSM-Passwörter auf einen eigenen Server umleiten, oder sie
    gingen unverschlüsselt durchs Netz. Verglichen wird der echte Hostname, nicht der Anfang der Adresse
    (https://localhost.example.org ist kein localhost)."""
    url = (url or "").strip().rstrip("/")
    if any(ord(c) <= 32 or c in "\\" for c in url):
        return None
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port  # wirft ValueError bei Unsinn
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or parts.username or parts.password:
        return None
    if parts.path or parts.query or parts.fragment:
        return None
    if (parts.hostname or "").lower() not in LOOPBACK_HOSTS:
        return None
    if port is not None and not 1 <= port <= 65535:
        return None
    return url

def dsm_password_check(conn: sqlite3.Connection, username: str, password: str, otp: str = "") -> str:
    """Fragt DSM, ob die Zugangsdaten stimmen.
    Ergebnis: ok | wrong | otp | blocked | unreachable"""
    # Nur Adressen auf der NAS selbst – auch ein alter oder von Hand eingetragener Wert wird hier nochmals geprüft.
    base = local_dsm_url(get_setting(conn, "dsm_url") or DEFAULT_DSM_URL) or DEFAULT_DSM_URL
    form = {
        "api": "SYNO.API.Auth", "version": "6", "method": "login",
        "account": username, "passwd": password, "session": "Zeiterfassung", "format": "sid",
    }
    if otp:
        form["otp_code"] = otp
    data = urllib.parse.urlencode(form).encode()
    ctx = ssl.create_default_context()
    if base.startswith("https://"):
        # Die Verbindung bleibt auf der NAS (Loopback, siehe local_dsm_url) und geht nie übers Netz.
        # DSM nutzt dort meist ein selbst signiertes Zertifikat, das nicht auf 127.0.0.1 ausgestellt ist.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    opener = _dsm_opener(ctx if base.startswith("https") else None)
    try:
        req = urllib.request.Request(base + "/webapi/entry.cgi", data=data, method="POST")
        with opener.open(req, timeout=10) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
    except (OSError, ValueError, urllib.error.URLError):
        return "unreachable"
    if body.get("success"):
        sid = (body.get("data") or {}).get("sid")
        if sid:  # Sitzung bei DSM gleich wieder schließen
            try:
                q = urllib.parse.urlencode({"api": "SYNO.API.Auth", "version": "6", "method": "logout",
                                            "session": "Zeiterfassung", "_sid": sid})
                with opener.open(base + "/webapi/entry.cgi?" + q, timeout=5) as r:
                    r.read()
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
# Zusätzlich pro Benutzername, egal von welcher Adresse (gegen verteiltes Durchprobieren)
MAX_FAILS_PER_NAME = 10


def too_many_attempts(key: str, limit: int = MAX_FAILS) -> bool:
    now = time.time()
    with _lock:
        recent = [t for t in _fails.get(key, []) if now - t < WINDOW_S]
        _fails[key] = recent
        return len(recent) >= limit


MAX_KEYS = 50000  # Obergrenze, damit viele verschiedene Namen den Speicher nicht füllen (≈ wenige MB)


def _prune(now: float) -> None:
    """Abgelaufene Einträge entfernen; ist es danach immer noch zu viel, die ältesten verwerfen."""
    for k in [k for k, v in _fails.items() if not v or now - v[-1] >= WINDOW_S]:
        del _fails[k]
    if len(_fails) > MAX_KEYS:
        for k in sorted(_fails, key=lambda k: _fails[k][-1])[: len(_fails) - MAX_KEYS]:
            del _fails[k]


def note_failure(key: str) -> None:
    now = time.time()
    with _lock:
        if len(_fails) >= MAX_KEYS:
            _prune(now)
        _fails.setdefault(key, []).append(now)


def clear_failures(key: str) -> None:
    with _lock:
        _fails.pop(key, None)
