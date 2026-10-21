"""Korrekturanträge: Mitarbeiter beantragen Nachtrag, Änderung oder Löschung einer Buchung,
ein Admin genehmigt (Änderung wird übernommen) oder lehnt ab. Mails an Admins bzw. Mitarbeiter."""
import sqlite3
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..closing import ensure_open, utc_day
from ..db import audit, get_db, get_setting
from ..security import admin_user, current_user
from ..timeutil import local_iso, now_utc_str, parse_utc, to_local, to_utc_str
from .entries import _check_entry, resolve_user_id

router = APIRouter(prefix="/api/v1/requests", tags=["Korrekturanträge"])
KINDS = {"add": "Nachtrag", "change": "Änderung", "delete": "Löschung"}
STATUS = ("offen", "genehmigt", "abgelehnt", "zurueckgezogen")


def _fmt(value: Optional[str]) -> str:
    return to_local(parse_utc(value)).strftime("%d.%m.%Y %H:%M") if value else "offen"


def _span(start: Optional[str], end: Optional[str]) -> str:
    if not start:
        return "–"
    s = to_local(parse_utc(start))
    if not end:
        return f"{s:%d.%m.%Y %H:%M} – läuft"
    e = to_local(parse_utc(end))
    return f"{s:%d.%m.%Y %H:%M} – {e:%H:%M}" + ("" if e.date() == s.date() else f" ({e:%d.%m.})")


def request_out(r: sqlite3.Row) -> dict:
    keys = r.keys()
    return {
        "id": r["id"], "user_id": r["user_id"], "kind": r["kind"], "kind_label": KINDS.get(r["kind"], r["kind"]),
        "entry_id": r["entry_id"],
        "old_start": local_iso(r["old_start_utc"]), "old_end": local_iso(r["old_end_utc"]),
        "start": local_iso(r["start_utc"]), "end": local_iso(r["end_utc"]),
        "old_text": _span(r["old_start_utc"], r["old_end_utc"]) if r["kind"] != "add" else "",
        "new_text": _span(r["start_utc"], r["end_utc"]) if r["kind"] != "delete" else "",
        "day": (utc_day(r["start_utc"] or r["old_start_utc"]) or datetime.now().date()).isoformat(),
        "reason": r["reason"], "status": r["status"],
        "created_at": local_iso(r["created_at"]), "decided_at": local_iso(r["decided_at"]),
        "decision_note": r["decision_note"],
        "display_name": r["display_name"] if "display_name" in keys else None,
        "decided_by_name": r["decided_by_name"] if "decided_by_name" in keys else None,
    }


def _load(conn, rid: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT r.*, u.display_name, d.display_name AS decided_by_name FROM correction_requests r "
        "JOIN users u ON u.id = r.user_id LEFT JOIN users d ON d.id = r.decided_by WHERE r.id = ?", (rid,)).fetchone()
    if not row:
        raise HTTPException(404, "Antrag nicht gefunden")
    return row


def _mail(conn, recipients: List[str], subject: str, body: str) -> None:
    """Benachrichtigung – Fehler beim Versand stören den Ablauf nicht."""
    from ..mailer import MailError, send_mail, smtp_config, valid_email
    if not smtp_config(conn)["host"]:
        return
    url = (get_setting(conn, "public_url") or "").rstrip("/")
    text = body + ("\n\n" + url if url else "") + "\n\nDiese Mail wurde automatisch von der Zeiterfassung verschickt."
    for to in dict.fromkeys(r for r in recipients if r and valid_email(r)):
        try:
            send_mail(conn, to, subject, text)
        except MailError as e:
            print(f"[zeiterfassung] Mail an {to} fehlgeschlagen: {e}", flush=True)


# --- Antrag stellen -------------------------------------------------------------

class RequestIn(BaseModel):
    kind: str = Field(..., description="add (nachtragen), change (ändern) oder delete (löschen)")
    entry_id: Optional[int] = None
    start: Optional[datetime] = Field(None, description="Ohne Zeitzone = Ortszeit")
    end: Optional[datetime] = None
    reason: str = Field(..., min_length=3, max_length=300)


