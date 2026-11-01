"""Urlaubsanträge: Mitarbeiter beantragen Urlaub (und andere Abwesenheiten), der Vorgesetzte genehmigt.

- Jeder Mitarbeiter kann einen Vorgesetzten haben (users.manager_id), z. B. den Filialleiter.
- Entscheiden darf der Vorgesetzte – sonst (und zusätzlich) jeder Admin. Ein Vorgesetzter ohne
  Admin-Rechte sieht nur die Anträge seiner Mitarbeiter, sonst nichts.
- Genehmigt → die Tage werden als Abwesenheit eingetragen (freie Tage und Feiertage übersprungen).
- Mails: an den Vorgesetzten (bzw. die Admins) bei neuem Antrag, an den Mitarbeiter bei der Entscheidung.
"""
import sqlite3
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..closing import ensure_open
from ..db import audit, get_db
from ..reports import ABSENCE_KINDS, holidays_between, target_fn, vacation_account
from ..security import current_user
from ..timeutil import daterange, local_iso, now_utc_str
from .requests import _mail

router = APIRouter(prefix="/api/v1/leave", tags=["Urlaubsanträge"])
KINDS = ("urlaub", "gleittag", "mehrarbeit", "bildungsurlaub", "sonstiges")
STATUS = ("offen", "genehmigt", "abgelehnt", "zurueckgezogen")
SELECT = ("SELECT l.*, u.display_name, u.manager_id, d.display_name AS decided_by_name, "
          "m.display_name AS manager_name FROM leave_requests l JOIN users u ON u.id = l.user_id "
          "LEFT JOIN users d ON d.id = l.decided_by LEFT JOIN users m ON m.id = u.manager_id")


def _de(d: str) -> str:
    return date.fromisoformat(d).strftime("%d.%m.%Y")


def _span(r) -> str:
    text = _de(r["from_day"]) if r["from_day"] == r["to_day"] else f"{_de(r['from_day'])} – {_de(r['to_day'])}"
    return text + (" (halber Tag)" if r["half_day"] else "")


def leave_out(r: sqlite3.Row, viewer=None) -> dict:
    out = {
        "id": r["id"], "user_id": r["user_id"], "display_name": r["display_name"],
        "kind": r["kind"], "kind_label": ABSENCE_KINDS.get(r["kind"], r["kind"]),
        "from": r["from_day"], "to": r["to_day"], "half_day": bool(r["half_day"]), "days": r["days"],
        "span": _span(r), "note": r["note"], "status": r["status"],
        "created_at": local_iso(r["created_at"]), "decided_at": local_iso(r["decided_at"]),
        "decided_by_name": r["decided_by_name"], "decision_note": r["decision_note"],
        "manager_name": r["manager_name"],
    }
    if viewer is not None:
        out["can_decide"] = r["status"] == "offen" and can_decide(viewer, r)
    return out


def can_decide(viewer, req) -> bool:
    """Vorgesetzter des Antragstellers oder Admin (Admins auch über eigene Anträge, z. B. im Ein-Admin-Betrieb)."""
    return bool(viewer["is_admin"]) or (req["manager_id"] is not None and req["manager_id"] == viewer["id"])


def _load(conn, lid: int) -> sqlite3.Row:
    row = conn.execute(SELECT + " WHERE l.id = ?", (lid,)).fetchone()
    if not row:
        raise HTTPException(404, "Antrag nicht gefunden")
    return row


def count_days(conn, user, d_from: date, d_to: date, half: bool) -> float:
    """Arbeitstage im Zeitraum (ohne freie Tage und Feiertage), halber Tag = 0,5."""
    holidays = holidays_between(conn, d_from, d_to)
    day_target = target_fn(conn, user)
    n = sum(1 for d in daterange(d_from, d_to) if day_target(d) > 0 and d not in holidays)
    return n * (0.5 if half else 1)


def _approvers(conn, user) -> List[sqlite3.Row]:
    """Wer bekommt die Mail zum neuen Antrag? Der Vorgesetzte – ohne Vorgesetzten alle Admins."""
    if user["manager_id"]:
        m = conn.execute("SELECT * FROM users WHERE id = ? AND active = 1", (user["manager_id"],)).fetchone()
        if m:
            return [m]
    return conn.execute("SELECT * FROM users WHERE is_admin = 1 AND active = 1 AND id != ?", (user["id"],)).fetchall()


# --- Antrag stellen ---------------------------------------------------------------------

class LeaveIn(BaseModel):
    kind: str = Field("urlaub", description=", ".join(KINDS))
    from_day: date
    to_day: Optional[date] = None
    half_day: bool = False
    note: Optional[str] = Field(None, max_length=300)


