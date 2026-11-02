"""Kiosk: ein Tablet an der Wache, an dem jeder per Name + PIN kommt und geht.

- Ein Admin schaltet das Tablet einmalig frei (Kiosk-Zugang, Token „zk_…“ per QR-Code/Link).
- Jeder Mitarbeiter setzt unter „Mein Zugang“ eine 4–6-stellige PIN; nur wer eine PIN hat, erscheint am Tablet.
- Das Tablet sieht nur Namen und Anwesenheit. Stempeln geht nur mit der richtigen PIN;
  nach 5 falschen PINs ist der Name 15 Minuten gesperrt.
- Filialen: Ist ein Tablet einer Filiale zugeordnet, zeigt es nur deren Mitarbeiter (und alle ohne Filiale).
- Nach der PIN sieht jeder seine Konten: heute, Gleitzeit, ggf. Überstunden, Resturlaub.
"""
import hashlib
import re
import secrets
import sqlite3
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from ..db import audit, get_db
from ..dsm_auth import clear_failures, note_failure, too_many_attempts
from ..security import admin_user, current_user, hash_password, verify_password
from ..reports import account_balance, compute_report, vacation_account
from ..timeutil import fmt_duration, local_hhmm, now_utc, now_utc_str, parse_utc, to_local
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


# --- Filialen (Admin) --------------------------------------------------------------------

class BranchIn(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=60)
    user_ids: Optional[List[int]] = Field(None, description="Mitarbeiter dieser Filiale (ersetzt die bisherige Liste)")


def _branches(conn) -> list:
    members: dict = {}
    for r in conn.execute("SELECT ub.branch_id, ub.user_id FROM user_branches ub JOIN users u ON u.id = ub.user_id "
                          "ORDER BY u.display_name COLLATE NOCASE"):
        members.setdefault(r["branch_id"], []).append(r["user_id"])
    return [{"id": r["id"], "name": r["name"], "user_ids": members.get(r["id"], [])}
            for r in conn.execute("SELECT * FROM branches ORDER BY name COLLATE NOCASE")]


def _set_members(conn, branch_id: int, user_ids: List[int]) -> None:
    known = {r[0] for r in conn.execute("SELECT id FROM users")}
    unknown = [u for u in user_ids if u not in known]
    if unknown:
        raise HTTPException(400, f"Unbekannte Mitarbeiter: {unknown}")
    conn.execute("DELETE FROM user_branches WHERE branch_id = ?", (branch_id,))
    conn.executemany("INSERT INTO user_branches(user_id, branch_id) VALUES (?, ?)", [(u, branch_id) for u in set(user_ids)])