@router.post("", status_code=201)
def create_request(data: RequestIn, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    if data.kind not in KINDS:
        raise HTTPException(400, "Art muss add, change oder delete sein")
    old = None
    if data.kind in ("change", "delete"):
        old = conn.execute("SELECT * FROM entries WHERE id = ? AND user_id = ?", (data.entry_id or -1, user["id"])).fetchone()
        if not old:
            raise HTTPException(404, "Buchung nicht gefunden")
        if conn.execute("SELECT 1 FROM correction_requests WHERE entry_id = ? AND status = 'offen'", (old["id"],)).fetchone():
            raise HTTPException(409, "Für diese Buchung gibt es schon einen offenen Antrag")
    start = end = None
    if data.kind in ("add", "change"):
        if not data.start or not data.end:
            raise HTTPException(400, "Bitte Kommen und Gehen angeben")
        start, end = to_utc_str(data.start), to_utc_str(data.end)
        _check_entry(conn, user["id"], start, end, exclude_id=old["id"] if old else None)
    days = [utc_day(start), utc_day(end)] + ([utc_day(old["start_utc"]), utc_day(old["end_utc"])] if old else [])
    ensure_open(conn, user["id"], days)
    cur = conn.execute(
        "INSERT INTO correction_requests(user_id, kind, entry_id, old_start_utc, old_end_utc, start_utc, end_utc, "
        "reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user["id"], data.kind, old["id"] if old else None, old["start_utc"] if old else None,
         old["end_utc"] if old else None, start, end, data.reason.strip(), now_utc_str()),
    )
    req = _load(conn, cur.lastrowid)
    audit(conn, user["id"], "request_created", f"request:{req['id']}", f"{data.kind}: {data.reason.strip()}")
    admins = [r["email"] for r in conn.execute(
        "SELECT email FROM users WHERE is_admin = 1 AND active = 1 AND id != ? AND email IS NOT NULL", (user["id"],))]
    out = request_out(req)
    lines = [f"{user['display_name']} hat einen Korrekturantrag gestellt ({out['kind_label']}).", ""]
    if out["old_text"]:
        lines.append(f"Bisher:     {out['old_text']}")
    if out["new_text"]:
        lines.append(f"Beantragt:  {out['new_text']}")
    lines += [f"Begründung: {out['reason']}", "", "Genehmigen oder ablehnen unter Verwaltung → Korrekturanträge."]
    _mail(conn, admins, f"Zeiterfassung: Korrekturantrag von {user['display_name']}", "\n".join(lines))
    return out


# --- Anzeigen -------------------------------------------------------------------

