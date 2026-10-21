"""Login, Logout, eigenes Profil, eigene API-Tokens und Einrichtungs-QR-Code."""
import io
import json
import sqlite3
from typing import Optional

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..config import settings
from ..dsm_auth import (
    clear_failures,
    proxy_info,
    dsm_password_check,
    dsm_user_row,
    is_dsm_admin,
    note_failure,
    too_many_attempts,
    MAX_FAILS_PER_NAME,
)
from ..db import audit, get_db, get_setting
from ..security import (
    SESSION_COOKIE,
    SESSION_HEADER,
    create_api_token,
    create_session,
    current_user,
    delete_session,
    hash_password,
    verify_password,
)
from ..timeutil import local_iso

router = APIRouter(prefix="/api/v1", tags=["Anmeldung & Profil"])


class LoginIn(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)
    otp_code: Optional[str] = Field(None, max_length=12, description="Zwei-Faktor-Code des DSM-Kontos")


class PasswordIn(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8)


class TokenIn(BaseModel):
    label: str = Field("iPhone Kurzbefehl", max_length=60)
    # Im DSM-Fenster: Hostname aus der Browserzeile (z. B. 192.168.1.10), falls keine
    # öffentliche Adresse eingetragen ist – damit das iPhone die App im WLAN findet
    host_hint: Optional[str] = Field(None, max_length=253, pattern=r"^[A-Za-z0-9.\-:\[\]]+$")


def user_out(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "is_admin": bool(row["is_admin"]),
        "active": bool(row["active"]),
        "weekly_hours": row["weekly_hours"],
        "workdays": [int(d) for d in row["workdays"].split(",") if d],
        "tracking_start": row["tracking_start"],
        "carryover_minutes": row["carryover_minutes"],
        "carryover_overtime_minutes": row["carryover_overtime_minutes"],
        "vacation_days_per_year": row["vacation_days_per_year"],
        "carryover_vacation": row["carryover_vacation"],
        "has_password": bool(row["password_hash"]),
        "email": row["email"] if "email" in row.keys() else None,
        "report_mail": bool(row["report_mail"]) if "report_mail" in row.keys() else True,
    }


@router.post("/auth/login")
def login(data: LoginIn, request: Request, response: Response, conn: sqlite3.Connection = Depends(get_db)):
    username = data.username.strip()
    # Sperre pro Name+Adresse (5 Fehlversuche) und pro Name insgesamt (10), je 15 Minuten.
    # Die Adresse kommt hinter dem Reverse Proxy aus X-Forwarded-For – ausgewertet nur, wenn die
    # Anfrage vom Proxy auf der NAS selbst kommt (siehe main.py), sonst ist sie nicht fälschbar.
    key = f"{username.lower()}|{request.client.host if request.client else ''}"
    name_key = f"{username.lower()}|*"
    if too_many_attempts(key) or too_many_attempts(name_key, MAX_FAILS_PER_NAME):
        raise HTTPException(429, "Zu viele Fehlversuche – bitte in 15 Minuten erneut versuchen")
    user = conn.execute("SELECT * FROM users WHERE username = ? AND active = 1", (username,)).fetchone()
    via = "local"
    if not (user and verify_password(data.password, user["password_hash"])):
        user = None
        # Zweiter Weg: Zugangsdaten des DSM-Kontos
        if get_setting(conn, "dsm_login") == "1" and username and data.password:
            result = dsm_password_check(conn, username, data.password, (data.otp_code or "").strip())
            if result == "otp":
                raise HTTPException(401, "otp_required")
            if result == "otp_wrong":
                note_failure(key)
                note_failure(name_key)
                raise HTTPException(401, "Zwei-Faktor-Code falsch")
            if result == "ok":
                user, error = dsm_user_row(conn, username, is_dsm_admin(username))
                if not user:
                    raise HTTPException(403, error)
                via = "dsm"
        if not user:
            note_failure(key)
            note_failure(name_key)
            raise HTTPException(401, "Benutzername oder Passwort falsch")
    clear_failures(key)
    token = create_session(conn, user["id"])
    https = settings.cookie_secure or request.url.scheme == "https"
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        # über https darf der Cookie auch im DSM-Fenster (iframe) mitgehen
        samesite="none" if https else "lax",
        # https über den Reverse Proxy → Secure; im Heimnetz per http klappt der Login trotzdem
        secure=https,
        max_age=settings.session_days * 86400,
        path=settings.root_path or "/",
    )
    audit(conn, user["id"], "login", "", via)
    # Zusätzlich als Wert für den Header X-Session-Token (DSM-Fenster, Safari)
    return {**user_out(user), "session_token": token}


@router.post("/auth/logout")
def logout(request: Request, response: Response, conn: sqlite3.Connection = Depends(get_db)):
    token = request.headers.get(SESSION_HEADER) or request.cookies.get(SESSION_COOKIE)
    if token:
        delete_session(conn, token)
    response.delete_cookie(SESSION_COOKIE, path=settings.root_path or "/")
    return {"ok": True}


@router.get("/me")
def me(request: Request, user=Depends(current_user)):
    return {**user_out(user), "via_dsm": bool(getattr(request.state, "via_dsm", False))}