@router.post("", status_code=201)
def create_leave(data: LeaveIn, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    if data.kind not in KINDS:
        raise HTTPException(400, f"Art muss eine von {', '.join(KINDS)} sein")
    to_day = data.to_day or data.from_day
    if to_day < data.from_day or (to_day - data.from_day).days > 366:
        raise HTTPException(400, "Ungültiger Zeitraum")
    if data.half_day and to_day != data.from_day:
        raise HTTPException(400, "Ein halber Tag geht nur für einen einzelnen Tag")
    ensure_open(conn, user["id"], [data.from_day, to_day])
    days = count_days(conn, user, data.from_day, to_day, data.half_day)
    if days <= 0:
        raise HTTPException(400, "Im Zeitraum liegt kein Arbeitstag (nur freie Tage oder Feiertage)")
    clash = conn.execute("SELECT day FROM absences WHERE user_id = ? AND day BETWEEN ? AND ? ORDER BY day LIMIT 1",
                         (user["id"], data.from_day.isoformat(), to_day.isoformat())).fetchone()
    if clash:
        raise HTTPException(409, f"Am {_de(clash[0])} ist schon eine Abwesenheit eingetragen")
    open_clash = conn.execute(
        "SELECT id FROM leave_requests WHERE user_id = ? AND status = 'offen' AND from_day <= ? AND to_day >= ?",
        (user["id"], to_day.isoformat(), data.from_day.isoformat())).fetchone()
    if open_clash:
        raise HTTPException(409, "Für diesen Zeitraum gibt es schon einen offenen Antrag")
    cur = conn.execute(
        "INSERT INTO leave_requests(user_id, kind, from_day, to_day, half_day, days, note, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'offen', ?)",
        (user["id"], data.kind, data.from_day.isoformat(), to_day.isoformat(), int(data.half_day), days,
         (data.note or "").strip() or None, now_utc_str()))
    req = _load(conn, cur.lastrowid)
    out = leave_out(req, user)
    audit(conn, user["id"], "leave_requested", f"user:{user['id']}", f"{out['kind_label']} {out['span']} ({_num(days)} Tage)")
    lines = [f"{user['display_name']} hat einen Antrag gestellt: {out['kind_label']}", "",
             f"Zeitraum:   {out['span']}", f"Arbeitstage: {_num(days)}"]
    if data.kind == "urlaub":
        vac = vacation_account(conn, user, data.from_day.year)
        if vac:
            lines.append(f"Resturlaub {vac['year']} danach: {_num(vac['remaining'] - days)} Tage")
    if out["note"]:
        lines.append(f"Bemerkung:  {out['note']}")
    lines += ["", "Genehmigen oder ablehnen in der Zeiterfassung unter Übersicht → Anträge zur Freigabe."]
    _mail(conn, [a["email"] for a in _approvers(conn, user)],
          f"Zeiterfassung: {out['kind_label']}santrag von {user['display_name']}"
          if data.kind == "urlaub" else f"Zeiterfassung: Antrag von {user['display_name']} ({out['kind_label']})",
          "\n".join(lines))
    return out


def _num(v: float) -> str:
    return (f"{v:g}").replace(".", ",")


# --- Anzeigen -----------------------------------------------------------------------------

@router.get("")
def list_leave(
    scope: str = Query("mine", description="mine = eigene, decide = die ich entscheiden darf"),
    status: Optional[str] = Query(None, description=", ".join(STATUS)),
    limit: int = Query(100, le=500),
    user=Depends(current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    sql, args = SELECT + " WHERE 1 = 1", []
    if scope == "mine":
        sql += " AND l.user_id = ?"
        args.append(user["id"])
    elif scope == "decide":
        if not user["is_admin"]:
            sql += " AND u.manager_id = ?"
            args.append(user["id"])
    else:
        raise HTTPException(400, "scope muss mine oder decide sein")
    if status:
        if status not in STATUS:
            raise HTTPException(400, "Unbekannter Status")
        sql += " AND l.status = ?"
        args.append(status)
    sql += " ORDER BY CASE l.status WHEN 'offen' THEN 0 ELSE 1 END, l.from_day DESC LIMIT ?"
    args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    out = []
    for r in rows:
        item = leave_out(r, user)
        if scope == "decide" and r["kind"] == "urlaub":
            target = conn.execute("SELECT * FROM users WHERE id = ?", (r["user_id"],)).fetchone()
            vac = vacation_account(conn, target, int(r["from_day"][:4]))
            item["vacation_remaining"] = vac["remaining"] if vac else None
        out.append(item)
    return out


@router.get("/count")
def count_leave(user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Offene Anträge zum Entscheiden (Zähler am Reiter) und eigene offene."""
    if user["is_admin"]:
        decide = conn.execute("SELECT COUNT(*) FROM leave_requests WHERE status = 'offen'").fetchone()[0]
    else:
        decide = conn.execute("SELECT COUNT(*) FROM leave_requests l JOIN users u ON u.id = l.user_id "
                              "WHERE l.status = 'offen' AND u.manager_id = ?", (user["id"],)).fetchone()[0]
    mine = conn.execute("SELECT COUNT(*) FROM leave_requests WHERE status = 'offen' AND user_id = ?",
                        (user["id"],)).fetchone()[0]
    is_manager = bool(conn.execute("SELECT 1 FROM users WHERE manager_id = ? AND active = 1 LIMIT 1",
                                   (user["id"],)).fetchone())
    return {"decide": decide, "mine": mine, "can_decide": bool(user["is_admin"]) or is_manager}


# --- Entscheiden / zurückziehen -------------------------------------------------------------

class DecisionIn(BaseModel):
    note: Optional[str] = Field(None, max_length=300)


def _decide_check(conn, lid: int, user) -> sqlite3.Row:
    req = _load(conn, lid)
    if req["status"] != "offen":
        raise HTTPException(409, "Antrag ist nicht mehr offen")
    if not can_decide(user, req):
        raise HTTPException(403, "Diesen Antrag darf nur der Vorgesetzte oder ein Admin entscheiden")
    if req["user_id"] == user["id"] and not user["is_admin"]:
        raise HTTPException(403, "Über den eigenen Antrag entscheidet jemand anderes")
    return req


def _notify(conn, lid: int) -> dict:
    req = _load(conn, lid)
    out = leave_out(req)
    who = conn.execute("SELECT email, display_name FROM users WHERE id = ?", (req["user_id"],)).fetchone()
    word = "genehmigt" if req["status"] == "genehmigt" else "abgelehnt"
    lines = [f"Hallo {who['display_name']},", "",
             f"dein Antrag ({out['kind_label']}, {out['span']}) wurde von {req['decided_by_name']} {word}."]
    if req["decision_note"]:
        lines.append(f"Anmerkung: {req['decision_note']}")
    _mail(conn, [who["email"]], f"Zeiterfassung: {out['kind_label']} {word}", "\n".join(lines))
    return out


@router.post("/{lid}/approve")
def approve_leave(lid: int, data: DecisionIn, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    req = _decide_check(conn, lid, user)
    d_from, d_to = date.fromisoformat(req["from_day"]), date.fromisoformat(req["to_day"])
    ensure_open(conn, req["user_id"], [d_from, d_to])
    target = conn.execute("SELECT * FROM users WHERE id = ?", (req["user_id"],)).fetchone()
    holidays = holidays_between(conn, d_from, d_to)
    day_target = target_fn(conn, target)
    note = f"Antrag genehmigt von {user['display_name']}" + (f": {req['note']}" if req["note"] else "")
    single, count = d_from == d_to, 0
    for d in daterange(d_from, d_to):
        if not single and (day_target(d) <= 0 or d in holidays):
            continue
        conn.execute(
            "INSERT INTO absences(user_id, day, kind, half_day, note) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, day) DO UPDATE SET kind = excluded.kind, half_day = excluded.half_day, note = excluded.note",
            (req["user_id"], d.isoformat(), req["kind"], req["half_day"], note[:300]))
        count += 1
    conn.execute("UPDATE leave_requests SET status = 'genehmigt', decided_at = ?, decided_by = ?, decision_note = ? "
                 "WHERE id = ?", (now_utc_str(), user["id"], (data.note or "").strip() or None, lid))
    audit(conn, user["id"], "leave_approved", f"user:{req['user_id']}",
          f"{ABSENCE_KINDS.get(req['kind'])} {_span(req)} ({count} Tage eingetragen)")
    return _notify(conn, lid)


@router.post("/{lid}/reject")
def reject_leave(lid: int, data: DecisionIn, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    req = _decide_check(conn, lid, user)
    if not (data.note or "").strip():
        raise HTTPException(400, "Bitte kurz begründen, warum der Antrag abgelehnt wird")
    conn.execute("UPDATE leave_requests SET status = 'abgelehnt', decided_at = ?, decided_by = ?, decision_note = ? "
                 "WHERE id = ?", (now_utc_str(), user["id"], data.note.strip(), lid))
    audit(conn, user["id"], "leave_rejected", f"user:{req['user_id']}",
          f"{ABSENCE_KINDS.get(req['kind'])} {_span(req)}: {data.note.strip()}")
    return _notify(conn, lid)


@router.post("/{lid}/withdraw")
def withdraw_leave(lid: int, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    req = _load(conn, lid)
    if req["user_id"] != user["id"]:
        raise HTTPException(403, "Nur eigene Anträge können zurückgezogen werden")
    if req["status"] != "offen":
        raise HTTPException(409, "Nur offene Anträge können zurückgezogen werden – bei genehmigten bitte an den Vorgesetzten wenden")
    conn.execute("UPDATE leave_requests SET status = 'zurueckgezogen', decided_at = ? WHERE id = ?", (now_utc_str(), lid))
    audit(conn, user["id"], "leave_withdrawn", f"user:{user['id']}", f"{ABSENCE_KINDS.get(req['kind'])} {_span(req)}")
    return leave_out(_load(conn, lid), user)

