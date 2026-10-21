"""Vergessenes Ausstempeln.

Läuft eine Buchung länger als open_limit_hours (Standard 12 h):
1. Erinnerung per Mail an den Mitarbeiter und alle Admins mit Mailadresse (einmal pro Buchung).
2. Ist sie um Mitternacht nach Erreichen der Grenze immer noch offen und open_auto_close an,
   endet sie automatisch zur Grenze (Start + Grenze). Die Buchung wird als auto_closed markiert,
   der Tag erscheint in Monatsansicht und Bericht mit „bitte prüfen“. Korrigiert ein Admin die
   Buchung, verschwindet die Markierung.
"""
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from .db import audit, get_setting
from .timeutil import day_start_utc, local_hhmm, now_utc, parse_utc, to_local, to_utc_str

AUTO_NOTE = "automatisch beendet – bitte prüfen"


def limit_hours(conn: sqlite3.Connection) -> float:
    try:
        return max(0.0, float(get_setting(conn, "open_limit_hours") or 0))
    except ValueError:
        return 0.0


def close_time(start: datetime, limit_h: float) -> datetime:
    """Zeitpunkt, zu dem automatisch beendet wird: Mitternacht (lokal) nach Erreichen der Grenze."""
    reached = start + timedelta(hours=limit_h)
    return day_start_utc(to_local(reached).date() + timedelta(days=1))


def _public_url(conn) -> str:
    from .config import settings
    return (get_setting(conn, "public_url") or settings.public_url or "").rstrip("/")


def _remind(conn, user: sqlite3.Row, entry: sqlite3.Row, limit_h: float, auto: bool) -> List[str]:
    from .mailer import MailError, send_mail, smtp_config, valid_email
    if not smtp_config(conn)["host"]:
        return []
    start = parse_utc(entry["start_utc"])
    s_local = to_local(start)
    end_auto = to_local(start + timedelta(hours=limit_h))
    hours = int(limit_h) if limit_h == int(limit_h) else limit_h
    lines = [
        f"Hallo {user['display_name']},",
        "",
        f"du bist seit {s_local:%d.%m.%Y} um {local_hhmm(start)} Uhr eingestempelt – das sind über {hours} Stunden.",
        "Falls du vergessen hast auszustempeln, hole das bitte jetzt nach oder sag der Verwaltung Bescheid.",
    ]
    if auto:
        lines += ["", f"Passiert bis Mitternacht nichts, wird die Buchung automatisch auf {end_auto:%d.%m.} "
                      f"{end_auto:%H:%M} Uhr beendet und zur Prüfung markiert."]
    url = _public_url(conn)
    if url:
        lines += ["", url]
    lines += ["", "Diese Mail wurde automatisch von der Zeiterfassung verschickt."]
    body = "\n".join(lines)
    recipients = []
    if user["email"] and valid_email(user["email"]):
        recipients.append(user["email"])
    for a in conn.execute("SELECT email FROM users WHERE is_admin = 1 AND active = 1 AND email IS NOT NULL AND email != ''"):
        if valid_email(a["email"]) and a["email"] not in recipients:
            recipients.append(a["email"])
    sent = []
    for to in recipients:
        try:
            send_mail(conn, to, f"Zeiterfassung: {user['display_name']} noch eingestempelt?", body)
            sent.append(to)
        except MailError as e:
            print(f"[zeiterfassung] Erinnerung an {to} fehlgeschlagen: {e}", flush=True)
    return sent


def run(conn: sqlite3.Connection, now: Optional[datetime] = None) -> Dict[str, list]:
    """Erinnern und ggf. beenden. Liefert, was passiert ist (für Log und Tests)."""
    now = now or now_utc()
    limit_h = limit_hours(conn)
    result = {"reminded": [], "closed": []}
    if limit_h <= 0:
        return result
    auto = get_setting(conn, "open_auto_close") == "1"
    border = to_utc_str(now - timedelta(hours=limit_h))
    rows = conn.execute(
        "SELECT e.*, u.display_name, u.email, u.username FROM entries e JOIN users u ON u.id = e.user_id "
        "WHERE e.end_utc IS NULL AND e.start_utc <= ?", (border,)).fetchall()
    for e in rows:
        start = parse_utc(e["start_utc"])
        if auto and now >= close_time(start, limit_h):
            end = to_utc_str(start + timedelta(hours=limit_h))
            note = (e["note"] + " · " if e["note"] else "") + AUTO_NOTE
            conn.execute("UPDATE entries SET end_utc = ?, auto_closed = 1, note = ?, updated_at = ? WHERE id = ?",
                         (end, note[:200], to_utc_str(now), e["id"]))
            audit(conn, None, "entry_auto_closed", f"entry:{e['id']}", f"{e['username']}: Ende auf {end} gesetzt")
            result["closed"].append(e["id"])
            continue
        if not e["reminded_at"]:
            sent = _remind(conn, e, e, limit_h, auto)
            conn.execute("UPDATE entries SET reminded_at = ? WHERE id = ?", (to_utc_str(now), e["id"]))
            audit(conn, None, "entry_reminder", f"entry:{e['id']}", ", ".join(sent) or "keine Mail (keine Adresse/SMTP)")
            result["reminded"].append(e["id"])
    return result
