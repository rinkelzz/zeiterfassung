"""Auswertungslogik: Buchungen auf lokale Tage verteilen, Soll/Ist/Pausen rechnen,
Gleitzeit-, Überstunden- und Urlaubskonto führen.

Buchungen über Mitternacht (z. B. 24-h-Dienste auf der Wache) werden an der
lokalen Mitternacht aufgeteilt, damit jeder Tag seine echten Stunden bekommt.

Konten-Modell (übernommen aus rinkelzz/gleitzeit):
- Jeder Tag landet im Gleitzeit-Konto. Ist er als „Ü“-Tag markiert, geht nur die Zeit über
  dem Tagessoll ins Überstunden-Konto (am Wochenende also alles); ein Minus bleibt Gleitzeit.
  Eine „Mehrarbeit-Entnahme“ belastet das Überstunden-Konto.
- Urlaub, Krank, Bildungsurlaub, Feiertag, Sonderurlaub: kein Soll (halber Tag: halbes Soll).
- Gleittag / Mehrarbeit-Entnahme: Soll bleibt, es wird nicht gearbeitet → Minus im jeweiligen Konto.
"""
import sqlite3
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from .db import get_setting
from .holidays import holidays_for_year
from .timeutil import (
    day_start_utc,
    daterange,
    fmt_duration,
    local_hhmm,
    now_utc,
    parse_utc,
    to_local,
    to_utc_str,
)

WEEKDAYS_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
ABSENCE_KINDS = {
    "urlaub": "Urlaub",
    "krank": "Krank",
    "bildungsurlaub": "Bildungsurlaub",
    "gleittag": "Gleittag",
    "mehrarbeit": "Mehrarbeit-Entnahme",
    "feiertag": "Feiertag",
    "sonstiges": "Sonderurlaub / frei",
}
# Arten, bei denen das Soll entfällt (bezahlt frei)
ZERO_TARGET_KINDS = {"urlaub", "krank", "bildungsurlaub", "feiertag", "sonstiges"}
MIN_BREAK_S = 15 * 60  # nach ArbZG zählen nur Pausen ab 15 Minuten
BREAK_MODES = {"none": "kein Abzug", "arbzg": "nach ArbZG (6 h → 30 min, 9 h → 45 min)", "fixed": "fester Abzug pro Arbeitstag"}


def required_break_s(worked_s: float) -> int:
    if worked_s > 9 * 3600:
        return 45 * 60
    if worked_s > 6 * 3600:
        return 30 * 60
    return 0


FOREVER = "1970-01-01"  # Verlaufseintrag „von Anfang an“


def _target(weekly_hours: float, workdays_csv: str, d: date) -> float:
    workdays = [int(x) for x in (workdays_csv or "").split(",") if x]
    if not workdays or d.isoweekday() not in workdays:
        return 0.0
    return weekly_hours * 3600 / len(workdays)


def daily_target_s(user: sqlite3.Row, d: date) -> float:
    """Tagessoll aus den aktuellen Werten des Nutzers (ohne Verlauf)."""
    return _target(user["weekly_hours"], user["workdays"], d)


def schedules(conn: sqlite3.Connection, user_id: int) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM work_schedules WHERE user_id = ? ORDER BY valid_from", (user_id,)
    ).fetchall()


def target_fn(conn: sqlite3.Connection, user: sqlite3.Row):
    """Liefert eine Funktion d → Tagessoll, die den zum Datum gültigen Verlaufseintrag nutzt."""
    rows = [(date.fromisoformat(r["valid_from"]), r["weekly_hours"], r["workdays"]) for r in schedules(conn, user["id"])]
    if not rows:
        return lambda d: daily_target_s(user, d)

    def fn(d: date) -> float:
        current = None
        for start, hours, days in rows:
            if start <= d:
                current = (hours, days)
            else:
                break
        if current is None:  # vor dem ersten Eintrag: der früheste gilt
            current = (rows[0][1], rows[0][2])
        return _target(current[0], current[1], d)
    return fn


def schedule_on(conn: sqlite3.Connection, user_id: int, d: date) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM work_schedules WHERE user_id = ? AND valid_from <= ? ORDER BY valid_from DESC LIMIT 1",
        (user_id, d.isoformat()),
    ).fetchone()


