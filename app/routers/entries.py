"""Buchungen ansehen und korrigieren, Abwesenheiten (Urlaub, Krank …) pflegen."""
import sqlite3
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..db import audit, get_db
from ..reports import ABSENCE_KINDS, holidays_between, target_fn
from ..security import admin_user, current_user
from ..timeutil import day_start_utc, daterange, local_iso, now_utc, now_utc_str, to_local, to_utc_str

router = APIRouter(prefix="/api/v1", tags=["Buchungen & Abwesenheiten"])


def resolve_user_id(user: sqlite3.Row, user_id: Optional[int]) -> int:
    """Mitarbeiter sehen nur sich selbst, Admins dürfen jeden abfragen."""
    if user_id is None or user_id == user["id"]:
        return user["id"]
    if not user["is_admin"]:
        raise HTTPException(403, "Nur für Administratoren")
    return user_id


def default_range(d_from: Optional[date], d_to: Optional[date]):
    today = to_local(now_utc()).date()
    d_from = d_from or today.replace(day=1)
    d_to = d_to or today
    if d_to < d_from:
        raise HTTPException(400, "'to' liegt vor 'from'")
    if (d_to - d_from).days > 400:
        raise HTTPException(400, "Zeitraum maximal 400 Tage")
    return d_from, d_to


def entry_out(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "user_id": r["user_id"],
        "start": local_iso(r["start_utc"]),
        "end": local_iso(r["end_utc"]),
        "source": r["source"],
        "note": r["note"],
        "edited": r["updated_by"] is not None,
        "travel_in_min": r["travel_in_min"],
        "travel_out_min": r["travel_out_min"],
        "auto_closed": bool(r["auto_closed"]) if "auto_closed" in r.keys() else False,
    }


# --- Buchungen --------------------------------------------------------------

class EntryCreate(BaseModel):
    user_id: int
    start: datetime = Field(description="Ohne Zeitzone = Ortszeit")
    end: Optional[datetime] = None
    note: Optional[str] = Field(None, max_length=200)


class EntryUpdate(BaseModel):
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    note: Optional[str] = Field(None, max_length=200)
    reopen: bool = Field(False, description="Ende entfernen (wieder eingestempelt)")


def _check_entry(conn, user_id: int, start: str, end: Optional[str], exclude_id: Optional[int] = None):
    if end is not None and end <= start:
        raise HTTPException(400, "Ende muss nach dem Beginn liegen")
    # bis 2 h in die Zukunft erlaubt (Wegezeit beim Gehen)
    limit = to_utc_str(now_utc() + timedelta(hours=2))
    if start > limit or (end is not None and end > limit):
        raise HTTPException(400, "Buchungen in der Zukunft sind nicht erlaubt")
    far_future = "9999-12-31T00:00:00+00:00"
    overlap = conn.execute(
        "SELECT id FROM entries WHERE user_id = ? AND id != ? "
        "AND start_utc < ? AND COALESCE(end_utc, ?) > ?",
        (user_id, exclude_id or -1, end or far_future, far_future, start),
    ).fetchone()
    if overlap:
        raise HTTPException(409, f"Überschneidet sich mit Buchung {overlap['id']}")


