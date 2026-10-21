"""Kiosk: ein Tablet an der Wache, an dem jeder per Name + PIN kommt und geht.

- Ein Admin schaltet das Tablet einmalig frei (Kiosk-Zugang, Token „zk_…“ per QR-Code/Link).
- Jeder Mitarbeiter setzt unter „Mein Zugang“ eine 4–6-stellige PIN; nur wer eine PIN hat, erscheint am Tablet.
- Das Tablet sieht nur Namen und Anwesenheit. Stempeln geht nur mit der richtigen PIN;
  nach 5 falschen PINs ist der Name 15 Minuten gesperrt.
"""
import hashlib
import re
import secrets
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from ..db import audit, get_db
from ..dsm_auth import clear_failures, note_failure, too_many_attempts
from ..security import admin_user, current_user, hash_password, verify_password
from ..timeutil import local_hhmm, now_utc_str, parse_utc
from .auth import qr_svg, server_url
from .clock import _open_entry, _today_total, do_clock_in, do_clock_out

router = APIRouter(prefix="/api/v1", tags=["Kiosk"])
KIOSK_PREFIX = "zk_"
PIN_RE = re.compile(r"^\d{4,6}$")


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- PIN ---------------------------------------------------------------------------

class PinIn(BaseModel):
    pin: Optional[str] = Field(None, description="4–6 Ziffern; leer/null = PIN entfernen")


def _set_pin(conn, user_id: int, pin: Optional[str]) -> None:
    pin = (pin or "").strip()
    if pin and not PIN_RE.match(pin):
        raise HTTPException(400, "Die PIN muss aus 4 bis 6 Ziffern bestehen")
    if pin and len(set(pin)) == 1:
        raise HTTPException(400, "Bitte keine PIN aus lauter gleichen Ziffern")
    conn.execute("UPDATE users SET pin_hash = ? WHERE id = ?", (hash_password(pin) if pin else None, user_id))


