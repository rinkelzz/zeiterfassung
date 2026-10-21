"""Monatsabschluss pro Mitarbeiter.

Regeln:
- Abgeschlossen wird der Reihe nach: Monat M nur, wenn alle Monate davor (ab Erfassungsbeginn)
  abgeschlossen sind. Wieder öffnen geht nur beim zuletzt abgeschlossenen Monat (mit Begründung).
- Nur vergangene Monate, und nur ohne laufende Buchung im Monat.
- Beim Abschluss werden die Kontostände am Monatsende festgeschrieben (Snapshot). Spätere
  Kontostände rechnen ab diesem Snapshot weiter – rückwirkende Änderungen (z. B. Wochenstunden,
  Übertrag) verändern abgeschlossene Monate dadurch nicht mehr.
- In abgeschlossenen Monaten sind Buchungen, Abwesenheiten, Ü-Tage und Arbeitszeit-Verlauf gesperrt.
"""
import sqlite3
from datetime import date, timedelta
from typing import Dict, Iterable, Optional

from fastapi import HTTPException

from .db import audit
from .timeutil import now_utc_str, parse_utc, to_local, today_local

MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
          "September", "Oktober", "November", "Dezember"]


def month_end(month: str) -> date:
    y, m = int(month[:4]), int(month[5:7])
    return date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)


def month_label(month: str) -> str:
    return f"{MONTHS[int(month[5:7]) - 1]} {month[:4]}"


def next_month(month: str) -> str:
    d = month_end(month) + timedelta(days=1)
    return f"{d.year}-{d.month:02d}"


def last_closing(conn: sqlite3.Connection, user_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM month_closings WHERE user_id = ? ORDER BY month DESC LIMIT 1",
                        (user_id,)).fetchone()


def closed_until(conn: sqlite3.Connection, user_id: int) -> Optional[date]:
    """Letzter Tag des zuletzt abgeschlossenen Monats (oder None)."""
    row = last_closing(conn, user_id)
    return month_end(row["month"]) if row else None


def ensure_open(conn: sqlite3.Connection, user_id: int, days: Iterable[Optional[date]]) -> None:
    """409, wenn einer der Tage in einem abgeschlossenen Monat liegt."""
    until = closed_until(conn, user_id)
    if not until:
        return
    for d in days:
        if d is not None and d <= until:
            m = f"{d.year}-{d.month:02d}"
            raise HTTPException(409, f"{month_label(m)} ist abgeschlossen – Änderungen nur nach „Monat wieder öffnen“ "
                                     "(Verwaltung oder Monatsansicht)")


def utc_day(value: Optional[str]) -> Optional[date]:
    return to_local(parse_utc(value)).date() if value else None


def base(conn: sqlite3.Connection, user: sqlite3.Row) -> Dict:
    """Ausgangspunkt für Kontostände: letzter Abschluss oder Erfassungsbeginn + Übertrag."""
    row = last_closing(conn, user["id"])
    if row:
        return {"from": month_end(row["month"]) + timedelta(days=1), "gleitzeit_s": row["gleitzeit_s"],
                "ueberstunden_s": row["ueberstunden_s"], "closed_month": row["month"]}
    from .reports import tracking_start
    return {"from": tracking_start(user), "gleitzeit_s": int(user["carryover_minutes"] or 0) * 60,
            "ueberstunden_s": int(user["carryover_overtime_minutes"] or 0) * 60, "closed_month": None}


def first_open_month(conn: sqlite3.Connection, user: sqlite3.Row) -> Optional[str]:
    row = last_closing(conn, user["id"])
    if row:
        return next_month(row["month"])
    ts = user["tracking_start"]
    return ts[:7] if ts else None


def close_month(conn: sqlite3.Connection, user: sqlite3.Row, month: str, actor_id: int) -> Dict:
    from .report_files import balance_until  # spät importiert (Zyklus)
    from .reports import compute_report

    expected = first_open_month(conn, user)
    if expected and month != expected:
        if month < expected:
            raise HTTPException(409, f"{month_label(month)} ist schon abgeschlossen")
        raise HTTPException(409, f"Bitte zuerst {month_label(expected)} abschließen – Monate werden der Reihe nach abgeschlossen")
    end = month_end(month)
    if end >= today_local():
        raise HTTPException(400, "Nur vergangene Monate können abgeschlossen werden")
    start_utc_limit = to_utc_end(end)
    running = conn.execute("SELECT 1 FROM entries WHERE user_id = ? AND end_utc IS NULL AND start_utc < ?",
                           (user["id"], start_utc_limit)).fetchone()
    if running:
        raise HTTPException(409, f"{user['display_name']} hat noch eine laufende Buchung aus {month_label(month)}")
    bal = balance_until(conn, user, end)
    first = date(end.year, end.month, 1)
    t = compute_report(conn, user, first, end)["totals"]
    conn.execute(
        "INSERT INTO month_closings(user_id, month, closed_at, closed_by, gleitzeit_s, ueberstunden_s, worked_s, target_s) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user["id"], month, now_utc_str(), actor_id, bal["gleitzeit_s"], bal["ueberstunden_s"], t["worked_s"], t["target_s"]),
    )
    audit(conn, actor_id, "month_closed", f"user:{user['id']}",
          f"{month} – Gleitzeit {bal['gleitzeit_s'] // 60} min, Überstunden {bal['ueberstunden_s'] // 60} min")
    return closing_out(conn.execute("SELECT * FROM month_closings WHERE user_id = ? AND month = ?",
                                    (user["id"], month)).fetchone())


def reopen_month(conn: sqlite3.Connection, user: sqlite3.Row, month: str, reason: str, actor_id: int) -> None:
    row = last_closing(conn, user["id"])
    if not row or row["month"] != month:
        if conn.execute("SELECT 1 FROM month_closings WHERE user_id = ? AND month = ?", (user["id"], month)).fetchone():
            raise HTTPException(409, f"Zuerst {month_label(row['month'])} wieder öffnen – "
                                     "es geht nur der zuletzt abgeschlossene Monat")
        raise HTTPException(404, f"{month_label(month)} ist nicht abgeschlossen")
    if len((reason or "").strip()) < 3:
        raise HTTPException(400, "Bitte eine Begründung angeben")
    conn.execute("DELETE FROM month_closings WHERE user_id = ? AND month = ?", (user["id"], month))
    audit(conn, actor_id, "month_reopened", f"user:{user['id']}", f"{month}: {reason.strip()}")


def to_utc_end(d: date) -> str:
    from .timeutil import day_start_utc, to_utc_str
    return to_utc_str(day_start_utc(d + timedelta(days=1)))


def closing_out(row: sqlite3.Row) -> Dict:
    from .timeutil import local_iso
    return {"month": row["month"], "label": month_label(row["month"]), "closed_at": local_iso(row["closed_at"]),
            "closed_by": row["closed_by"], "gleitzeit_s": row["gleitzeit_s"], "ueberstunden_s": row["ueberstunden_s"]}