@router.get("/branches")
def list_branches(admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    return _branches(conn)


@router.post("/branches", status_code=201)
def create_branch(data: BranchIn, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    name = (data.name or "").strip()
    if not name:
        raise HTTPException(400, "Bitte einen Namen für die Filiale angeben")
    try:
        cur = conn.execute("INSERT INTO branches(name, created_at) VALUES (?, ?)", (name, now_utc_str()))
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"Die Filiale „{name}“ gibt es schon")
    if data.user_ids:
        _set_members(conn, cur.lastrowid, data.user_ids)
    audit(conn, admin["id"], "branch_saved", f"branch:{cur.lastrowid}", f"{name} angelegt")
    return _branches(conn)


@router.patch("/branches/{branch_id}")
def update_branch(branch_id: int, data: BranchIn, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT * FROM branches WHERE id = ?", (branch_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Filiale nicht gefunden")
    parts = []
    if data.name is not None and data.name.strip() != row["name"]:
        try:
            conn.execute("UPDATE branches SET name = ? WHERE id = ?", (data.name.strip(), branch_id))
        except sqlite3.IntegrityError:
            raise HTTPException(409, f"Die Filiale „{data.name.strip()}“ gibt es schon")
        parts.append(f"umbenannt in {data.name.strip()}")
    if data.user_ids is not None:
        _set_members(conn, branch_id, data.user_ids)
        parts.append(f"{len(set(data.user_ids))} Mitarbeiter")
    audit(conn, admin["id"], "branch_saved", f"branch:{branch_id}", f"{row['name']}: " + ", ".join(parts))
    return _branches(conn)


@router.delete("/branches/{branch_id}")
def delete_branch(branch_id: int, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT * FROM branches WHERE id = ?", (branch_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Filiale nicht gefunden")
    conn.execute("UPDATE kiosk_devices SET branch_id = NULL WHERE branch_id = ?", (branch_id,))
    conn.execute("DELETE FROM branches WHERE id = ?", (branch_id,))
    audit(conn, admin["id"], "branch_deleted", f"branch:{branch_id}", row["name"])
    return _branches(conn)


# --- Tablets freischalten (Admin) ----------------------------------------------------

class KioskIn(BaseModel):
    name: str = Field("Tablet Wache", min_length=1, max_length=60)
    host_hint: Optional[str] = Field(None, max_length=255, pattern=r"^[A-Za-z0-9.\-:\[\]]*$")
    branch_id: Optional[int] = Field(None, description="Filiale des Tablets; leer = alle Mitarbeiter")


class KioskUpdate(BaseModel):
    branch_id: Optional[int] = Field(None, description="Filiale; null = alle Mitarbeiter")


def _check_branch(conn, branch_id: Optional[int]) -> None:
    if branch_id is not None and not conn.execute("SELECT 1 FROM branches WHERE id = ?", (branch_id,)).fetchone():
        raise HTTPException(400, "Filiale nicht gefunden")


def _kiosk_out(r: sqlite3.Row) -> dict:
    from ..timeutil import local_iso
    return {"id": r["id"], "name": r["name"], "created_at": local_iso(r["created_at"]),
            "last_used_at": local_iso(r["last_used_at"]), "branch_id": r["branch_id"], "branch": r["branch"]}


@router.get("/kiosks")
def list_kiosks(admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    return [_kiosk_out(r) for r in conn.execute(
        "SELECT k.*, b.name AS branch FROM kiosk_devices k LEFT JOIN branches b ON b.id = k.branch_id ORDER BY k.id")]


@router.patch("/kiosks/{kiosk_id}")
def update_kiosk(kiosk_id: int, data: KioskUpdate, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT * FROM kiosk_devices WHERE id = ?", (kiosk_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Kiosk nicht gefunden")
    _check_branch(conn, data.branch_id)
    conn.execute("UPDATE kiosk_devices SET branch_id = ? WHERE id = ?", (data.branch_id, kiosk_id))
    bname = conn.execute("SELECT name FROM branches WHERE id = ?", (data.branch_id,)).fetchone() if data.branch_id else None
    audit(conn, admin["id"], "kiosk_branch", f"kiosk:{kiosk_id}", f"{row['name']}: {bname[0] if bname else 'alle Mitarbeiter'}")
    return list_kiosks(admin, conn)


@router.post("/kiosks")
def create_kiosk(data: KioskIn, request: Request, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    _check_branch(conn, data.branch_id)
    token = KIOSK_PREFIX + secrets.token_urlsafe(32)
    cur = conn.execute("INSERT INTO kiosk_devices(name, token_hash, created_at, created_by, branch_id) VALUES (?, ?, ?, ?, ?)",
                       (data.name.strip(), _sha(token), now_utc_str(), admin["id"], data.branch_id))
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
    rows = [r for r in rows if _on_tablet(conn, device, r["id"])]
    branch = conn.execute("SELECT name FROM branches WHERE id = ?", (device["branch_id"],)).fetchone() \
        if device["branch_id"] else None
    theme = conn.execute("SELECT value FROM app_settings WHERE key = 'theme_default'").fetchone()
    from .. import logo
    company = conn.execute("SELECT value FROM app_settings WHERE key = 'company_name'").fetchone()
    return {"kiosk": device["name"], "branch": branch[0] if branch else None, "theme": theme[0] if theme else "classic",
            "company_name": company[0] if company else "", "logo": logo.info(conn),
            "users": [{"id": r["id"], "name": r["display_name"], "present": r["id"] in present} for r in rows]}


def _on_tablet(conn, device, user_id: int) -> bool:
    """Gehört der Mitarbeiter auf dieses Tablet? Ohne Filiale am Tablet: alle. Mitarbeiter ohne Filiale: überall."""
    if not device["branch_id"]:
        return True
    mine = {r[0] for r in conn.execute("SELECT branch_id FROM user_branches WHERE user_id = ?", (user_id,))}
    return not mine or device["branch_id"] in mine


def _accounts(conn, user) -> dict:
    """Kontostände für die Anzeige am Tablet (erst nach richtiger PIN)."""
    today = to_local(now_utc()).date()
    day = compute_report(conn, user, today, today)["days"][0]
    acc = account_balance(conn, user)
    uses_overtime = bool(acc["ueberstunden_s"] or user["carryover_overtime_minutes"]) or bool(conn.execute(
        "SELECT 1 FROM day_flags WHERE user_id = ? AND overtime = 1 LIMIT 1", (user["id"],)).fetchone())
    vac = vacation_account(conn, user, today.year)
    out = {
        "today_worked": day["worked"], "today_target": day["target"], "today_diff": day["diff"],
        "today_diff_s": day["diff_s"], "today_has_target": day["target_s"] > 0,
        "gleitzeit": acc["gleitzeit"], "gleitzeit_s": acc["gleitzeit_s"],
        "ueberstunden": acc["ueberstunden"] if uses_overtime else None,
        "ueberstunden_s": acc["ueberstunden_s"] if uses_overtime else None,
        "vacation_remaining": vac["remaining"] if vac else None, "vacation_year": today.year,
    }
    return out


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
    if not _on_tablet(conn, device, user["id"]):
        raise HTTPException(403, "Dieser Mitarbeiter gehört nicht zu der Filiale dieses Tablets")
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
    res["accounts"] = _accounts(conn, user)
    return res