def sync_current_schedule(conn: sqlite3.Connection, user_id: int) -> None:
    """users.weekly_hours/workdays spiegeln den heute gültigen Eintrag (für Anzeige und Rückfall)."""
    row = schedule_on(conn, user_id, to_local(now_utc()).date())
    if row is None:
        row = conn.execute("SELECT * FROM work_schedules WHERE user_id = ? ORDER BY valid_from LIMIT 1", (user_id,)).fetchone()
    if row is not None:
        conn.execute("UPDATE users SET weekly_hours = ?, workdays = ? WHERE id = ?",
                     (row["weekly_hours"], row["workdays"], user_id))


def tracking_start(user: sqlite3.Row) -> Optional[date]:
    value = user["tracking_start"] if "tracking_start" in user.keys() else None
    return date.fromisoformat(value) if value else None


def fetch_entries(conn: sqlite3.Connection, user_id: int, start: datetime, end: datetime) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM entries WHERE user_id = ? AND start_utc < ? "
        "AND (end_utc IS NULL OR end_utc > ?) ORDER BY start_utc",
        (user_id, to_utc_str(end), to_utc_str(start)),
    ).fetchall()


def break_rule(conn: sqlite3.Connection) -> Dict:
    mode = get_setting(conn, "break_mode")
    try:
        minutes = max(0, min(120, int(get_setting(conn, "break_minutes") or 0)))
    except ValueError:
        minutes = 30
    return {"mode": mode if mode in BREAK_MODES else "none", "minutes": minutes}


def holidays_between(conn: sqlite3.Connection, d_from: date, d_to: date) -> Dict[date, str]:
    bundesland = get_setting(conn, "bundesland")
    free_days = get_setting(conn, "company_free_days")
    result: Dict[date, str] = {}
    for year in range(d_from.year, d_to.year + 1):
        result.update(holidays_for_year(year, bundesland, free_days))
    return result


def _break_for_day(rule: Dict, gross: float, gaps: float, half_day: bool) -> float:
    if gross <= 0:
        return 0
    if rule["mode"] == "arbzg":
        need = required_break_s(gross)
    elif rule["mode"] == "fixed":
        need = rule["minutes"] * 60 * (0.5 if half_day else 1)
    else:
        return 0
    return min(max(0, need - gaps), gross)


