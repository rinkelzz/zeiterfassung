"""CSV-Import mit Vorschau – vor allem für die Übernahme aus rinkelzz/gleitzeit.

Drei Arten:
- entries:   Buchungen. Format des Gleitzeit-Exports `datum;checkin;checkout;dauer_h;notiz`,
             aber auch eigene Tabellen mit Spalten wie Datum/Kommen/Gehen/Notiz.
- absences:  Abwesenheiten, z. B. Tabellenexport von `absences` (date;type;half_day;note).
- dayflags:  Überstunden-Markierungen, Tabellenexport von `day_flags` (date;overtime).

Ablauf: erst mit commit=false eine Vorschau holen, dann mit commit=true übernehmen.
Vorhandene Daten werden nie stillschweigend überschrieben; Dubletten und
Überschneidungen werden erkannt und übersprungen.
"""
import csv
import io
import sqlite3
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..db import audit, get_db
from ..reports import ABSENCE_KINDS
from ..security import admin_user
from ..timeutil import now_utc_str, to_utc_str

router = APIRouter(prefix="/api/v1/import", tags=["Import"])

MAX_BYTES = 5 * 1024 * 1024

TYPE_MAP = {
    # Gleitzeit-App (englische Typen)
    "vacation": "urlaub", "sick": "krank", "holiday": "feiertag", "gleitzeit": "gleittag",
    "overtime_withdrawal": "mehrarbeit", "bildungsurlaub": "bildungsurlaub", "other": "sonstiges",
    # deutsche Bezeichnungen
    "urlaub": "urlaub", "krank": "krank", "feiertag": "feiertag", "gleittag": "gleittag",
    "mehrarbeit": "mehrarbeit", "mehrarbeit-entnahme": "mehrarbeit", "entnahme mehrarbeit": "mehrarbeit",
    "sonstiges": "sonstiges", "sonderurlaub": "sonstiges", "frei": "gleittag",
}
COLS = {
    "date": ("datum", "date", "day", "tag"),
    "start": ("checkin", "checkin_time", "kommen", "start", "beginn", "von"),
    "end": ("checkout", "checkout_time", "gehen", "ende", "end", "bis"),
    "note": ("notiz", "note", "bemerkung", "kommentar"),
    "type": ("type", "typ", "art", "kind", "abwesenheit"),
    "half": ("half_day", "halbtag", "halber_tag", "halb"),
    "overtime": ("overtime", "ueberstunden", "überstunden", "ue"),
}


class ImportIn(BaseModel):
    user_id: int
    kind: str = Field("entries", description="entries, absences oder dayflags")
    csv_text: str
    commit: bool = False


# --- Parser-Hilfen ----------------------------------------------------------

def _rows(text: str) -> Tuple[Optional[Dict[str, int]], List[Tuple[int, List[str]]]]:
    text = text.lstrip("﻿")
    sample = text[:2000]
    delim = ";" if sample.count(";") >= sample.count(",") else ","
    if "\t" in sample and sample.count("\t") > sample.count(delim):
        delim = "\t"
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    lines = [(i + 1, [c.strip() for c in row]) for i, row in enumerate(reader) if any(c.strip() for c in row)]
    if not lines:
        return None, []
    first = [c.lower().strip('"') for c in lines[0][1]]
    header = None
    if any(c in names for c in first for names in COLS.values()):
        header = {}
        for key, names in COLS.items():
            for idx, c in enumerate(first):
                if c in names and key not in header:
                    header[key] = idx
        lines = lines[1:]
    return header, lines


def _get(row: List[str], header: Optional[Dict[str, int]], key: str, pos: Optional[int]) -> str:
    idx = header.get(key) if header is not None else pos
    if idx is None or idx >= len(row):
        return ""
    v = row[idx].strip()
    return "" if v.upper() == "NULL" else v