@router.get("")
def list_requests(
    status: Optional[str] = Query(None, description="offen, genehmigt, abgelehnt, zurueckgezogen; leer = alle"),
    user_id: Optional[int] = None,
    all_users: bool = Query(False, description="Admins: Anträge aller Mitarbeiter"),
    limit: int = Query(100, le=500),
    user=Depends(current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    sql = ("SELECT r.*, u.display_name, d.display_name AS decided_by_name FROM correction_requests r "
           "JOIN users u ON u.id = r.user_id LEFT JOIN users d ON d.id = r.decided_by WHERE 1 = 1")
    args: list = []
    if all_users:
        if not user["is_admin"]:
            raise HTTPException(403, "Nur für Administratoren")
    else:
        sql += " AND r.user_id = ?"
        args.append(resolve_user_id(user, user_id))
    if status:
        if status not in STATUS:
            raise HTTPException(400, "Unbekannter Status")
        sql += " AND r.status = ?"
        args.append(status)
    sql += " ORDER BY CASE r.status WHEN 'offen' THEN 0 ELSE 1 END, COALESCE(r.decided_at, r.created_at) DESC LIMIT ?"
    args.append(limit)
    return [request_out(r) for r in conn.execute(sql, args).fetchall()]


@router.get("/count")
def count_open(user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Offene Anträge: eigene – und für Admins alle (Zähler an den Reitern)."""
    mine = conn.execute("SELECT COUNT(*) FROM correction_requests WHERE status = 'offen' AND user_id = ?",
                        (user["id"],)).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM correction_requests WHERE status = 'offen'").fetchone()[0] \
        if user["is_admin"] else None
    return {"mine": mine, "all": total}


# --- Entscheiden ----------------------------------------------------------------

class DecisionIn(BaseModel):
    note: Optional[str] = Field(None, max_length=300)


def _decide(conn, req: sqlite3.Row, status: str, admin, note: Optional[str]) -> None:
    conn.execute("UPDATE correction_requests SET status = ?, decided_at = ?, decided_by = ?, decision_note = ? WHERE id = ?",
                 (status, now_utc_str(), admin["id"], (note or "").strip() or None, req["id"]))


def _notify_user(conn, req_id: int) -> dict:
    req = _load(conn, req_id)
    out = request_out(req)
    who = conn.execute("SELECT email, display_name FROM users WHERE id = ?", (req["user_id"],)).fetchone()
    word = "genehmigt" if req["status"] == "genehmigt" else "abgelehnt"
    lines = [f"Hallo {who['display_name']},", "",
             f"dein Korrekturantrag ({out['kind_label']}) wurde {word}."]
    if out["new_text"]:
        lines.append(f"Beantragt: {out['new_text']}")
    if out["old_text"] and req["kind"] == "delete":
        lines.append(f"Buchung: {out['old_text']}")
    if req["decision_note"]:
        lines.append(f"Anmerkung: {req['decision_note']}")
    _mail(conn, [who["email"]], f"Zeiterfassung: Korrekturantrag {word}", "\n".join(lines))
    return out


@router.post("/{rid}/approve")
def approve(rid: int, data: DecisionIn, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    req = _load(conn, rid)
    if req["status"] != "offen":
        raise HTTPException(409, "Antrag ist nicht mehr offen")
    uid, now = req["user_id"], now_utc_str()
    note = f"Korrekturantrag: {req['reason']}"[:200]
    if req["kind"] in ("change", "delete"):
        e = conn.execute("SELECT * FROM entries WHERE id = ?", (req["entry_id"],)).fetchone()
        if not e:
            raise HTTPException(409, "Die Buchung gibt es nicht mehr – bitte ablehnen")
        if (e["start_utc"], e["end_utc"]) != (req["old_start_utc"], req["old_end_utc"]):
            raise HTTPException(409, "Die Buchung wurde inzwischen geändert – bitte prüfen und ablehnen oder neu beantragen lassen")
        ensure_open(conn, uid, [utc_day(e["start_utc"]), utc_day(e["end_utc"]), utc_day(req["start_utc"]), utc_day(req["end_utc"])])
    else:
        ensure_open(conn, uid, [utc_day(req["start_utc"]), utc_day(req["end_utc"])])
    if req["kind"] == "add":
        _check_entry(conn, uid, req["start_utc"], req["end_utc"])
        cur = conn.execute(
            "INSERT INTO entries(user_id, start_utc, end_utc, source, note, created_at, updated_at, updated_by) "
            "VALUES (?, ?, ?, 'manual', ?, ?, ?, ?)", (uid, req["start_utc"], req["end_utc"], note, now, now, admin["id"]))
        audit(conn, admin["id"], "entry_created", f"entry:{cur.lastrowid}",
              f"{req['start_utc']} – {req['end_utc']} (Antrag {rid})")
    elif req["kind"] == "change":
        _check_entry(conn, uid, req["start_utc"], req["end_utc"], exclude_id=req["entry_id"])
        conn.execute("UPDATE entries SET start_utc = ?, end_utc = ?, note = ?, updated_at = ?, updated_by = ?, auto_closed = 0 "
                     "WHERE id = ?", (req["start_utc"], req["end_utc"], note, now, admin["id"], req["entry_id"]))
        audit(conn, admin["id"], "entry_updated", f"entry:{req['entry_id']}",
              f"vorher {req['old_start_utc']} – {req['old_end_utc']}, nachher {req['start_utc']} – {req['end_utc']} (Antrag {rid})")
    else:
        conn.execute("DELETE FROM entries WHERE id = ?", (req["entry_id"],))
        audit(conn, admin["id"], "entry_deleted", f"entry:{req['entry_id']}",
              f"user:{uid} {req['old_start_utc']} – {req['old_end_utc']} (Antrag {rid})")
    _decide(conn, req, "genehmigt", admin, data.note)
    audit(conn, admin["id"], "request_approved", f"request:{rid}", data.note or "")
    return _notify_user(conn, rid)


@router.post("/{rid}/reject")
def reject(rid: int, data: DecisionIn, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    req = _load(conn, rid)
    if req["status"] != "offen":
        raise HTTPException(409, "Antrag ist nicht mehr offen")
    if not (data.note or "").strip():
        raise HTTPException(400, "Bitte kurz begründen, warum der Antrag abgelehnt wird")
    _decide(conn, req, "abgelehnt", admin, data.note)
    audit(conn, admin["id"], "request_rejected", f"request:{rid}", data.note)
    return _notify_user(conn, rid)


@router.post("/{rid}/withdraw")
def withdraw(rid: int, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    req = _load(conn, rid)
    if req["user_id"] != user["id"]:
        raise HTTPException(403, "Nur eigene Anträge können zurückgezogen werden")
    if req["status"] != "offen":
        raise HTTPException(409, "Antrag ist nicht mehr offen")
    conn.execute("UPDATE correction_requests SET status = 'zurueckgezogen', decided_at = ? WHERE id = ?", (now_utc_str(), rid))
    audit(conn, user["id"], "request_withdrawn", f"request:{rid}")
    return request_out(_load(conn, rid))