class ProfileIn(BaseModel):
    email: Optional[str] = Field(None, max_length=200, description="für Berichte per Mail, leer = keine Mails")
    report_mail: Optional[bool] = Field(None, description="Monats-/Jahresberichte automatisch per Mail")


@router.patch("/me")
def update_me(data: ProfileIn, request: Request, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Eigene Mailadresse und Mail-Wunsch ändern."""
    from ..mailer import valid_email
    changes = data.model_dump(exclude_unset=True)
    if "email" in changes:
        addr = (data.email or "").strip()
        if addr and not valid_email(addr):
            raise HTTPException(400, "Ungültige E-Mail-Adresse")
        conn.execute("UPDATE users SET email = ? WHERE id = ?", (addr or None, user["id"]))
    if data.report_mail is not None:
        conn.execute("UPDATE users SET report_mail = ? WHERE id = ?", (int(data.report_mail), user["id"]))
    if changes:
        audit(conn, user["id"], "profile_updated", f"user:{user['id']}", ",".join(sorted(changes)))
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    return {**user_out(row), "via_dsm": bool(getattr(request.state, "via_dsm", False))}


@router.post("/me/password")
def change_password(data: PasswordIn, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    if not user["password_hash"]:
        raise HTTPException(400, "Du meldest dich mit deinem DSM-Konto an – das Passwort änderst du in DSM.")
    if not verify_password(data.old_password, user["password_hash"]):
        raise HTTPException(400, "Altes Passwort stimmt nicht")
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(data.new_password), user["id"]))
    # alle anderen Sessions beenden
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
    audit(conn, user["id"], "password_changed", f"user:{user['id']}")
    return {"ok": True}


# --- API-Tokens & QR-Code -------------------------------------------------

def list_tokens(conn: sqlite3.Connection, user_id: int) -> list:
    rows = conn.execute(
        "SELECT id, label, created_at, last_used_at FROM api_tokens WHERE user_id = ? ORDER BY id",
        (user_id,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "label": r["label"],
            "created_at": local_iso(r["created_at"]),
            "last_used_at": local_iso(r["last_used_at"]),
        }
        for r in rows
    ]


def server_url(request: Request, conn: sqlite3.Connection, host_hint: Optional[str] = None) -> str:
    """Adresse für QR-Code und Kurzbefehle: Einstellung in der App > config.env > aufgerufene Adresse.
    Im DSM-Fenster kommt die Anfrage über die DSM-Weiterleitung von 127.0.0.1 – dann zählt der
    Hostname aus dem Browser plus der Port der App."""
    configured = get_setting(conn, "public_url") or settings.public_url
    if configured:
        return configured.rstrip("/") + ("" if get_setting(conn, "public_url") else settings.root_path)
    if proxy_info(request) is not None:
        host = (host_hint or "").strip("[]") or "localhost"
        if ":" in host:  # IPv6
            host = f"[{host}]"
        return f"http://{host}:{settings.port}{settings.root_path}"
    return str(request.base_url).rstrip("/")  # enthält root_path bereits


def setup_link(server: str, token: str) -> str:
    """Persönlicher Einrichtungslink. Das Token steht hinter # und wird so nie an Server oder Proxy geschickt."""
    return f"{server}/setup#t={token}"


def setup_payload(user: sqlite3.Row, token: str, server: str) -> dict:
    """Zugangsdaten für Kurzbefehle (Adresse, Benutzer, Token)."""
    return {
        "app": "zeiterfassung",
        "v": 1,
        "server": server,
        "user": user["username"],
        "token": token,
    }


def qr_svg(text: str) -> str:
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()


def issue_token_with_qr(conn: sqlite3.Connection, user: sqlite3.Row, data: "TokenIn", actor_id: int, request: Request) -> dict:
    token = create_api_token(conn, user["id"], data.label)
    server = server_url(request, conn, data.host_hint)
    payload = setup_payload(user, token, server)
    link = setup_link(server, token)
    audit(conn, actor_id, "token_created", f"user:{user['id']}", data.label)
    return {
        "token": token,
        "label": data.label,
        "setup": payload,
        "setup_url": link,
        "server_is_public": bool(get_setting(conn, "public_url") or settings.public_url),
        "qr_svg": qr_svg(link),
        "hinweis": "QR-Code mit der iPhone-Kamera scannen – es öffnet sich die persönliche Einrichtungsseite.",
    }


@router.get("/me/tokens")
def my_tokens(user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    return list_tokens(conn, user["id"])


@router.post("/me/tokens")
def create_my_token(data: TokenIn, request: Request, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    return issue_token_with_qr(conn, user, data, user["id"], request)


@router.delete("/me/tokens/{token_id}")
def delete_my_token(token_id: int, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    cur = conn.execute("DELETE FROM api_tokens WHERE id = ? AND user_id = ?", (token_id, user["id"]))
    if cur.rowcount == 0:
        raise HTTPException(404, "Token nicht gefunden")
    audit(conn, user["id"], "token_deleted", f"user:{user['id']}", str(token_id))
    return {"ok": True}