@router.put("/me/pin")
def set_my_pin(data: PinIn, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Eigene PIN fürs Kiosk-Tablet setzen oder entfernen."""
    _set_pin(conn, user["id"], data.pin)
    audit(conn, user["id"], "pin_set" if data.pin else "pin_removed", f"user:{user['id']}")
    return {"ok": True, "has_pin": bool(data.pin)}


@router.put("/users/{user_id}/pin")
def set_user_pin(user_id: int, data: PinIn, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    """Admin setzt oder entfernt die PIN eines Mitarbeiters (z. B. vergessen)."""
    if not conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
        raise HTTPException(404, "Nutzer nicht gefunden")
    _set_pin(conn, user_id, data.pin)
    audit(conn, admin["id"], "pin_set" if data.pin else "pin_removed", f"user:{user_id}")
    return {"ok": True, "has_pin": bool(data.pin)}


# --- Tablets freischalten (Admin) ----------------------------------------------------

class KioskIn(BaseModel):
    name: str = Field("Tablet Wache", min_length=1, max_length=60)
    host_hint: Optional[str] = Field(None, max_length=255, pattern=r"^[A-Za-z0-9.\-:\[\]]*$")


def _kiosk_out(r: sqlite3.Row) -> dict:
    from ..timeutil import local_iso
    return {"id": r["id"], "name": r["name"], "created_at": local_iso(r["created_at"]),
            "last_used_at": local_iso(r["last_used_at"])}


@router.get("/kiosks")
def list_kiosks(admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    return [_kiosk_out(r) for r in conn.execute("SELECT * FROM kiosk_devices ORDER BY id")]


@router.post("/kiosks")
def create_kiosk(data: KioskIn, request: Request, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    token = KIOSK_PREFIX + secrets.token_urlsafe(32)
    cur = conn.execute("INSERT INTO kiosk_devices(name, token_hash, created_at, created_by) VALUES (?, ?, ?, ?)",
                       (data.name.strip(), _sha(token), now_utc_str(), admin["id"]))
    link = f"{server_url(request, conn, data.host_hint)}/kiosk#k={token}"
    audit(conn, admin["id"], "kiosk_created", f"kiosk:{cur.lastrowid}", data.name.strip())
    return {"id": cur.lastrowid, "name": data.name.strip(), "url": link, "qr_svg": qr_svg(link)}


@router.delete("/kiosks/{kiosk_id}")
def delete_kiosk(kiosk_id: int, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT * FROM kiosk_devices WHERE id = ?", (kiosk_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Kiosk nicht gefunden")
    conn.execute("DELETE FROM kiosk_devices WHERE id = ?", (kiosk_id,))
    audit(conn, admin["id"], "kiosk_deleted", f"kiosk:{kiosk_id}", row["name"])
    return {"ok": True}


# --- Am Tablet -------------------------------------------------------------------------

def kiosk_device(x_kiosk_token: Optional[str] = Header(None), conn: sqlite3.Connection = Depends(get_db)) -> sqlite3.Row:
    if not x_kiosk_token or not x_kiosk_token.startswith(KIOSK_PREFIX):
        raise HTTPException(401, "Tablet nicht freigeschaltet")
    row = conn.execute("SELECT * FROM kiosk_devices WHERE token_hash = ?", (_sha(x_kiosk_token),)).fetchone()
    if not row:
        raise HTTPException(401, "Tablet nicht (mehr) freigeschaltet")
    conn.execute("UPDATE kiosk_devices SET last_used_at = ? WHERE id = ?", (now_utc_str(), row["id"]))
    return row


@router.get("/kiosk/users")
def kiosk_users(device=Depends(kiosk_device), conn: sqlite3.Connection = Depends(get_db)):
    """Namen (nur mit PIN) und wer gerade eingestempelt ist."""
    present = {r[0] for r in conn.execute("SELECT user_id FROM entries WHERE end_utc IS NULL")}
    rows = conn.execute("SELECT id, display_name FROM users WHERE active = 1 AND pin_hash IS NOT NULL "
                        "ORDER BY display_name COLLATE NOCASE").fetchall()
    return {"kiosk": device["name"], "users": [{"id": r["id"], "name": r["display_name"], "present": r["id"] in present}
                                               for r in rows]}


class KioskAction(BaseModel):
    user_id: int
    pin: str = Field(..., max_length=6)
    action: str = Field("status", description="status, in oder out")


def _check_pin(conn, device, data: KioskAction) -> sqlite3.Row:
    user = conn.execute("SELECT * FROM users WHERE id = ? AND active = 1", (data.user_id,)).fetchone()
    key = f"kiosk-pin|{data.user_id}"
    if too_many_attempts(key):
        raise HTTPException(429, "Zu viele falsche PINs – bitte in 15 Minuten erneut versuchen")
    if not user or not user["pin_hash"] or not verify_password(data.pin or "", user["pin_hash"]):
        note_failure(key)
        if user:
            audit(conn, None, "kiosk_pin_wrong", f"user:{user['id']}", device["name"])
        raise HTTPException(403, "PIN falsch")
    clear_failures(key)
    return user


@router.post("/kiosk/action")
def kiosk_action(data: KioskAction, device=Depends(kiosk_device), conn: sqlite3.Connection = Depends(get_db)):
    """PIN prüfen und Status zeigen (action=status) bzw. kommen (in) / gehen (out)."""
    if data.action not in ("status", "in", "out"):
        raise HTTPException(400, "action muss status, in oder out sein")
    user = _check_pin(conn, device, data)
    if data.action == "in":
        res = do_clock_in(conn, user, None, "kiosk")
    elif data.action == "out":
        res = do_clock_out(conn, user, None, "kiosk")
    else:
        e = _open_entry(conn, user["id"])
        res = {"status": "in" if e else "out", "changed": False,
               "since_hhmm": local_hhmm(parse_utc(e["start_utc"])) if e else None,
               "today_total": _today_total(conn, user)}
    res["name"] = user["display_name"]
    return res
