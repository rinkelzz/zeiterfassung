"""Stempeln: Kommen, Gehen, Umschalten, Status.

Gedacht für alle Wege gleichzeitig: Weboberfläche (Session), iPhone-Kurzbefehl,
NFC oder Kiosk-Tablet (Bearer-Token). Alle Aufrufe sind idempotent: Wer schon
eingestempelt ist und nochmal „Kommen“ schickt, bekommt nur eine Info zurück.
Das ist wichtig, weil Standort-Automationen gern doppelt auslösen.
"""
import sqlite3
from typing import Optional

from datetime import timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..db import audit, get_db, get_setting
from ..reports import compute_report
from ..security import current_user
from ..timeutil import fmt_duration, local_hhmm, local_iso, now_utc, now_utc_str, parse_utc, to_local, to_utc_str

router = APIRouter(prefix="/api/v1/clock", tags=["Stempeln"])

SOURCES = ("web", "shortcut", "kiosk", "nfc", "manual", "api")


class ClockIn(BaseModel):
    source: str = Field("api", description="web, shortcut, kiosk, nfc, api")
    note: Optional[str] = Field(None, max_length=200)


def _open_entry(conn: sqlite3.Connection, user_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM entries WHERE user_id = ? AND end_utc IS NULL", (user_id,)
    ).fetchone()


def _today_total(conn: sqlite3.Connection, user: sqlite3.Row) -> str:
    today = to_local(now_utc()).date()
    rep = compute_report(conn, user, today, today)
    return rep["totals"]["worked"]


def _status(conn: sqlite3.Connection, user: sqlite3.Row, changed: bool, message: str) -> dict:
    entry = _open_entry(conn, user["id"])
    return {
        "status": "in" if entry else "out",
        "changed": changed,
        "message": message,
        "since": local_iso(entry["start_utc"]) if entry else None,
        "today_total": _today_total(conn, user),
        "user": user["display_name"],
    }


def _source(data: Optional[ClockIn], query_source: Optional[str] = None) -> str:
    """Stempelweg aus ?source=… (einfach im Kurzbefehl einzutragen) oder dem JSON-Body."""
    src = query_source or (data.source if data else None) or "api"
    return src if src in SOURCES else "api"


def travel_minutes(conn: sqlite3.Connection, source: str, direction: str) -> int:
    """Wegezeit in Minuten, falls für diesen Stempelweg eingeschaltet (direction: in/out)."""
    sources = [x.strip() for x in (get_setting(conn, "travel_sources") or "").split(",") if x.strip()]
    if source not in sources:
        return 0
    try:
        return max(0, min(120, int(get_setting(conn, f"travel_{direction}_minutes") or 0)))
    except ValueError:
        return 0


def do_clock_in(conn: sqlite3.Connection, user: sqlite3.Row, data: Optional[ClockIn], source: Optional[str] = None) -> dict:
    src = _source(data, source)
    entry = _open_entry(conn, user["id"])
    if entry:
        since = local_hhmm(parse_utc(entry["start_utc"]))
        return _status(conn, user, False, f"Bereits eingestempelt seit {since} Uhr")
    now = now_utc()
    travel = travel_minutes(conn, src, "in")
    start = now - timedelta(minutes=travel)
    # nie vor das Ende der letzten Buchung (z. B. kurz raus und wieder rein)
    last = conn.execute("SELECT MAX(end_utc) AS e FROM entries WHERE user_id = ?", (user["id"],)).fetchone()["e"]
    if last and parse_utc(last) > start:
        start = parse_utc(last)
        travel = max(0, int((now - start).total_seconds() // 60))
    try:
        cur = conn.execute(
            "INSERT INTO entries(user_id, start_utc, source, note, created_at, travel_in_min) VALUES (?, ?, ?, ?, ?, ?)",
            (user["id"], to_utc_str(start), src, data.note if data else None, to_utc_str(now), travel),
        )
    except sqlite3.IntegrityError:  # zwei Auslöser gleichzeitig
        return _status(conn, user, False, "Bereits eingestempelt")
    audit(conn, user["id"], "clock_in", f"entry:{cur.lastrowid}", src + (f" (+{travel} min Wegezeit)" if travel else ""))
    msg = f"Eingestempelt um {local_hhmm(now)} Uhr"
    if travel:
        msg += f" – mit {travel} min Wegezeit ab {local_hhmm(start)} Uhr"
    return _status(conn, user, True, msg)


def do_clock_out(conn: sqlite3.Connection, user: sqlite3.Row, data: Optional[ClockIn], source: Optional[str] = None) -> dict:
    src = _source(data, source)
    entry = _open_entry(conn, user["id"])
    if not entry:
        return _status(conn, user, False, "Du bist nicht eingestempelt")
    now = now_utc()
    travel = travel_minutes(conn, src, "out")
    end = now + timedelta(minutes=travel)
    note = entry["note"]
    if data and data.note:
        note = f"{note} | {data.note}" if note else data.note
    conn.execute(
        "UPDATE entries SET end_utc = ?, note = ?, updated_at = ?, travel_out_min = ? WHERE id = ?",
        (to_utc_str(end), note, to_utc_str(now), travel, entry["id"]),
    )
    audit(conn, user["id"], "clock_out", f"entry:{entry['id']}", src + (f" (+{travel} min Wegezeit)" if travel else ""))
    status = _status(conn, user, True, "")
    status["message"] = f"Ausgestempelt um {local_hhmm(now)} Uhr"
    if travel:
        status["message"] += f" – mit {travel} min Wegezeit bis {local_hhmm(end)} Uhr"
    status["message"] += f" – heute {status['today_total']} h"
    return status


@router.get("/status")
def clock_status(user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    entry = _open_entry(conn, user["id"])
    msg = f"Eingestempelt seit {local_hhmm(parse_utc(entry['start_utc']))} Uhr" if entry else "Ausgestempelt"
    return _status(conn, user, False, msg)


SOURCE_Q = Query(None, description="Stempelweg, z. B. nfc oder shortcut (alternativ im JSON-Body)")


@router.post("/in")
def clock_in(data: Optional[ClockIn] = Body(None), source: Optional[str] = SOURCE_Q,
             user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    return do_clock_in(conn, user, data, source)


@router.post("/out")
def clock_out(data: Optional[ClockIn] = Body(None), source: Optional[str] = SOURCE_Q,
              user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    return do_clock_out(conn, user, data, source)


@router.post("/toggle")
def clock_toggle(data: Optional[ClockIn] = Body(None), source: Optional[str] = SOURCE_Q,
                 user=Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Für NFC-Tag oder einen einzelnen Button: kommt, wenn draußen – geht, wenn drin."""
    if _open_entry(conn, user["id"]):
        return do_clock_out(conn, user, data, source)
    return do_clock_in(conn, user, data, source)