def _parse_date(s: str) -> Optional[date]:
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(s[:10] if fmt == "%Y-%m-%d" else s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_dt(s: str, day: Optional[date]) -> Optional[datetime]:
    s = s.strip().replace("T", " ")
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    if day:
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                t = datetime.strptime(s, fmt).time()
                return datetime.combine(day, t)
            except ValueError:
                continue
    return None


def _truthy(s: str) -> bool:
    return s.strip().lower() in ("1", "true", "ja", "yes", "x", "½", "0.5", "0,5")


# --- Buchungen --------------------------------------------------------------

def _plan_entries(conn: sqlite3.Connection, user_id: int, text: str) -> List[dict]:
    header, lines = _rows(text)
    now = now_utc_str()
    planned: List[Tuple[str, str]] = []
    out = []
    for lineno, row in lines:
        day = _parse_date(_get(row, header, "date", 0))
        raw_in, raw_out = _get(row, header, "start", 1), _get(row, header, "end", 2)
        note = _get(row, header, "note", 4)[:200] or None
        item = {"line": lineno, "raw": ";".join(row)[:120], "note": note}
        start = _parse_dt(raw_in, day)
        end = _parse_dt(raw_out, day or (start.date() if start else None))
        if not start:
            out.append({**item, "status": "fehler", "message": "Beginn nicht lesbar"})
            continue
        if not end:
            out.append({**item, "start": start.isoformat(" ", "minutes"), "status": "fehler",
                        "message": "kein Ende (offene Buchung) – bitte nachtragen"})
            continue
        if end <= start and len(raw_out) <= 8:  # nur Uhrzeit angegeben → Nachtdienst bis zum Folgetag
            end += timedelta(days=1)
        item.update(start=start.isoformat(" ", "minutes"), end=end.isoformat(" ", "minutes"),
                    hours=round((end - start).total_seconds() / 3600, 2))
        if end <= start:
            out.append({**item, "status": "fehler", "message": "Ende vor Beginn"})
            continue
        s_utc, e_utc = to_utc_str(start), to_utc_str(end)
        if e_utc > now:
            out.append({**item, "status": "fehler", "message": "liegt in der Zukunft"})
            continue
        if conn.execute("SELECT 1 FROM entries WHERE user_id = ? AND start_utc = ? AND end_utc = ?",
                        (user_id, s_utc, e_utc)).fetchone():
            out.append({**item, "status": "doppelt", "message": "schon vorhanden"})
            continue
        clash = conn.execute(
            "SELECT start_utc FROM entries WHERE user_id = ? AND start_utc < ? AND COALESCE(end_utc, '9999') > ?",
            (user_id, e_utc, s_utc)).fetchone()
        if clash or any(ps < e_utc and pe > s_utc for ps, pe in planned):
            out.append({**item, "status": "konflikt", "message": "überschneidet sich mit einer anderen Buchung"})
            continue
        planned.append((s_utc, e_utc))
        out.append({**item, "status": "neu", "start_utc": s_utc, "end_utc": e_utc})
    return out


# --- Abwesenheiten und Ü-Markierungen ---------------------------------------

def _plan_absences(conn: sqlite3.Connection, user_id: int, text: str) -> List[dict]:
    header, lines = _rows(text)
    out = []
    seen = set()
    for lineno, row in lines:
        # Tabellenexport ohne Kopfzeile: id;date;type;half_day;note
        if header is None and len(row) >= 3 and row[0].isdigit():
            row = row[1:]
        day = _parse_date(_get(row, header, "date", 0))
        raw_type = _get(row, header, "type", 1)
        kind = TYPE_MAP.get(raw_type.lower())
        half = _truthy(_get(row, header, "half", 2))
        note = _get(row, header, "note", 3)[:200] or None
        item = {"line": lineno, "raw": ";".join(row)[:120], "day": day.isoformat() if day else None,
                "kind": kind, "label": ABSENCE_KINDS.get(kind or "", raw_type), "half_day": half, "note": note}
        if not day:
            out.append({**item, "status": "fehler", "message": "Datum nicht lesbar"})
            continue
        if not kind:
            out.append({**item, "status": "fehler", "message": f"unbekannte Art „{raw_type}“"})
            continue
        if day in seen:
            out.append({**item, "status": "doppelt", "message": "Datum mehrfach in der Datei"})
            continue
        seen.add(day)
        old = conn.execute("SELECT kind, half_day FROM absences WHERE user_id = ? AND day = ?",
                           (user_id, day.isoformat())).fetchone()
        if old and old["kind"] == kind and bool(old["half_day"]) == half:
            out.append({**item, "status": "doppelt", "message": "schon vorhanden"})
        elif old:
            out.append({**item, "status": "konflikt",
                        "message": f"schon eingetragen als {ABSENCE_KINDS.get(old['kind'], old['kind'])} – wird nicht geändert"})
        else:
            out.append({**item, "status": "neu"})
    return out


def _plan_dayflags(conn: sqlite3.Connection, user_id: int, text: str) -> List[dict]:
    header, lines = _rows(text)
    out = []
    for lineno, row in lines:
        day = _parse_date(_get(row, header, "date", 0))
        flag = _get(row, header, "overtime", 1)
        item = {"line": lineno, "raw": ";".join(row)[:120], "day": day.isoformat() if day else None}
        if not day:
            out.append({**item, "status": "fehler", "message": "Datum nicht lesbar"})
        elif flag and not _truthy(flag):
            out.append({**item, "status": "übersprungen", "message": "nicht als Überstunden markiert"})
        elif conn.execute("SELECT 1 FROM day_flags WHERE user_id = ? AND day = ? AND overtime = 1",
                          (user_id, day.isoformat())).fetchone():
            out.append({**item, "status": "doppelt", "message": "schon markiert"})
        else:
            out.append({**item, "status": "neu"})
    return out


PLANNERS = {"entries": _plan_entries, "absences": _plan_absences, "dayflags": _plan_dayflags}


@router.post("")
def run_import(data: ImportIn, admin=Depends(admin_user), conn: sqlite3.Connection = Depends(get_db)):
    if data.kind not in PLANNERS:
        raise HTTPException(400, "Art muss entries, absences oder dayflags sein")
    if len(data.csv_text.encode("utf-8")) > MAX_BYTES:
        raise HTTPException(400, "Datei zu groß (max. 5 MB)")
    if not conn.execute("SELECT 1 FROM users WHERE id = ?", (data.user_id,)).fetchone():
        raise HTTPException(404, "Nutzer nicht gefunden")

    rows = PLANNERS[data.kind](conn, data.user_id, data.csv_text)
    from ..closing import closed_until, utc_day
    until = closed_until(conn, data.user_id)
    if until:  # nichts in abgeschlossene Monate importieren
        for r in rows:
            if r["status"] != "neu":
                continue
            day = utc_day(r["start_utc"]) if r.get("start_utc") else (
                date.fromisoformat(r["day"]) if r.get("day") else None)
            if day and day <= until:
                r["status"], r["message"] = "fehler", "Monat ist abgeschlossen"
    summary: Dict[str, int] = {}
    for r in rows:
        summary[r["status"]] = summary.get(r["status"], 0) + 1

    imported = 0
    if data.commit:
        now = now_utc_str()
        conn.execute("BEGIN")
        try:
            for r in rows:
                if r["status"] != "neu":
                    continue
                if data.kind == "entries":
                    conn.execute(
                        "INSERT INTO entries(user_id, start_utc, end_utc, source, note, created_at, updated_at, updated_by) "
                        "VALUES (?, ?, ?, 'import', ?, ?, ?, ?)",
                        (data.user_id, r["start_utc"], r["end_utc"], r["note"], now, now, admin["id"]),
                    )
                elif data.kind == "absences":
                    conn.execute(
                        "INSERT INTO absences(user_id, day, kind, half_day, note) VALUES (?, ?, ?, ?, ?)",
                        (data.user_id, r["day"], r["kind"], int(r["half_day"]), r["note"]),
                    )
                else:
                    conn.execute(
                        "INSERT INTO day_flags(user_id, day, overtime) VALUES (?, ?, 1) "
                        "ON CONFLICT(user_id, day) DO UPDATE SET overtime = 1",
                        (data.user_id, r["day"]),
                    )
                imported += 1
            audit(conn, admin["id"], "import", f"user:{data.user_id}", f"{data.kind}: {imported} übernommen, {summary}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    for r in rows:  # interne Felder nicht ausliefern
        r.pop("start_utc", None)
        r.pop("end_utc", None)
    return {"kind": data.kind, "committed": data.commit, "imported": imported, "summary": summary, "rows": rows}
