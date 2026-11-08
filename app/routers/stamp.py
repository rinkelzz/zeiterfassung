"""Stempel-Link: stempeln ohne Kurzbefehle – per Home-Bildschirm-Icon oder NFC-Aufkleber mit Web-Adresse.

Ablauf:
1. Auf der Einrichtungsseite öffnet man einmal /stempeln#t=<Schlüssel> auf dem eigenen iPhone. Die Seite
   „koppelt“ das Gerät: Der Server legt den Schlüssel in einen HttpOnly-Cookie (nur für /api/v1/stamp,
   SameSite=Strict, 400 Tage). Den Schlüssel sieht danach kein Skript mehr.
2. Ein NFC-Aufkleber enthält nur eine Adresse ohne Geheimnis, z. B. /stempeln?a=in&src=nfc. Wer den Aufkleber
   mit einem nicht gekoppelten Handy scannt, sieht nur „Dieses Gerät ist nicht eingerichtet“.

Der Schlüssel ist derselbe wie für die Kurzbefehle und darf nur stempeln (siehe security.token_allowed).
"""
import sqlite3
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from ..config import settings
from ..db import get_db
from ..security import _user_from_api_token
from ..timeutil import local_hhmm, now_utc, parse_utc
from .clock import _open_entry, _status, do_clock_in, do_clock_out

router = APIRouter(prefix="/api/v1/stamp", tags=["Stempeln"])

COOKIE = "ze_stamp"
COOKIE_DAYS = 400          # Höchstwert, den Browser für Cookies erlauben
TOGGLE_GUARD_SECONDS = 60  # Doppelt gescannt? Umschalten innerhalb einer Minute ändert nichts
STAMP_SOURCES = ("nfc", "link")


def _cookie_path() -> str:
    return (settings.root_path or "") + "/api/v1/stamp"


def _user(request: Request, conn: sqlite3.Connection):
    token = request.cookies.get(COOKIE, "")
    user = _user_from_api_token(conn, token) if token else None
    if not user:
        raise HTTPException(401, "not_paired")
    return user


def _src(src: Optional[str]) -> str:
    return src if src in STAMP_SOURCES else "link"


class Pair(BaseModel):
    token: str = Field(..., min_length=10, max_length=200)


@router.post("/pair")
def pair(data: Pair, request: Request, response: Response, conn: sqlite3.Connection = Depends(get_db)):
    """Gerät koppeln: Schlüssel prüfen und als HttpOnly-Cookie ablegen."""
    user = _user_from_api_token(conn, data.token.strip())
    if not user:
        raise HTTPException(401, "Der Schlüssel ist ungültig oder wurde gesperrt.")
    https = settings.cookie_secure or request.url.scheme == "https"
    response.set_cookie(COOKIE, data.token.strip(), max_age=COOKIE_DAYS * 86400, path=_cookie_path(),
                        httponly=True, secure=https, samesite="strict")
    st = _status(conn, user, False, "Dieses Gerät ist eingerichtet.")
    st["insecure_http"] = not https
    return st


@router.post("/forget")
def forget(response: Response):
    response.delete_cookie(COOKIE, path=_cookie_path())
    return {"ok": True}


@router.get("/status")
def status(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    user = _user(request, conn)
    entry = _open_entry(conn, user["id"])
    msg = f"Eingestempelt seit {local_hhmm(parse_utc(entry['start_utc']))} Uhr" if entry else "Ausgestempelt"
    return _status(conn, user, False, msg)


SRC_Q = Query(None, description="nfc (Aufkleber, Wegezeit möglich) oder link (Home-Bildschirm)")


@router.post("/in")
def stamp_in(request: Request, src: Optional[str] = SRC_Q, conn: sqlite3.Connection = Depends(get_db)):
    return do_clock_in(conn, _user(request, conn), None, _src(src))


@router.post("/out")
def stamp_out(request: Request, src: Optional[str] = SRC_Q, conn: sqlite3.Connection = Depends(get_db)):
    return do_clock_out(conn, _user(request, conn), None, _src(src))


def _last_change(conn: sqlite3.Connection, user_id: int):
    row = conn.execute(
        "SELECT MAX(COALESCE(updated_at, created_at)) AS t FROM entries WHERE user_id = ?", (user_id,)).fetchone()
    return parse_utc(row["t"]) if row and row["t"] else None


@router.post("/toggle")
def stamp_toggle(request: Request, src: Optional[str] = SRC_Q, conn: sqlite3.Connection = Depends(get_db)):
    """Ein Aufkleber für beides. Wird er versehentlich zweimal gescannt, schaltet er nicht gleich zurück."""
    user = _user(request, conn)
    last = _last_change(conn, user["id"])
    if last and now_utc() - last < timedelta(seconds=TOGGLE_GUARD_SECONDS):
        entry = _open_entry(conn, user["id"])
        return _status(conn, user, False, "Gerade erst gestempelt – nichts geändert. ("
                       + ("Du bist eingestempelt." if entry else "Du bist ausgestempelt.") + ")")
    if _open_entry(conn, user["id"]):
        return do_clock_out(conn, user, None, _src(src))
    return do_clock_in(conn, user, None, _src(src))