def compute_report(
    conn: sqlite3.Connection,
    user: sqlite3.Row,
    d_from: date,
    d_to: date,
    now: Optional[datetime] = None,
) -> Dict:
    now = now or now_utc()
    today = to_local(now).date()
    range_start = day_start_utc(d_from)
    range_end = day_start_utc(d_to + timedelta(days=1))
    rule = break_rule(conn)

    absences = {
        r["day"]: r
        for r in conn.execute(
            "SELECT * FROM absences WHERE user_id = ? AND day BETWEEN ? AND ?",
            (user["id"], d_from.isoformat(), d_to.isoformat()),
        )
    }
    flags = {
        r["day"]
        for r in conn.execute(
            "SELECT day FROM day_flags WHERE user_id = ? AND overtime = 1 AND day BETWEEN ? AND ?",
            (user["id"], d_from.isoformat(), d_to.isoformat()),
        )
    }
    holidays = holidays_between(conn, d_from, d_to)
    t_start = tracking_start(user)
    day_target = target_fn(conn, user)

    # Intervalle je lokalem Tag sammeln
    per_day: Dict[date, List[tuple]] = {d: [] for d in daterange(d_from, d_to)}
    entries_per_day: Dict[date, List[int]] = {d: [] for d in daterange(d_from, d_to)}
    open_days = set()
    travel_per_day: Dict[date, int] = {}
    for e in fetch_entries(conn, user["id"], range_start, range_end):
        keys = e.keys()
        for col, ts in (("travel_in_min", e["start_utc"]), ("travel_out_min", e["end_utc"])):
            minutes = e[col] if col in keys else 0
            if minutes and ts:
                td = to_local(parse_utc(ts)).date()
                if d_from <= td <= d_to:
                    travel_per_day[td] = travel_per_day.get(td, 0) + int(minutes)
        start = max(parse_utc(e["start_utc"]), range_start)
        is_open = e["end_utc"] is None
        end = min(parse_utc(e["end_utc"]) if not is_open else now, range_end)
        if end <= start:
            continue
        d = to_local(start).date()
        while d <= d_to:
            seg_start = max(start, day_start_utc(d))
            seg_end = min(end, day_start_utc(d + timedelta(days=1)))
            if seg_end > seg_start:
                per_day[d].append((seg_start, seg_end))
                entries_per_day[d].append(e["id"])
                if is_open and seg_end == end:
                    open_days.add(d)
            if end <= day_start_utc(d + timedelta(days=1)):
                break
            d += timedelta(days=1)

    days = []
    tot = {"target": 0.0, "worked": 0.0, "break": 0.0, "gleitzeit": 0.0, "ueberstunden": 0.0, "vacation": 0.0}
    for d in daterange(d_from, d_to):
        key = d.isoformat()
        intervals = sorted(per_day[d])
        gross = sum((b - a).total_seconds() for a, b in intervals)
        gaps = sum(
            g
            for g in ((intervals[i + 1][0] - intervals[i][1]).total_seconds() for i in range(len(intervals) - 1))
            if g >= MIN_BREAK_S
        )
        ab = absences.get(key)
        kind = ab["kind"] if ab else None
        half = bool(ab["half_day"]) if ab else False
        holiday = holidays.get(d)
        regular = day_target(d)

        deducted = _break_for_day(rule, gross, gaps, half and kind in ZERO_TARGET_KINDS)
        worked = gross - deducted

        if kind in ZERO_TARGET_KINDS:
            target = regular / 2 if half else 0.0
            label = ABSENCE_KINDS[kind] + (" (½)" if half else "")
        elif kind:  # gleittag / mehrarbeit: Soll bleibt
            target = 0.0 if holiday else regular
            label = ABSENCE_KINDS.get(kind, kind) + (" (½)" if half else "")
        elif holiday:
            kind, target, label = "feiertag", 0.0, holiday
        else:
            target, label = regular, None

        before_start = t_start is not None and d < t_start
        counts = not before_start and d <= today
        if not counts:  # Zukunft und Zeit vor Erfassungsbeginn zählen nicht
            target = 0.0
        diff = worked - target if (counts or worked) else 0.0
        if before_start:
            diff = 0.0  # vor Erfassungsbeginn fließt nichts in die Konten
        # Kontenzuordnung:
        # - Mehrarbeit-Entnahme: ganzer Tagessaldo aufs Überstunden-Konto
        # - „Ü“-Tag: nur die Zeit ÜBER dem Tagessoll aufs Überstunden-Konto, ein Minus bleibt Gleitzeit
        # - sonst: alles Gleitzeit
        if kind == "mehrarbeit":
            part_gz, part_ue = 0.0, diff
        elif key in flags:
            part_gz, part_ue = min(diff, 0.0), max(diff, 0.0)
        else:
            part_gz, part_ue = diff, 0.0
        account = "ueberstunden" if part_ue else "gleitzeit"

        vac = 0.0
        if kind == "urlaub" and regular > 0 and not holiday and not before_start:
            vac = 0.5 if half else 1.0

        tot["target"] += target
        tot["worked"] += worked
        tot["break"] += deducted
        tot["gleitzeit"] += part_gz
        tot["ueberstunden"] += part_ue
        tot["vacation"] += vac if d <= today else 0
        days.append(
            {
                "date": key,
                "weekday": WEEKDAYS_DE[d.weekday()],
                "first_in": local_hhmm(intervals[0][0]) if intervals else None,
                "last_out": (
                    None if d in open_days
                    # Schicht geht über Mitternacht weiter → 24:00 statt 00:00
                    else "24:00" if intervals[-1][1] == day_start_utc(d + timedelta(days=1))
                    else local_hhmm(intervals[-1][1])
                ) if intervals else None,
                "running": d in open_days,
                "segments": len(intervals),
                "entry_ids": sorted(set(entries_per_day[d])),
                "absence": kind,
                "absence_id": ab["id"] if ab else None,
                "half_day": half,
                "absence_label": label,
                "holiday": holiday,
                "workday": regular > 0,
                "before_start": before_start,
                "overtime_day": key in flags,
                "account": account,
                "diff_gleitzeit_s": int(part_gz),
                "diff_ueberstunden_s": int(part_ue),
                "vacation_days": vac,
                "travel_min": travel_per_day.get(d, 0),
                "target_s": int(target),
                "worked_s": int(worked),
                "break_deducted_s": int(deducted),
                "diff_s": int(diff),
                "target": fmt_duration(target),
                "worked": fmt_duration(worked),
                "diff": fmt_duration(diff),
            }
        )

    diff_total = tot["gleitzeit"] + tot["ueberstunden"]
    return {
        "user": {"id": user["id"], "username": user["username"], "display_name": user["display_name"]},
        "from": d_from.isoformat(),
        "to": d_to.isoformat(),
        "break_rule": rule,
        "days": days,
        "totals": {
            "target_s": int(tot["target"]),
            "worked_s": int(tot["worked"]),
            "break_deducted_s": int(tot["break"]),
            "diff_s": int(diff_total),
            "gleitzeit_s": int(tot["gleitzeit"]),
            "ueberstunden_s": int(tot["ueberstunden"]),
            "target": fmt_duration(tot["target"]),
            "worked": fmt_duration(tot["worked"]),
            "diff": fmt_duration(diff_total),
            "gleitzeit": fmt_duration(tot["gleitzeit"]),
            "ueberstunden": fmt_duration(tot["ueberstunden"]),
            "vacation_days": tot["vacation"],
            "absence_days": {
                k: sum(
                    (0.5 if x["half_day"] else 1)
                    for x in days
                    if x["absence"] == k and x["workday"] and not x["before_start"]
                    and (k == "feiertag" or not x["holiday"])
                )
                for k in ABSENCE_KINDS
            },
        },
    }