@router.get("/entries")
def list_entries(
    user_id: Optional[int] = None,
    from_: Optional[date] = Query(None, alias="from"),
    to: Optional[date] = None,
    user=Depends(current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    uid = resolve_user_id(user, user_id)
    d_from, d_to = default_range(from_, to)
    rows = conn.execute(
        "SELECT * FROM entries WHERE user_id = ? AND start_utc < ? AND (end_utc IS NULL OR end_utc > ?) "
        "ORDER BY start_utc",
        (uid, to_utc_str(day_start_utc(d_to + timedelta(days=1))), to_utc_str(day_start_utc(d_from))),
    ).fetchall()
    return [entry_out(r) for r in rows]


@router.post("/entries", status_code=201)
def create_entry(data: EntryCreate, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    if not conn.execute("SELECT 1 FROM users WHERE id = ?", (data.user_id,)).fetchone():
        raise HTTPException(404, "Nutzer nicht gefunden")
    start = to_utc_str(data.start)
    end = to_utc_str(data.end) if data.end else None
    _check_entry(conn, data.user_id, start, end)
    now = now_utc_str()
    try:
        cur = conn.execute(
            "INSERT INTO entries(user_id, start_utc, end_utc, source, note, created_at, updated_at, updated_by) "
            "VALUES (?, ?, ?, 'manual', ?, ?, ?, ?)",
            (data.user_id, start, end, data.note, now, now, admin["id"]),
        )
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Nutzer hat bereits eine offene Buchung")
    audit(conn, admin["id"], "entry_created", f"entry:{cur.lastrowid}", f"{start} – {end}")
    return entry_out(conn.execute("SELECT * FROM entries WHERE id = ?", (cur.lastrowid,)).fetchone())


@router.patch("/entries/{entry_id}")
def update_entry(entry_id: int, data: EntryUpdate, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    e = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    if not e:
        raise HTTPException(404, "Buchung nicht gefunden")
    start = to_utc_str(data.start) if data.start else e["start_utc"]
    if data.reopen:
        end = None
    else:
        end = to_utc_str(data.end) if data.end else e["end_utc"]
    note = data.note if data.note is not None else e["note"]
    _check_entry(conn, e["user_id"], start, end, exclude_id=entry_id)
    try:
        conn.execute(
            # eine Korrektur durch den Admin gilt als geprüft
            "UPDATE entries SET start_utc = ?, end_utc = ?, note = ?, updated_at = ?, updated_by = ?, auto_closed = 0 "
            "WHERE id = ?",
            (start, end, note, now_utc_str(), admin["id"], entry_id),
        )
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Nutzer hat bereits eine offene Buchung")
    audit(
        conn, admin["id"], "entry_updated", f"entry:{entry_id}",
        f"vorher {e['start_utc']} – {e['end_utc']}, nachher {start} – {end}",
    )
    return entry_out(conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone())


@router.delete("/entries/{entry_id}")
def delete_entry(entry_id: int, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    e = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    if not e:
        raise HTTPException(404, "Buchung nicht gefunden")
    conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    audit(conn, admin["id"], "entry_deleted", f"entry:{entry_id}",
          f"user:{e['user_id']} {e['start_utc']} – {e['end_utc']}")
    return {"ok": True}


# --- Abwesenheiten ----------------------------------------------------------

class AbsenceCreate(BaseModel):
    user_id: int
    from_day: date
    to_day: Optional[date] = None
    kind: str = Field(description="urlaub, krank, bildungsurlaub, gleittag, mehrarbeit, feiertag, sonstiges")
    half_day: bool = False
    note: Optional[str] = Field(None, max_length=200)
    only_workdays: bool = Field(True, description="Wochenenden, freie Tage und Feiertage überspringen")


def _absence_out(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "user_id": r["user_id"], "day": r["day"], "kind": r["kind"],
            "label": ABSENCE_KINDS.get(r["kind"], r["kind"]), "half_day": bool(r["half_day"]), "note": r["note"]}


@router.get("/absences")
def list_absences(
    user_id: Optional[int] = None,
    from_: Optional[date] = Query(None, alias="from"),
    to: Optional[date] = None,
    user=Depends(current_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    uid = resolve_user_id(user, user_id)
    d_from, d_to = default_range(from_, to)
    rows = conn.execute(
        "SELECT * FROM absences WHERE user_id = ? AND day BETWEEN ? AND ? ORDER BY day",
        (uid, d_from.isoformat(), d_to.isoformat()),
    ).fetchall()
    return [_absence_out(r) for r in rows]


@router.post("/absences", status_code=201)
def create_absences(data: AbsenceCreate, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    if data.kind not in ABSENCE_KINDS:
        raise HTTPException(400, f"Art muss eine von {', '.join(ABSENCE_KINDS)} sein")
    to_day = data.to_day or data.from_day
    if to_day < data.from_day or (to_day - data.from_day).days > 366:
        raise HTTPException(400, "Ungültiger Zeitraum")
    target = conn.execute("SELECT * FROM users WHERE id = ?", (data.user_id,)).fetchone()
    if not target:
        raise HTTPException(404, "Nutzer nicht gefunden")
    holidays = holidays_between(conn, data.from_day, to_day)
    day_target = target_fn(conn, target)
    single = data.from_day == to_day
    count = skipped = 0
    for d in daterange(data.from_day, to_day):
        # Einzelner Tag wird immer gesetzt, bei Zeiträumen werden freie Tage übersprungen
        if data.only_workdays and not single and data.kind != "feiertag":
            if day_target(d) <= 0 or d in holidays:
                skipped += 1
                continue
        conn.execute(
            "INSERT INTO absences(user_id, day, kind, half_day, note) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, day) DO UPDATE SET kind = excluded.kind, half_day = excluded.half_day, "
            "note = excluded.note",
            (data.user_id, d.isoformat(), data.kind, int(data.half_day), data.note),
        )
        count += 1
    audit(conn, admin["id"], "absence_set", f"user:{data.user_id}",
          f"{data.kind}{' ½' if data.half_day else ''} {data.from_day} – {to_day} ({count} Tage)")
    return {"ok": True, "days": count, "skipped": skipped}


@router.delete("/absences/{absence_id}")
def delete_absence(absence_id: int, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT * FROM absences WHERE id = ?", (absence_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Abwesenheit nicht gefunden")
    conn.execute("DELETE FROM absences WHERE id = ?", (absence_id,))
    audit(conn, admin["id"], "absence_deleted", f"user:{row['user_id']}", f"{row['day']} {row['kind']}")
    return {"ok": True}


@router.delete("/absences")
def delete_absence_range(
    user_id: int,
    from_: date = Query(..., alias="from"),
    to: date = Query(...),
    admin=Depends(admin_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    """Alle Abwesenheiten eines Nutzers im Zeitraum entfernen (z. B. Auswahl im Jahreskalender)."""
    cur = conn.execute(
        "DELETE FROM absences WHERE user_id = ? AND day BETWEEN ? AND ?",
        (user_id, from_.isoformat(), to.isoformat()),
    )
    audit(conn, admin["id"], "absence_deleted", f"user:{user_id}", f"{from_} – {to} ({cur.rowcount} Tage)")
    return {"ok": True, "days": cur.rowcount}


# --- Überstunden-Markierung pro Tag -----------------------------------------

class DayFlagIn(BaseModel):
    user_id: int
    day: date
    overtime: bool


@router.put("/dayflags")
def set_day_flag(data: DayFlagIn, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    """Markiert einen Tag als Überstundentag: sein Saldo fließt ins Überstunden-Konto."""
    if data.overtime:
        conn.execute(
            "INSERT INTO day_flags(user_id, day, overtime) VALUES (?, ?, 1) "
            "ON CONFLICT(user_id, day) DO UPDATE SET overtime = 1",
            (data.user_id, data.day.isoformat()),
        )
    else:
        conn.execute("DELETE FROM day_flags WHERE user_id = ? AND day = ?", (data.user_id, data.day.isoformat()))
    audit(conn, admin["id"], "dayflag_set", f"user:{data.user_id}", f"{data.day} Überstunden={data.overtime}")
    return {"ok": True, "day": data.day.isoformat(), "overtime": data.overtime}
