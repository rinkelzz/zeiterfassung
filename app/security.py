"""Passwörter, Sessions und API-Tokens – nur Standardbibliothek."""
import hashlib
import hmac
import secrets
import sqlite3
from datetime import timedelta
from typing import Optional

from fastapi import Depends, HTTPException, Request, status

from .config import settings
from .db import get_db
from .timeutil import now_utc, now_utc_str, to_utc_str

SESSION_COOKIE = "ze_session"
SESSION_HEADER = "x-session-token"  # für das DSM-Fenster (iframe), wo Browser Cookies oft blocken
TOKEN_PREFIX = "ze_"
_PBKDF2_ITERATIONS = 200_000


# --- Passwörter -------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: Optional[str]) -> bool:
    if not stored:
        return False
    try:
        _algo, iterations, salt, expected = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations))
        return hmac.compare_digest(digest.hex(), expected)
    except ValueError:
        return False


# --- Tokens (Sessions und API) ---------------------------------------------

def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_api_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def create_api_token(conn: sqlite3.Connection, user_id: int, label: str) -> str:
    """Erzeugt ein Token; im Klartext gibt es das nur dieses eine Mal."""
    token = new_api_token()
    conn.execute(
        "INSERT INTO api_tokens(user_id, token_hash, label, created_at) VALUES (?, ?, ?, ?)",
        (user_id, _sha256(token), label, now_utc_str()),
    )
    return token


def create_session(conn: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = now_utc() + timedelta(days=settings.session_days)
    conn.execute(
        "INSERT INTO sessions(token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (_sha256(token), user_id, now_utc_str(), to_utc_str(expires)),
    )
    return token


def delete_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_sha256(token),))


# --- Dependencies -----------------------------------------------------------

def _user_from_session(conn: sqlite3.Connection, token: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ? AND s.expires_at > ? AND u.active = 1",
        (_sha256(token), now_utc_str()),
    ).fetchone()


def _user_from_api_token(conn: sqlite3.Connection, token: str) -> Optional[sqlite3.Row]:
    token_hash = _sha256(token)
    row = conn.execute(
        "SELECT u.* FROM api_tokens t JOIN users u ON u.id = t.user_id "
        "WHERE t.token_hash = ? AND u.active = 1",
        (token_hash,),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE api_tokens SET last_used_at = ? WHERE token_hash = ?", (now_utc_str(), token_hash)
        )
    return row


# Schlüssel für Kurzbefehle/NFC dürfen nur stempeln (plus die zwei Abfragen der Einrichtungsseite).
# Geht ein Aufkleber oder Handy verloren, kommt damit niemand an Konten, Berichte oder die Verwaltung.
TOKEN_PREFIXES = ("/api/v1/clock/",)
TOKEN_GET_PATHS = ("/api/v1/me", "/api/v1/settings")


def token_allowed(request: Request) -> bool:
    path = request.url.path
    root = settings.root_path
    if root and path.startswith(root + "/"):
        path = path[len(root):]
    if path.startswith(TOKEN_PREFIXES):
        return True
    return request.method in ("GET", "HEAD") and path.rstrip("/") in TOKEN_GET_PATHS


def current_user(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> sqlite3.Row:
    """Login per Session (Cookie oder X-Session-Token-Header, Weboberfläche) oder
    Bearer-Token (Kurzbefehle, NFC, Kiosk)."""
    # 1) Anfrage aus dem DSM-Fenster: DSM hat den Benutzer schon geprüft (Single Sign-on)
    from .dsm_auth import dsm_user_row, proxy_info  # spät importiert, vermeidet Zyklus
    proxied = proxy_info(request)
    request.state.via_dsm = False
    request.state.via_token = False
    if proxied and proxied["user"]:
        user, error = dsm_user_row(conn, proxied["user"], proxied["admin"])
        if not user:
            raise HTTPException(status.HTTP_403_FORBIDDEN, error)
        request.state.via_dsm = True
        return user
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        if not token_allowed(request):
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "Dieser Schlüssel (Kurzbefehl/NFC) gilt nur zum Stempeln.")
        user = _user_from_api_token(conn, auth[7:].strip())
        request.state.via_token = True
    else:
        token = request.headers.get(SESSION_HEADER) or request.cookies.get(SESSION_COOKIE)
        user = _user_from_session(conn, token) if token else None
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Nicht angemeldet")
    return user


def admin_user(user: sqlite3.Row = Depends(current_user)) -> sqlite3.Row:
    if not user["is_admin"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Nur für Administratoren")
    return user