def account_balance(conn: sqlite3.Connection, user: sqlite3.Row) -> Dict:
    """Kontostände vom Erfassungsbeginn bis heute, inklusive Überträge."""
    today = to_local(now_utc()).date()
    start = tracking_start(user) or today
    carry_g = int(user["carryover_minutes"] or 0) * 60
    carry_u = int(user["carryover_overtime_minutes"] or 0) * 60
    g = u = 0
    if start <= today:
        t = compute_report(conn, user, start, today)["totals"]
        g, u = t["gleitzeit_s"], t["ueberstunden_s"]
    gleitzeit, ueberstunden = carry_g + g, carry_u + u
    return {
        "since": start.isoformat(),
        "carryover_s": carry_g,
        "carryover": fmt_duration(carry_g),
        "carryover_overtime_s": carry_u,
        "gleitzeit_s": gleitzeit,
        "gleitzeit": fmt_duration(gleitzeit),
        "ueberstunden_s": ueberstunden,
        "ueberstunden": fmt_duration(ueberstunden),
        # Abwärtskompatibel: balance = Summe beider Konten
        "balance_s": gleitzeit + ueberstunden,
        "balance": fmt_duration(gleitzeit + ueberstunden),
    }


def vacation_account(conn: sqlite3.Connection, user: sqlite3.Row, year: int) -> Optional[Dict]:
    """Urlaubskonto eines Jahres. Resturlaub wird automatisch ins Folgejahr übertragen."""
    today = to_local(now_utc()).date()
    start = tracking_start(user) or today
    if year < start.year:
        return None
    rows = conn.execute(
        "SELECT day, half_day FROM absences WHERE user_id = ? AND kind = 'urlaub' AND day >= ? AND day <= ?",
        (user["id"], start.isoformat(), f"{year}-12-31"),
    ).fetchall()
    holidays = holidays_between(conn, date(start.year, 1, 1), date(year, 12, 31))
    day_target = target_fn(conn, user)
    taken_by_year: Dict[int, List[float]] = {}
    for r in rows:
        d = date.fromisoformat(r["day"])
        if day_target(d) <= 0 or d in holidays:
            continue
        past, future = taken_by_year.setdefault(d.year, [0.0, 0.0])
        val = 0.5 if r["half_day"] else 1.0
        if d <= today:
            taken_by_year[d.year][0] += val
        else:
            taken_by_year[d.year][1] += val

    entitlement = float(user["vacation_days_per_year"] or 0)
    carry = float(user["carryover_vacation"] or 0)
    for y in range(start.year, year + 1):
        taken, planned = taken_by_year.get(y, [0.0, 0.0])
        remaining = entitlement + carry - taken - planned
        if y == year:
            return {
                "year": year,
                "entitlement": entitlement,
                "carryover": carry,
                "taken": taken,
                "planned": planned,
                "remaining": remaining,
            }
        carry = remaining
    return None
